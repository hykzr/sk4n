from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from sk4n.outlook import auth, browser, cli, client
from sk4n.tools.shared import load_session, save_session


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    monkeypatch.setenv("SK4N_SESSION_DIR", str(tmp_path / "sessions"))
    data: dict[str, Any] = {
        "storage_state": {"cookies": [], "origins": []},
        "access_token": "test-secret",
        "base_url": browser.DEFAULT_BASE_URL,
    }
    save_session(browser.DEFAULT_SITE_NAME, data)
    return data


class JSONResponse(requests.Response):
    def __init__(self, payload: Any, status: int = 200):
        super().__init__()
        self.status_code = status
        self._content = json.dumps(payload).encode()


def response(payload: Any, status: int = 200) -> requests.Response:
    return JSONResponse(payload, status)


def exchange_response(root: dict[str, Any]) -> dict[str, Any]:
    return {
        "Body": {"ResponseMessages": {"Items": [{"ResponseCode": "NoError", "RootFolder": root}]}}
    }


def test_missing_auth_does_not_open_browser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SK4N_SESSION_DIR", str(tmp_path))
    monkeypatch.setattr(client, "capture_session", lambda **_kw: pytest.fail("unexpected browser"))
    assert not auth.check_auth_status().authenticated
    with pytest.raises(client.OutlookAuthError, match="auth login"):
        client.OutlookClient().folders()


def test_status_json_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "check_auth_status",
        lambda **_kw: auth.AuthStatus(True, "Student", "test@u.nus.edu", "user"),
    )
    assert cli.main(["auth", "status", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "authenticated": True,
        "name": "Student",
        "email": "test@u.nus.edu",
        "user_id": "user",
        "error": None,
    }


@pytest.mark.parametrize(
    "path,method,data",
    [
        ("https://attacker.test/owa/service.svc?action=GetItem", "POST", None),
        ("//attacker.test/owa/service.svc?action=GetItem", "POST", None),
        ("/owa/service.svc?action=SendItem", "GET", None),
        ("/owa/service.svc?action=DeleteItem", "POST", None),
        ("/owa/service.svc?action=GetItem&action=SendItem", "POST", None),
        ("/owa/service.svc?action=GetItem", "DELETE", None),
        ("/search/api/v2/query", "GET", None),
        ("/search/api/v2/query?action=SendItem", "POST", None),
        ("/search/api/v2/events", "POST", None),
        ("/owa/service.svc?action=GetItem", "POST", {"__type": "SendItemJsonRequest:#Exchange"}),
        (
            "/owa/service.svc?action=GetItem",
            "POST",
            {"Body": {"__type": "SendItemRequest:#Exchange"}},
        ),
    ],
)
def test_api_rejects_unsafe_requests_before_auth(path: str, method: str, data: Any) -> None:
    with pytest.raises(ValueError):
        client.OutlookClient().request(method, path, data)


@pytest.mark.parametrize(
    "url",
    [
        "http://outlook.office.com/mail/",
        "https://outlook.office.com.attacker.test/",
        "https://attacker.test/",
        "https://user:password@outlook.office.com/",
        "https://outlook.office.com:8443/",
    ],
)
def test_base_url_rejects_credential_destinations(url: str) -> None:
    with pytest.raises(ValueError):
        browser.validate_base_url(url)


def test_login_hint_removes_forced_realm() -> None:
    assert (
        browser.login_url("https://outlook.office.com/mail/?realm=nus.edu.sg", "e123@u.nus.edu")
        == "https://outlook.office.com/mail/?login_hint=e123%40u.nus.edu"
    )


def test_expired_token_renews_once_and_retries(
    monkeypatch: pytest.MonkeyPatch, saved: dict[str, Any]
) -> None:
    requests_seen: list[dict[str, Any]] = []
    renewals: list[dict[str, Any]] = []

    def request(_self: Any, _method: str, _url: str, **kwargs: Any) -> requests.Response:
        requests_seen.append(kwargs)
        return (
            response({}, 401)
            if len(requests_seen) == 1
            else response({"SessionSettings": {"UserEmailAddress": "student@u.nus.edu"}})
        )

    def renew(**kwargs: Any) -> dict[str, Any]:
        renewals.append(kwargs)
        return {**saved, "access_token": "renewed-secret"}

    monkeypatch.setattr(requests.Session, "request", request)
    monkeypatch.setattr(client, "capture_session", renew)
    assert client.OutlookClient().profile()["UserEmailAddress"] == "student@u.nus.edu"
    assert len(renewals) == 1
    assert renewals[0].get("headed", False) is False
    assert [r["headers"]["Authorization"] for r in requests_seen] == [
        "Bearer test-secret",
        "Bearer renewed-secret",
    ]
    assert all(r["allow_redirects"] is False for r in requests_seen)


def test_repeated_auth_failure_is_bounded(
    monkeypatch: pytest.MonkeyPatch, saved: dict[str, Any]
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        requests.Session, "request", lambda *_a, **_kw: calls.append("request") or response({}, 401)
    )
    monkeypatch.setattr(client, "capture_session", lambda **_kw: calls.append("renew") or saved)
    with pytest.raises(client.OutlookAuthError, match="expired"):
        client.OutlookClient().profile()
    assert calls == ["request", "renew", "request"]


def test_transport_failure_keeps_saved_session(
    monkeypatch: pytest.MonkeyPatch, saved: dict[str, Any]
) -> None:
    def fail(*_a: Any, **_kw: Any) -> Any:
        raise requests.ConnectionError("network unavailable")

    monkeypatch.setattr(requests.Session, "request", fail)
    with pytest.raises(client.OutlookTransportError):
        client.OutlookClient().profile()
    assert load_session(browser.DEFAULT_SITE_NAME) == saved


def test_folder_pagination_and_stalled_page(monkeypatch: pytest.MonkeyPatch) -> None:
    c = client.OutlookClient()
    pages = iter(
        [
            exchange_response(
                {
                    "Folders": [{"FolderId": {"Id": "one"}}],
                    "IncludesLastItemInRange": False,
                    "IndexedPagingOffset": 1,
                }
            ),
            exchange_response(
                {"Folders": [{"FolderId": {"Id": "two"}}], "IncludesLastItemInRange": True}
            ),
        ]
    )
    monkeypatch.setattr(c, "action", lambda *_a: next(pages))
    assert [f["FolderId"]["Id"] for f in c.folders()] == ["one", "two"]
    monkeypatch.setattr(
        c,
        "action",
        lambda *_a: exchange_response({"Folders": [], "IncludesLastItemInRange": False}),
    )
    with pytest.raises(client.OutlookError, match="pagination"):
        c.folders()


def test_message_read_uses_only_get_item(monkeypatch: pytest.MonkeyPatch) -> None:
    c = client.OutlookClient()
    seen: list[tuple[str, dict[str, Any]]] = []

    def action(name: str, body: dict[str, Any]) -> dict[str, Any]:
        seen.append((name, body))
        return {
            "Body": {
                "ResponseMessages": {
                    "Items": [
                        {
                            "ResponseCode": "NoError",
                            "Items": [{"ItemId": {"Id": "test"}, "IsRead": False}],
                        }
                    ]
                }
            }
        }

    monkeypatch.setattr(c, "action", action)
    assert c.message("test")["IsRead"] is False
    assert [name for name, _body in seen] == ["GetItem"]
    assert seen[0][1]["ItemShape"]["BodyType"] == "Text"


def test_search_and_unread_use_folder_scoped_search(monkeypatch: pytest.MonkeyPatch) -> None:
    c = client.OutlookClient()
    seen: list[Any] = []

    def request(method: str, path: str, data: Any = None) -> dict[str, Any]:
        seen.append((method, path, data))
        return {
            "EntitySets": [
                {
                    "ResultSets": [
                        {
                            "Results": [
                                {
                                    "Source": {
                                        "ItemId": {"Id": "one"},
                                        "IsRead": False,
                                        "UnknownField": "preserved",
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }

    monkeypatch.setattr(c, "request", request)
    result = c.messages(unread=True, limit=2, offset=3)
    assert result[0]["UnknownField"] == "preserved"
    method, path, data = seen[0]
    assert (method, path) == ("POST", "/searchservice/api/v2/query")
    search = data["EntityRequests"][0]
    assert search["EntityType"] == "Message"
    assert search["Query"]["QueryString"] == "isread:no"
    assert search["Filter"] == {"Term": {"DistinguishedFolderName": "inbox"}}
    assert (search["From"], search["Size"]) == (3, 2)
    c.messages(query="subject:deadline", folder="opaque-folder-id")
    assert seen[-1][2]["EntityRequests"][0]["Filter"] == {"Term": {"FolderId": "opaque-folder-id"}}


def test_search_error_does_not_look_like_empty_results(monkeypatch: pytest.MonkeyPatch) -> None:
    c = client.OutlookClient()
    monkeypatch.setattr(c, "request", lambda *_a: {"Error": "changed schema"})
    with pytest.raises(client.OutlookError, match="search result"):
        c.messages(query="deadline")


def test_people_lookup_preserves_ambiguous_matches_and_directory_fields(
    monkeypatch: pytest.MonkeyPatch, saved: dict[str, Any]
) -> None:
    seen: list[Any] = []
    people = [
        {
            "Id": "one",
            "DisplayName": "Alex Tan",
            "EmailAddresses": ["alex@nus.edu.sg", "alias@nus.edu.sg"],
            "Department": "Computing",
            "JobTitle": "Lecturer",
            "UnknownField": "preserved",
        },
        {
            "Id": "two",
            "DisplayName": "Alex Tan",
            "EmailAddresses": ["e1234567@u.nus.edu"],
            "JobTitle": "Student",
        },
    ]

    def request(_self: Any, method: str, url: str, **kwargs: Any) -> requests.Response:
        seen.append((method, url, kwargs))
        return response(
            {"EntitySets": [{"ResultSets": [{"Results": [{"Source": p} for p in people]}]}]}
        )

    monkeypatch.setattr(requests.Session, "request", request)
    assert client.OutlookClient().people("  Alex Tan  ", limit=2) == people
    method, url, kwargs = seen[0]
    assert method == "POST" and url.endswith("/search/api/v2/query")
    assert kwargs["headers"]["Authorization"] == "Bearer test-secret"
    search = kwargs["json"]["EntityRequests"][0]
    assert search["Query"]["QueryString"] == "Alex Tan"
    assert search["EntityType"] == "People"
    assert search["ContentSources"] == ["Exchange"]
    assert search["Provenances"] == ["Directory"]
    assert search["Size"] == 2
    assert search["Filter"] == {
        "And": [
            {"Term": {"PeopleType": "Person"}},
            {
                "Or": [
                    {"Term": {"PeopleSubtype": "OrganizationUser"}},
                    {"Term": {"PeopleSubtype": "OrganizationContact"}},
                ]
            },
        ]
    }


@pytest.mark.parametrize("query,limit", [("", 20), ("  ", 20), ("Alex", 0), ("Alex", -1)])
def test_people_invalid_input_does_not_make_a_request(
    monkeypatch: pytest.MonkeyPatch, query: str, limit: int
) -> None:
    c = client.OutlookClient()
    monkeypatch.setattr(c, "request", lambda *_a: pytest.fail("unexpected request"))
    with pytest.raises(ValueError):
        c.people(query, limit=limit)


def test_people_no_matches_and_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    c = client.OutlookClient()
    monkeypatch.setattr(
        c, "request", lambda *_a: {"EntitySets": [{"ResultSets": [{"Results": []}]}]}
    )
    assert c.people("Missing Person") == []
    monkeypatch.setattr(
        c,
        "request",
        lambda *_a: {
            "EntitySets": [
                {
                    "ResultSets": [
                        {"Results": [{"Source": {"Id": "one"}}, {"Source": {"Id": "two"}}]}
                    ]
                }
            ]
        },
    )
    assert c.people("Alex", limit=1) == [{"Id": "one"}]
    monkeypatch.setattr(c, "request", lambda *_a: {"Error": "changed schema"})
    with pytest.raises(client.OutlookError, match="search result"):
        c.people("Alex")


@pytest.mark.parametrize("output_format", ["json", "jsonl"])
def test_people_cli_structured_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], output_format: str
) -> None:
    people = [
        {"DisplayName": "Alex Tan", "EmailAddresses": ["alex@nus.edu.sg"]},
        {"DisplayName": "Alex Tan", "EmailAddresses": ["e1234567@u.nus.edu"]},
    ]
    queries: list[Any] = []

    def lookup(_self: Any, query: str, *, limit: int) -> Any:
        queries.append((query, limit))
        return people

    monkeypatch.setattr(client.OutlookClient, "people", lookup)
    assert cli.main(["people", "Alex Tan", "--limit", "2", "--format", output_format]) == 0
    output = capsys.readouterr().out
    parsed = (
        json.loads(output)
        if output_format == "json"
        else [json.loads(row) for row in output.splitlines()]
    )
    assert parsed == people
    assert queries == [("Alex Tan", 2)]


def test_invalid_offset_and_timeout_are_validation_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["messages", "--offset", "-1"]) == 2
    assert "offset" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["--timeout", "0", "auth", "status"])
    assert exc.value.code == 2


def test_capture_preserves_indexeddb_and_closes_browser(
    monkeypatch: pytest.MonkeyPatch, saved: dict[str, Any]
) -> None:
    calls: list[Any] = []

    class Page:
        url = browser.DEFAULT_BASE_URL

        def on(self, _name: str, handler: Any) -> None:
            self.handler = handler

        async def goto(self, *_a: Any, **_kw: Any) -> None:
            async def headers() -> dict[str, str]:
                return {"authorization": "Bearer renewed-secret"}

            await self.handler(
                SimpleNamespace(
                    url=browser.DEFAULT_BASE_URL.replace("/mail/", "/owa/startupdata.ashx"),
                    all_headers=headers,
                )
            )

    class Context:
        request: Any

        async def new_page(self) -> Page:
            return Page()

        async def post(self, *_a: Any, **_kw: Any) -> Any:
            async def payload() -> Any:
                return {
                    "SessionSettings": {
                        "UserEmailAddress": "test@u.nus.edu",
                        "UserPrincipalName": "user@nus.edu.sg",
                    }
                }

            return SimpleNamespace(ok=True, json=payload)

        async def storage_state(self, *, indexed_db: bool) -> Any:
            calls.append(("indexed_db", indexed_db))
            return {
                "cookies": [],
                "origins": [
                    {"origin": "https://outlook.cloud.microsoft", "indexedDB": [{"name": "msal"}]}
                ],
            }

    context = Context()
    context.request = context

    class Browser:
        async def new_context(self, **kwargs: Any) -> Context:
            calls.append(("restored", kwargs["storage_state"]))
            return context

        async def close(self) -> None:
            calls.append("closed")

    class Chromium:
        async def launch(self, **kwargs: Any) -> Browser:
            calls.append(kwargs)
            return Browser()

    class Playwright:
        async def __aenter__(self) -> Any:
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *_a: Any) -> None:
            pass

    monkeypatch.setattr(browser, "async_playwright", Playwright)
    result = asyncio.run(
        browser.capture_session_async(
            site_name=browser.DEFAULT_SITE_NAME, base_url=browser.DEFAULT_BASE_URL
        )
    )
    assert result["access_token"] == "renewed-secret"
    assert ("indexed_db", True) in calls
    assert ("restored", saved["storage_state"]) in calls
    assert calls[-1] == "closed"
    assert load_session(browser.DEFAULT_SITE_NAME) == result
