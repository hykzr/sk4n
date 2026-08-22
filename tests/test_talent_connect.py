from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

import talent_connect.cli as talent_cli
from talent_connect.authenticated_client import (
    AuthenticatedKinobiClient,
    workflow_record_to_job,
)
from talent_connect.cli import build_parser, filters_from_args, handle_api
from talent_connect.client import KinobiAPIError, KinobiClient
from talent_connect.storage import TalentConnectStore, job_matches_filters, summarize_job


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)  # type: ignore[arg-type]

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def mount(self, *_args: object) -> None:
        pass

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float,
    ) -> FakeResponse:
        self.calls.append((url, params))
        return self.responses.pop(0)


def sample_company() -> dict[str, Any]:
    return {
        "_id": "company-db-id",
        "company_id": "acme",
        "slug": "acme-sg",
        "name": "Acme",
        "type": "company",
        "industry": "technology",
        "industries": ["technology"],
        "country_code": "SG",
        "updated_at": "2026-07-30T01:00:00Z",
    }


def sample_job(**overrides: Any) -> dict[str, Any]:
    job = {
        "_id": "job-1",
        "slug": "software-engineer-acme",
        "title": "Software Engineer",
        "description": "<p>Build Python services.</p>",
        "description_text": "Build Python services.",
        "employment_type": "internship",
        "work_arrangement": "hybrid",
        "country_code": "SG",
        "city": "Singapore",
        "application_type": "external job",
        "expired_at": "2026-12-01T00:00:00Z",
        "updated_at": "2026-07-30T02:00:00Z",
        "company": sample_company(),
    }
    job.update(overrides)
    return job


def test_cli_exposes_playwright_cli_command() -> None:
    parser = build_parser()

    defaults = parser.parse_args(["playwright-cli"])
    assert defaults.url is None
    assert defaults.headed is False
    assert defaults.session == "talent-connect"

    configured = parser.parse_args(
        [
            "playwright-cli",
            "--url",
            "https://talent.example.test/jobs",
            "--headless",
            "-s",
            "talent-debug",
        ]
    )
    assert configured.url == "https://talent.example.test/jobs"
    assert configured.headed is False
    assert configured.session == "talent-debug"


def test_playwright_cli_command_requires_explicit_login(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[dict[str, Any]] = []
    monkeypatch.setattr(talent_cli, "playwright_cli_executable", lambda: "/bin/playwright-cli")
    monkeypatch.setattr(talent_cli, "ensure_session_available", lambda *_args: None)
    monkeypatch.setattr(
        talent_cli,
        "check_auth_status",
        lambda **_kwargs: SimpleNamespace(authenticated=False, display_name="", email=""),
    )
    login_called = False

    def unexpected_login(**_kwargs: Any) -> None:
        nonlocal login_called
        login_called = True

    monkeypatch.setattr(talent_cli, "login", unexpected_login)
    monkeypatch.setattr(
        talent_cli,
        "open_authenticated_session",
        lambda **kwargs: opened.append(kwargs),
    )
    args = build_parser().parse_args(["playwright-cli", "--session", "talent-test"])

    with pytest.raises(KinobiAPIError, match="talent-connect auth login"):
        talent_cli.handle_playwright_cli(args)
    assert not login_called
    assert opened == []


def test_client_paginates_and_encodes_repeatable_filters() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "data": [sample_job()],
                    "pagination": {"total_pages": 2},
                }
            ),
            FakeResponse(
                {
                    "data": [sample_job(_id="job-2", slug="job-2")],
                    "pagination": {"total_pages": 2},
                }
            ),
        ]
    )
    client = KinobiClient(base_url="https://example.test", session=session)  # type: ignore[arg-type]

    jobs = client.list_jobs(
        filters={
            "query": "engineer",
            "employment_types": ["internship", "contract"],
        },
        page_size=50,
    )

    assert [job["_id"] for job in jobs] == ["job-1", "job-2"]
    assert session.calls[0][0] == "https://example.test/api/job/public"
    assert session.calls[0][1] == {
        "query": "engineer",
        "employment_types": "internship,contract",
        "page": 1,
        "entries_per_page": 50,
    }
    assert session.calls[1][1]["page"] == 2  # type: ignore[index]


def test_client_respects_max_jobs_without_fetching_another_page() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "data": [
                        sample_job(),
                        sample_job(_id="job-2", slug="job-2"),
                    ],
                    "pagination": {"total_pages": 10},
                }
            )
        ]
    )
    client = KinobiClient(base_url="https://example.test", session=session)  # type: ignore[arg-type]

    jobs = client.list_jobs(max_jobs=1)

    assert [job["_id"] for job in jobs] == ["job-1"]
    assert len(session.calls) == 1
    assert session.calls[0][1]["entries_per_page"] == 100  # type: ignore[index]


def test_storage_upserts_changed_records_and_preserves_detail_fields(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    with TalentConnectStore(database) as store:
        first = store.upsert_jobs([sample_job()], detail_level=1)
        unchanged = store.upsert_jobs([sample_job()], detail_level=1)
        detailed = sample_job(role="software engineering", number_of_applicants=3)
        updated = store.upsert_jobs([detailed], detail_level=2)
        lower_detail = sample_job(title="Senior Software Engineer")
        merged = store.upsert_jobs([lower_detail], detail_level=1)
        stored = store.get_job("job-1")

    assert (first.inserted, first.updated, first.unchanged) == (1, 0, 0)
    assert (unchanged.inserted, unchanged.updated, unchanged.unchanged) == (0, 0, 1)
    assert updated.updated == 1
    assert merged.updated == 1
    assert stored is not None
    assert stored["title"] == "Senior Software Engineer"
    assert stored["role"] == "software engineering"
    assert stored["number_of_applicants"] == 3


def test_detail_refresh_uses_remote_updated_at(tmp_path: Path) -> None:
    with TalentConnectStore(tmp_path / "jobs.sqlite3") as store:
        listing = sample_job(updated_at="2026-07-30T02:00:00Z")
        store.upsert_jobs([listing], detail_level=1)
        assert store.job_needs_detail_refresh("job-1", "2026-07-30T02:00:00Z")

        store.upsert_jobs([sample_job(role="engineering")], detail_level=2)
        assert not store.job_needs_detail_refresh("job-1", "2026-07-30T02:00:00Z")

        changed_listing = sample_job(updated_at="2026-07-31T02:00:00Z")
        store.upsert_jobs([changed_listing], detail_level=1)
        assert store.job_needs_detail_refresh("job-1", "2026-07-31T02:00:00Z")


def test_list_and_detail_shapes_do_not_oscillate_update_status(tmp_path: Path) -> None:
    listing = sample_job(published_at="2026-01-01T00:00:00Z")
    detail = sample_job(
        published_at="2026-02-01T00:00:00Z",
        role="software engineering",
    )
    with TalentConnectStore(tmp_path / "jobs.sqlite3") as store:
        store.upsert_jobs([listing], detail_level=1)
        store.upsert_jobs([detail], detail_level=2)
        repeated = store.upsert_jobs([listing], detail_level=1)
        stored = store.get_job("job-1")

    assert repeated.unchanged == 1
    assert stored is not None
    assert stored["published_at"] == "2026-02-01T00:00:00Z"
    assert stored["role"] == "software engineering"


def test_summary_records_do_not_erase_existing_verbose_details(tmp_path: Path) -> None:
    with TalentConnectStore(tmp_path / "jobs.sqlite3") as store:
        store.upsert_jobs([sample_job()], detail_level=1)
        store.upsert_jobs([summarize_job(sample_job(title="Updated title"))], detail_level=0)
        stored = store.get_job("job-1")

    assert stored is not None
    assert stored["title"] == "Updated title"
    assert stored["description_text"] == "Build Python services."


def test_volatile_view_counts_do_not_mark_a_job_changed(tmp_path: Path) -> None:
    with TalentConnectStore(tmp_path / "jobs.sqlite3") as store:
        store.upsert_jobs([sample_job(number_of_views=1)])
        stats = store.upsert_jobs([sample_job(number_of_views=2)])
        stored = store.get_job("job-1")

    assert stats.unchanged == 1
    assert stored is not None
    assert stored["number_of_views"] == 2


def test_updated_jobs_are_returned_separately_from_unchanged_jobs(tmp_path: Path) -> None:
    with TalentConnectStore(tmp_path / "jobs.sqlite3") as store:
        store.upsert_jobs([sample_job(), sample_job(_id="job-2", slug="job-2")])
        stats, changed = store.upsert_jobs_with_changes(
            [
                sample_job(),
                sample_job(
                    _id="job-2",
                    slug="job-2",
                    user_has_applied=True,
                ),
            ]
        )

    assert stats.updated == 1
    assert stats.unchanged == 1
    assert [job["_id"] for job in changed] == ["job-2"]


def test_cached_job_filters_cover_query_and_structured_fields() -> None:
    job = sample_job()

    assert job_matches_filters(
        job,
        {
            "query": "python",
            "employment_types": ["internship"],
            "companies": ["acme"],
            "country_codes": ["SG"],
        },
    )
    assert not job_matches_filters(job, {"employment_types": ["full time"]})
    assert not job_matches_filters(job, {"exclude_company_types": ["company"]})


def test_cached_job_filters_cover_authenticated_and_local_fields() -> None:
    job = sample_job(
        is_unqualified_student=False,
        is_bookmarked=True,
        is_open_for_special_need=True,
        hard_industry_skill_value_ids=["python_4"],
        published_at="2026-07-30T08:00:00Z",
    )

    assert job_matches_filters(
        job,
        {
            "is_qualified": True,
            "is_my_jobs": True,
            "is_open_for_special_need": True,
            "hard_industry_skill_value_ids": ["python_4"],
            "posted_after": "2026-07-30T00:00:00Z",
        },
    )
    assert not job_matches_filters(job, {"posted_after": "2026-07-31T00:00:00Z"})
    assert not job_matches_filters(
        sample_job(is_unqualified_student=True),
        {"is_qualified": True},
    )


def test_cli_parser_builds_fetch_filters() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "fetch",
            "--query",
            "engineer",
            "--company",
            "Acme",
            "--company",
            "Example",
            "--employment-type",
            "internship",
            "--applied",
            "--saved",
            "--qualified",
            "--status",
            "interviewing",
            "--status",
            "declined-offer",
            "--posted-after",
            "2026-07-01",
            "--max-jobs",
            "10",
            "--format",
            "jsonl",
        ]
    )

    assert filters_from_args(args) == {
        "query": "engineer",
        "companies": ["Acme", "Example"],
        "employment_types": ["internship"],
        "is_applied": True,
        "is_my_jobs": True,
        "is_qualified": True,
        "talent_connect_statuses": ["interviewing", "declined-offer"],
        "posted_after": "2026-07-01T00:00:00Z",
    }
    assert args.max_jobs == 10
    assert args.format == "jsonl"


def test_cli_parser_builds_authenticated_api_request() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "api",
            "/api/job?entries_per_page=1",
            "--method",
            "post",
            "--data",
            '{"query": "engineer"}',
        ]
    )

    assert args.command == "api"
    assert args.path == "/api/job?entries_per_page=1"
    assert args.method == "POST"
    assert args.data == {"query": "engineer"}


def test_authenticated_request_rejects_external_urls() -> None:
    with pytest.raises(ValueError, match="must start with /api/"):
        AuthenticatedKinobiClient.validate_request(
            "GET",
            "https://example.test/api/auth/",
        )


def test_handle_api_prints_only_json_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()
    args = parser.parse_args(["api", "/api/auth/"])

    def fake_request(
        _self: AuthenticatedKinobiClient,
        method: str,
        path: str,
        data: Any,
    ) -> dict[str, Any]:
        assert (method, path, data) == ("GET", "/api/auth/", None)
        return {"data": {"authenticated": True}}

    monkeypatch.setattr(AuthenticatedKinobiClient, "request", fake_request)

    assert handle_api(args) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"data": {"authenticated": True}}
    assert captured.err == ""


def test_authenticated_request_reports_http_errors() -> None:
    class FakePage:
        async def evaluate(self, _script: str, request: dict[str, Any]) -> dict[str, Any]:
            assert request == {
                "method": "DELETE",
                "path": "/api/example",
                "data": None,
            }
            return {
                "ok": False,
                "status": 403,
                "message": "Request failed with status code 403",
                "payload": {"message": "forbidden"},
            }

    client = AuthenticatedKinobiClient()
    with pytest.raises(KinobiAPIError, match=r"DELETE.*HTTP 403.*forbidden"):
        asyncio.run(client.make_request(FakePage(), "delete", "/api/example"))


def test_authenticated_get_uses_generic_request_transport() -> None:
    class FakePage:
        async def evaluate(self, _script: str, request: dict[str, Any]) -> dict[str, Any]:
            assert request == {
                "method": "GET",
                "path": "/api/example",
                "data": None,
            }
            return {"ok": True, "payload": {"data": ["result"]}}

    client = AuthenticatedKinobiClient()

    assert asyncio.run(client.make_request(FakePage(), "GET", "/api/example")) == {
        "data": ["result"]
    }


def test_workflow_application_record_becomes_job_without_profile_data() -> None:
    application = {
        "_id": "application-1",
        "status": "rejected",
        "updated_at": "2026-07-30T03:00:00Z",
        "job": sample_job(company="company-db-id"),
        "user": {"email": "student@example.test"},
    }

    job = workflow_record_to_job(
        application,
        workflow_status="declined",
        source="application",
    )

    assert job is not None
    assert job["talent_connect_statuses"] == ["declined"]
    assert job["job_application"]["status"] == "rejected"
    assert "user" not in job["job_application"]
    assert "company" not in job
    assert job["user_has_applied"] is True


def test_cached_workflow_status_filters_distinguish_applications_and_offers() -> None:
    declined_application = sample_job(job_application={"status": "rejected"})
    accepted_offer = sample_job(
        job_offer={"response": "accepted", "is_past": False},
    )

    assert job_matches_filters(
        declined_application,
        {"talent_connect_statuses": ["declined"]},
    )
    assert not job_matches_filters(
        declined_application,
        {"talent_connect_statuses": ["declined-offer"]},
    )
    assert job_matches_filters(
        accepted_offer,
        {"talent_connect_statuses": ["accepted-offer"]},
    )


def test_database_contains_plain_json_not_pickles(tmp_path: Path) -> None:
    with TalentConnectStore(tmp_path / "jobs.sqlite3") as store:
        store.upsert_jobs([sample_job()])
        row = store.connection.execute("SELECT data_json FROM jobs WHERE id = 'job-1'").fetchone()

    assert row is not None
    assert json.loads(row["data_json"])["_id"] == "job-1"
