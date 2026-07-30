from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .client import utc_timestamp

DEFAULT_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "talent_connect"
DEFAULT_DATABASE_PATH = DEFAULT_DATA_PATH / "talent_connect.sqlite3"

VERBOSE_JOB_FIELDS = {
    "cc_email_addresses",
    "description",
    "description_text",
    "document_requirement_notes",
    "hard_industry_skill_value_ids",
    "job_additional_resource_assets",
    "requirements",
    "requirements_text",
    "responsibilities",
    "responsibilities_text",
    "soft_industry_skill_value_ids",
    "to_email_addresses",
}

VOLATILE_JOB_FIELDS = {
    "external_job_link_click_status",
    "external_job_link_clicked_apply",
    "number_of_applicants",
    "number_of_views",
}

VOLATILE_COMPANY_FIELDS = {"active_job_count"}


@dataclass
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged

    def add(self, other: UpsertStats) -> None:
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(serialized: str) -> str:
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _stable_company(company: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in company.items() if key not in VOLATILE_COMPANY_FIELDS}


def _stable_job(job: Mapping[str, Any]) -> dict[str, Any]:
    stable = {key: value for key, value in job.items() if key not in VOLATILE_JOB_FIELDS}
    if isinstance(stable.get("company"), dict):
        stable["company"] = _stable_company(stable["company"])
    return stable


def _merge_records(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_records(merged[key], value)
        else:
            merged[key] = value
    return merged


def summarize_job(job: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in job.items() if key not in VERBOSE_JOB_FIELDS}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    return str(value)


def _normalized_values(value: Any) -> set[str]:
    if isinstance(value, list):
        values = value
    elif value in (None, ""):
        values = []
    else:
        values = [value]
    return {_text(item).strip().casefold() for item in values if _text(item).strip()}


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class TalentConnectStore:
    def __init__(self, path: Path | str = DEFAULT_DATABASE_PATH) -> None:
        candidate = Path(path).expanduser()
        if candidate.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            self.path = candidate
        else:
            self.path = candidate / "talent_connect.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def __enter__(self) -> TalentConnectStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                slug TEXT,
                title TEXT NOT NULL,
                company_id TEXT,
                company_name TEXT,
                employment_type TEXT,
                country_code TEXT,
                remote_updated_at TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                detail_level INTEGER NOT NULL DEFAULT 1,
                detail_remote_updated_at TEXT NOT NULL DEFAULT '',
                list_fingerprint TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL,
                data_json TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_slug
                ON jobs(slug) WHERE slug IS NOT NULL AND slug != '';
            CREATE INDEX IF NOT EXISTS idx_jobs_company_id ON jobs(company_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_company_name ON jobs(company_name);
            CREATE INDEX IF NOT EXISTS idx_jobs_title ON jobs(title);

            CREATE TABLE IF NOT EXISTS companies (
                id TEXT PRIMARY KEY,
                slug TEXT,
                company_id TEXT,
                name TEXT NOT NULL,
                remote_updated_at TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                detail_level INTEGER NOT NULL DEFAULT 1,
                fingerprint TEXT NOT NULL,
                data_json TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_slug
                ON companies(slug) WHERE slug IS NOT NULL AND slug != '';
            CREATE INDEX IF NOT EXISTS idx_companies_company_id ON companies(company_id);
            CREATE INDEX IF NOT EXISTS idx_companies_name ON companies(name);
            """
        )
        job_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "list_fingerprint" not in job_columns:
            self.connection.execute(
                "ALTER TABLE jobs ADD COLUMN list_fingerprint TEXT NOT NULL DEFAULT ''"
            )
        if "detail_remote_updated_at" not in job_columns:
            self.connection.execute(
                """
                ALTER TABLE jobs
                ADD COLUMN detail_remote_updated_at TEXT NOT NULL DEFAULT ''
                """
            )
        self.connection.commit()

    def upsert_job(
        self,
        record: Mapping[str, Any],
        *,
        detail_level: int = 1,
        seen_at: str | None = None,
    ) -> UpsertStats:
        incoming_job = dict(record)
        job_id = str(incoming_job.get("_id") or "")
        if not job_id:
            raise ValueError("Cannot store a job without _id.")
        timestamp = seen_at or utc_timestamp()
        existing_row = self.connection.execute(
            """
            SELECT data_json, detail_level, detail_remote_updated_at,
                   list_fingerprint, fingerprint
            FROM jobs WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        existing: dict[str, Any] = {}
        existing_level = 0
        incoming_list_fingerprint = _fingerprint(_canonical_json(_stable_job(incoming_job)))
        list_changed = True
        if existing_row is not None:
            existing = json.loads(existing_row["data_json"])
            existing_level = int(existing_row["detail_level"])
            if detail_level <= 1:
                list_changed = existing_row["list_fingerprint"] != incoming_list_fingerprint
            else:
                list_changed = existing_row["fingerprint"] != incoming_list_fingerprint
        if existing and detail_level < existing_level and not list_changed:
            job = existing
        else:
            job = _merge_records(existing, incoming_job)
        serialized = _canonical_json(job)
        digest = _fingerprint(_canonical_json(_stable_job(job)))
        if detail_level <= 1 or existing_row is None:
            list_fingerprint = incoming_list_fingerprint
        else:
            list_fingerprint = str(existing_row["list_fingerprint"])
        company = _dict_value(job.get("company"))
        values = {
            "id": job_id,
            "slug": str(job.get("slug") or ""),
            "title": str(job.get("title") or "(untitled)"),
            "company_id": str(company.get("_id") or company.get("company_id") or ""),
            "company_name": str(company.get("name") or ""),
            "employment_type": str(job.get("employment_type") or ""),
            "country_code": str(job.get("country_code") or ""),
            "remote_updated_at": str(job.get("updated_at") or ""),
            "last_seen_at": timestamp,
            "detail_level": max(existing_level, detail_level),
            "detail_remote_updated_at": (
                str(job.get("updated_at") or "")
                if detail_level >= 2
                else (
                    str(existing_row["detail_remote_updated_at"])
                    if existing_row is not None
                    else ""
                )
            ),
            "list_fingerprint": list_fingerprint,
            "fingerprint": digest,
            "data_json": serialized,
        }
        if existing_row is None:
            self.connection.execute(
                """
                INSERT INTO jobs (
                    id, slug, title, company_id, company_name, employment_type,
                    country_code, remote_updated_at, first_seen_at, last_seen_at,
                    changed_at, detail_level, list_fingerprint, fingerprint, data_json
                    , detail_remote_updated_at
                ) VALUES (
                    :id, :slug, :title, :company_id, :company_name, :employment_type,
                    :country_code, :remote_updated_at, :last_seen_at, :last_seen_at,
                    :last_seen_at, :detail_level, :list_fingerprint, :fingerprint,
                    :data_json, :detail_remote_updated_at
                )
                """,
                values,
            )
            stats = UpsertStats(inserted=1)
        elif not list_changed:
            self.connection.execute(
                """
                UPDATE jobs SET
                    last_seen_at = ?,
                    detail_level = ?,
                    detail_remote_updated_at = ?,
                    list_fingerprint = ?,
                    data_json = ?
                WHERE id = ?
                """,
                (
                    timestamp,
                    values["detail_level"],
                    values["detail_remote_updated_at"],
                    list_fingerprint,
                    serialized,
                    job_id,
                ),
            )
            stats = UpsertStats(unchanged=1)
        else:
            self.connection.execute(
                """
                UPDATE jobs SET
                    slug = :slug,
                    title = :title,
                    company_id = :company_id,
                    company_name = :company_name,
                    employment_type = :employment_type,
                    country_code = :country_code,
                    remote_updated_at = :remote_updated_at,
                    last_seen_at = :last_seen_at,
                    changed_at = :last_seen_at,
                    detail_level = :detail_level,
                    detail_remote_updated_at = :detail_remote_updated_at,
                    list_fingerprint = :list_fingerprint,
                    fingerprint = :fingerprint,
                    data_json = :data_json
                WHERE id = :id
                """,
                values,
            )
            stats = UpsertStats(updated=1)
        if company.get("_id"):
            self.upsert_company(company, detail_level=1, seen_at=timestamp)
        return stats

    def upsert_jobs(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        detail_level: int = 1,
    ) -> UpsertStats:
        stats, _changed = self.upsert_jobs_with_changes(
            records,
            detail_level=detail_level,
        )
        return stats

    def upsert_jobs_with_changes(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        detail_level: int = 1,
    ) -> tuple[UpsertStats, list[dict[str, Any]]]:
        stats = UpsertStats()
        changed: list[dict[str, Any]] = []
        timestamp = utc_timestamp()
        for record in records:
            result = self.upsert_job(
                record,
                detail_level=detail_level,
                seen_at=timestamp,
            )
            stats.add(result)
            if result.inserted or result.updated:
                changed.append(dict(record))
        self.connection.commit()
        return stats, changed

    def upsert_company(
        self,
        record: Mapping[str, Any],
        *,
        detail_level: int = 1,
        seen_at: str | None = None,
    ) -> UpsertStats:
        company = dict(record)
        company_id = str(company.get("_id") or "")
        if not company_id:
            raise ValueError("Cannot store a company without _id.")
        timestamp = seen_at or utc_timestamp()
        existing_row = self.connection.execute(
            "SELECT data_json, detail_level, fingerprint FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        existing: dict[str, Any] = {}
        existing_level = 0
        if existing_row is not None:
            existing = json.loads(existing_row["data_json"])
            existing_level = int(existing_row["detail_level"])
            company = _merge_records(existing, company)
        serialized = _canonical_json(company)
        digest = _fingerprint(_canonical_json(_stable_company(company)))
        values = {
            "id": company_id,
            "slug": str(company.get("slug") or ""),
            "company_id": str(company.get("company_id") or ""),
            "name": str(company.get("name") or "(unnamed company)"),
            "remote_updated_at": str(company.get("updated_at") or ""),
            "last_seen_at": timestamp,
            "detail_level": max(existing_level, detail_level),
            "fingerprint": digest,
            "data_json": serialized,
        }
        if existing_row is None:
            self.connection.execute(
                """
                INSERT INTO companies (
                    id, slug, company_id, name, remote_updated_at, first_seen_at,
                    last_seen_at, changed_at, detail_level, fingerprint, data_json
                ) VALUES (
                    :id, :slug, :company_id, :name, :remote_updated_at, :last_seen_at,
                    :last_seen_at, :last_seen_at, :detail_level, :fingerprint, :data_json
                )
                """,
                values,
            )
            stats = UpsertStats(inserted=1)
        elif existing_row["fingerprint"] == digest:
            self.connection.execute(
                """
                UPDATE companies SET
                    last_seen_at = ?,
                    detail_level = ?,
                    data_json = ?
                WHERE id = ?
                """,
                (timestamp, values["detail_level"], serialized, company_id),
            )
            stats = UpsertStats(unchanged=1)
        else:
            self.connection.execute(
                """
                UPDATE companies SET
                    slug = :slug,
                    company_id = :company_id,
                    name = :name,
                    remote_updated_at = :remote_updated_at,
                    last_seen_at = :last_seen_at,
                    changed_at = :last_seen_at,
                    detail_level = :detail_level,
                    fingerprint = :fingerprint,
                    data_json = :data_json
                WHERE id = :id
                """,
                values,
            )
            stats = UpsertStats(updated=1)
        return stats

    def upsert_companies(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        detail_level: int = 1,
    ) -> UpsertStats:
        stats = UpsertStats()
        timestamp = utc_timestamp()
        for record in records:
            stats.add(self.upsert_company(record, detail_level=detail_level, seen_at=timestamp))
        self.connection.commit()
        return stats

    def get_job(self, identifier: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT data_json FROM jobs WHERE id = ? OR slug = ? LIMIT 1",
            (identifier, identifier),
        ).fetchone()
        return json.loads(row["data_json"]) if row else None

    def get_job_detail_level(self, identifier: str) -> int:
        row = self.connection.execute(
            "SELECT detail_level FROM jobs WHERE id = ? OR slug = ? LIMIT 1",
            (identifier, identifier),
        ).fetchone()
        return int(row["detail_level"]) if row else 0

    def job_needs_detail_refresh(
        self,
        identifier: str,
        remote_updated_at: str | None,
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT detail_level, detail_remote_updated_at
            FROM jobs WHERE id = ? OR slug = ? LIMIT 1
            """,
            (identifier, identifier),
        ).fetchone()
        if row is None or int(row["detail_level"]) < 2:
            return True
        return str(row["detail_remote_updated_at"] or "") != str(remote_updated_at or "")

    def get_company(self, identifier: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT data_json FROM companies
            WHERE id = ? OR slug = ? OR company_id = ?
            LIMIT 1
            """,
            (identifier, identifier, identifier),
        ).fetchone()
        return json.loads(row["data_json"]) if row else None

    def all_jobs(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT data_json FROM jobs ORDER BY remote_updated_at DESC, title"
        ).fetchall()
        return [json.loads(row["data_json"]) for row in rows]

    def all_companies(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT data_json FROM companies ORDER BY name").fetchall()
        return [json.loads(row["data_json"]) for row in rows]

    def find_jobs(
        self,
        *,
        filters: Mapping[str, Any] | None = None,
        max_jobs: int | None = None,
    ) -> list[dict[str, Any]]:
        matches = [job for job in self.all_jobs() if job_matches_filters(job, filters or {})]
        return matches[:max_jobs] if max_jobs is not None else matches

    def find_companies(
        self,
        query: str = "",
        *,
        max_companies: int | None = None,
    ) -> list[dict[str, Any]]:
        needle = query.strip().casefold()
        companies = self.all_companies()
        if needle:
            companies = [
                company
                for company in companies
                if needle
                in " ".join(
                    [
                        str(company.get("name") or ""),
                        str(company.get("company_id") or ""),
                        str(company.get("slug") or ""),
                        str(company.get("_id") or ""),
                    ]
                ).casefold()
            ]
        return companies[:max_companies] if max_companies is not None else companies


def job_matches_filters(job: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    company = _dict_value(job.get("company"))
    query = str(filters.get("query") or "").strip().casefold()
    if query:
        searchable = " ".join(
            [
                _text(job.get("title")),
                _text(job.get("description_text") or job.get("description")),
                _text(job.get("role")),
                _text(job.get("requirements_text")),
                _text(company.get("name")),
            ]
        ).casefold()
        if query not in searchable:
            return False

    value_map: dict[str, Any] = {
        "application_types": job.get("application_type"),
        "cities": job.get("city"),
        "companies": [
            company.get("_id"),
            company.get("company_id"),
            company.get("name"),
            company.get("slug"),
        ],
        "company_types": company.get("type"),
        "country_codes": job.get("country_code"),
        "employment_types": job.get("employment_type"),
        "hard_industry_skill_value_ids": job.get("hard_industry_skill_value_ids"),
        "industries": [
            job.get("industry"),
            company.get("industry"),
            *(company.get("industries") or []),
        ],
        "internship_programmes": [
            item.get("_id") if isinstance(item, dict) else item
            for item in (job.get("internship_programmes") or [])
        ],
        "programs": job.get("programs"),
        "related_work_terms": job.get("related_work_term"),
        "roles": job.get("role"),
        "soft_industry_skill_value_ids": job.get("soft_industry_skill_value_ids"),
        "work_arrangements": job.get("work_arrangement"),
        "work_terms": job.get("work_term"),
    }
    for key, actual in value_map.items():
        requested = _normalized_values(filters.get(key))
        if requested and not (_normalized_values(actual) & requested):
            return False

    for key, field in (
        ("is_applied", "user_has_applied"),
        ("is_drafted", "is_draft"),
        ("is_my_jobs", "is_bookmarked"),
        ("is_open_for_special_need", "is_open_for_special_need"),
    ):
        requested_bool = filters.get(key)
        if isinstance(requested_bool, bool) and job.get(field) is not requested_bool:
            return False

    if filters.get("is_qualified") is True and job.get("is_unqualified_student") is not False:
        return False

    posted_after = filters.get("posted_after")
    if posted_after:
        published_at = str(job.get("published_at") or "")
        try:
            threshold = datetime.fromisoformat(str(posted_after).replace("Z", "+00:00"))
            published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            if threshold.tzinfo is None:
                threshold = threshold.replace(tzinfo=UTC)
            if published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
        except ValueError:
            return False
        if published <= threshold:
            return False

    excluded_company_types = _normalized_values(filters.get("exclude_company_types"))
    if excluded_company_types & _normalized_values(company.get("type")):
        return False
    excluded_employment_types = _normalized_values(filters.get("exclude_employment_types"))
    return not (excluded_employment_types & _normalized_values(job.get("employment_type")))
