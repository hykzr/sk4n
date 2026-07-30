from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

from talent_connect.cli import build_parser, filters_from_args
from talent_connect.client import KinobiClient
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
        "posted_after": "2026-07-01T00:00:00Z",
    }
    assert args.max_jobs == 10
    assert args.format == "jsonl"


def test_database_contains_plain_json_not_pickles(tmp_path: Path) -> None:
    with TalentConnectStore(tmp_path / "jobs.sqlite3") as store:
        store.upsert_jobs([sample_job()])
        row = store.connection.execute("SELECT data_json FROM jobs WHERE id = 'job-1'").fetchone()

    assert row is not None
    assert json.loads(row["data_json"])["_id"] == "job-1"
