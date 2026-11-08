from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from sk4n.outlook import cli, preview
from sk4n.outlook.client import OutlookAuthError, OutlookClient, OutlookError
from sk4n.outlook.storage import OutlookStore


@pytest.mark.parametrize(
    "action",
    [
        "UpdateItem",
        "CreateItem",
        "SendItem",
        "DeleteItem",
        "ApplyConversationAction",
        "UnknownAction",
    ],
)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_preview_blocks_service_writes_even_when_sent_via_headers(action: str, method: str) -> None:
    root = "https://outlook.cloud.microsoft/owa/service.svc"
    assert preview.is_mailbox_write(root + "?action=" + action, method, {})
    assert preview.is_mailbox_write(root, method, {"x-owa-action": action})
    assert preview.is_mailbox_write(root + "/CreateAttachmentFromLocalFile", method, {})
    assert not preview.is_mailbox_write(root + "?action=GetItem", "POST", {})
    assert not preview.is_mailbox_write("https://outlook.cloud.microsoft/mail/", "GET", {})


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    events: list[Any] = []

    class Locator:
        first: Any

        def __init__(self, label: Any):
            self.label = label
            self.first = self

        async def wait_for(self) -> None:
            events.append(("wait", self.label))

        async def click(self) -> None:
            events.append(("click", self.label))

    class Page:
        def set_default_timeout(self, timeout: int) -> None:
            pass

        async def goto(self, url: str, **kwargs: Any) -> None:
            events.append(("goto", url))

        def get_by_role(self, role: str, *, name: Any, **kwargs: Any) -> Locator:
            return Locator((role, str(name)))

        def get_by_text(self, name: Any) -> Locator:
            return Locator(("text", str(name)))

        async def evaluate(self, script: str) -> None:
            pass

        async def screenshot(self, *, path: str, **kwargs: Any) -> None:
            events.append("screenshot")
            Path(path).write_bytes(b"test screenshot")

    class Context:
        async def route(self, pattern: str, callback: Any) -> None:
            class Request:
                url = "https://outlook.cloud.microsoft/owa/service.svc?action=UpdateItem"
                method = "POST"

                def __init__(self) -> None:
                    self.headers: dict[str, str] = {}

            class Route:
                request = Request()

                async def abort(self) -> None:
                    events.append("write blocked")

                async def continue_(self) -> None:
                    pytest.fail("Preview permitted a mailbox write")

            await callback(Route())

        async def new_page(self) -> Page:
            return Page()

    class Browser:
        async def new_context(self, **kwargs: Any) -> Context:
            assert kwargs["service_workers"] == "block"
            return Context()

        async def close(self) -> None:
            events.append("closed")

    class Playwright:
        chromium: Any

        def __init__(self) -> None:
            self.chromium = self

        async def __aenter__(self) -> Playwright:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def launch(self, **kwargs: Any) -> Browser:
            return Browser()

    monkeypatch.setattr(preview, "async_playwright", Playwright)
    monkeypatch.setattr(preview, "load_session", lambda name: {"storage_state": {}})
    return events


def message(**fields: Any) -> dict[str, Any]:
    return {
        "ItemId": {"Id": "one", "ChangeKey": "v1"},
        "Subject": "Test",
        "IsDraft": True,
        "IsRead": False,
        "Body": {"BodyType": "Text", "Value": "Hello"},
        "ToRecipients": [{"EmailAddress": "self@example.com"}],
        "Attachments": [{"Name": "preview.png", "AttachmentId": {"Id": "file"}}],
        **fields,
    }


class Client(OutlookClient):
    def __init__(self) -> None:
        super().__init__()
        self.item = message()
        self.calls: list[str] = []

    def message(self, message_id: str) -> dict[str, Any]:
        self.calls.append("message")
        return deepcopy(self.item)

    def message_metadata(self, message_id: str) -> dict[str, Any]:
        self.calls.append("metadata")
        return {"ItemId": deepcopy(self.item["ItemId"])}


def test_preview_waits_for_recipients_and_attachments_and_closes_browser(
    browser: list[Any], tmp_path: Path
) -> None:
    output = tmp_path / "preview.png"
    preview.capture_preview(Client(), message(), output)
    assert output.read_bytes() == b"test screenshot"
    assert "write blocked" in browser
    before_screenshot = browser[: browser.index("screenshot")]
    assert any(isinstance(e, tuple) and "self@example" in str(e) for e in before_screenshot)
    assert any(
        isinstance(e, tuple) and "preview" in str(e) and "option" in str(e)
        for e in before_screenshot
    )
    assert browser[-1] == "closed"


def test_preview_rejects_changed_message_without_replacing_existing_image(
    browser: list[Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "preview.png"
    output.write_bytes(b"previous image")
    client = Client()
    monkeypatch.setattr(
        client, "message", lambda identifier: message(ItemId={"Id": "one", "ChangeKey": "v2"})
    )
    with pytest.raises(OutlookError, match="changed during preview"):
        preview.capture_preview(client, message(), output)
    assert output.read_bytes() == b"previous image"
    assert browser[-1] == "closed"


def test_received_message_preview_does_not_open_composer(
    browser: list[Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Client()
    monkeypatch.setattr(client, "message", lambda identifier: message(IsDraft=False))
    preview.capture_preview(client, message(IsDraft=False), tmp_path / "read.png")
    assert not any(isinstance(e, tuple) and e[0] == "click" for e in browser)


def test_cli_draft_and_preview_grammar() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "draft",
            "edit",
            "id",
            "--to",
            "one@example.com",
            "--to",
            "two@example.com",
            "--clear-cc",
            "--body-file",
            "body.txt",
            "--attach",
            "one.png",
            "--attach",
            "two.png",
        ]
    )
    assert args.to == ["one@example.com", "two@example.com"]
    assert args.cc == [] and args.bcc is None
    assert args.body_file == Path("body.txt")
    assert args.attach == [Path("one.png"), Path("two.png")]
    assert parser.parse_args(["preview", "id"]).output is None
    assert parser.parse_args(["preview", "id", "--refresh"]).refresh_mode == "force"
    assert parser.parse_args(["preview", "id", "--no-refresh"]).refresh_mode == "none"
    assert parser.parse_args(["draft", "send", "id"]).message_id == "id"
    for command in (
        ["draft", "send"],
        ["draft", "send", "id", "--body", "direct send"],
        ["draft", "edit", "id", "--to", "a@example.com", "--clear-to"],
        ["preview", "id", "--refresh", "--no-refresh"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(command)


def test_preview_is_cached_beside_message_and_reused_without_browser(
    browser: list[Any],
    tmp_path: Path,
) -> None:
    client = Client()
    store = OutlookStore(tmp_path)
    previews = preview.OutlookPreview(client, store)
    first = previews.message("one")
    image = Path(first["_outlook"]["preview_path"])
    assert image.parent == Path(first["_outlook"]["path"]).parent
    assert image.name == "preview.png"
    assert not first["_outlook"]["preview_reused"]
    assert (record := store.message("one")) is not None
    assert record["preview"]["path"] == "preview.png"
    before = image.stat().st_mtime_ns
    browser.clear()
    client.calls.clear()
    second = previews.message("one")
    assert second["_outlook"]["preview_reused"]
    assert client.calls == ["metadata"]
    assert browser == []
    assert image.stat().st_mtime_ns == before
    client.calls.clear()
    offline = previews.message("one", refresh_mode="none")
    assert offline["_outlook"]["preview_reused"]
    assert client.calls == [] and browser == []


def test_force_refresh_and_changed_revision_recapture_preview(
    browser: list[Any],
    tmp_path: Path,
) -> None:
    client = Client()
    previews = preview.OutlookPreview(client, OutlookStore(tmp_path))
    previews.message("one")
    browser.clear()
    forced = previews.message("one", refresh_mode="force")
    assert not forced["_outlook"]["preview_reused"]
    assert "screenshot" in browser
    browser.clear()
    client.item.update(
        ItemId={"Id": "one", "ChangeKey": "v2"}, Body={"BodyType": "Text", "Value": "Edited"}
    )
    changed = previews.message("one")
    assert changed["Body"]["Value"] == "Edited"
    assert not changed["_outlook"]["preview_reused"]
    assert "screenshot" in browser


@pytest.mark.parametrize("damage", ["missing", "corrupt", "stale"])
def test_invalid_preview_is_rejected_offline_and_recaptured_online(
    damage: str,
    browser: list[Any],
    tmp_path: Path,
) -> None:
    client = Client()
    store = OutlookStore(tmp_path)
    previews = preview.OutlookPreview(client, store)
    first = previews.message("one")
    image = Path(first["_outlook"]["preview_path"])
    if damage == "missing":
        image.unlink()
    elif damage == "corrupt":
        image.write_bytes(b"X" * image.stat().st_size)
    else:
        record = store.message("one")
        assert record is not None
        record["message"]["Body"]["Value"] = "Edited with the same change key"
        store.save_message(record)
    browser.clear()
    client.calls.clear()
    with pytest.raises(OutlookError, match="Preview is not cached"):
        previews.message("one", refresh_mode="none")
    assert client.calls == [] and browser == []
    result = previews.message("one")
    assert not result["_outlook"]["preview_reused"]
    assert "screenshot" in browser


def test_output_exports_a_copy_and_preserves_canonical_cache(
    browser: list[Any],
    tmp_path: Path,
) -> None:
    client = Client()
    previews = preview.OutlookPreview(client, OutlookStore(tmp_path / "cache"))
    export = tmp_path / "export" / "review.png"
    first = previews.message("one", output=export)
    canonical = Path(first["_outlook"]["preview_path"])
    assert canonical != export
    assert canonical.read_bytes() == export.read_bytes()
    assert first["_outlook"]["preview_output_path"] == str(export)
    browser.clear()
    client.calls.clear()
    second_export = tmp_path / "second.png"
    second = previews.message("one", refresh_mode="none", output=second_export)
    assert second["_outlook"]["preview_reused"]
    assert second_export.read_bytes() == canonical.read_bytes()
    assert client.calls == [] and browser == []


def test_preview_missing_session_raises_auth_error_and_uses_saved_base_url(
    browser: list[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        preview,
        "load_session",
        lambda name: {
            "storage_state": {},
            "base_url": "https://outlook.office.com/mail/",
        },
    )
    output = tmp_path / "preview.png"
    preview.capture_preview(Client(), message(), output)
    assert ("goto", "https://outlook.office.com/mail/deeplink/read/one") in browser
    monkeypatch.setattr(preview, "load_session", lambda name: None)
    with pytest.raises(OutlookAuthError, match="auth login"):
        preview.capture_preview(Client(), message(), output)
