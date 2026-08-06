from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from agent_for_nus.errors import ExitCode

DEFAULT_API_BASE_URL = "https://nus-talentconnect.server.kinobi.asia"
DEFAULT_APP_BASE_URL = "https://nus-talentconnect.app.kinobi.asia"
DEFAULT_PAGE_SIZE = 100

JOB_FILTER_KEYS = {
    "application_types",
    "cities",
    "companies",
    "company_types",
    "country_codes",
    "employment_types",
    "exclude_company_types",
    "exclude_employment_types",
    "hard_industry_skill_value_ids",
    "include_expired_if_applied",
    "industries",
    "internship_programmes",
    "is_applied",
    "is_drafted",
    "is_my_jobs",
    "is_open_for_special_need",
    "programs",
    "related_work_terms",
    "roles",
    "soft_industry_skill_value_ids",
    "work_arrangements",
    "work_terms",
}

ProgressCallback = Callable[[int, int | None], None]


def encode_job_filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key, value in (filters or {}).items():
        if value is None or value == "" or value == []:
            continue
        if key not in JOB_FILTER_KEYS and key != "query":
            raise ValueError(f"Unsupported Kinobi job filter: {key}")
        if isinstance(value, bool):
            params[key] = str(value).lower()
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            params[key] = ",".join(str(item) for item in value if str(item))
        else:
            params[key] = value
    return params


class KinobiAPIError(RuntimeError):
    """Raised when Kinobi returns an unusable API response."""

    exit_code = ExitCode.REMOTE


class KinobiAuthError(KinobiAPIError):
    exit_code = ExitCode.AUTH


class KinobiTransportError(KinobiAPIError):
    exit_code = ExitCode.TRANSPORT


class KinobiHTTPError(KinobiAPIError):
    exit_code = ExitCode.REMOTE


class KinobiClient:
    """Deterministic client for Kinobi's public job and company APIs."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_API_BASE_URL,
        timeout: float = 30,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent",
            "nus-talent-connect-cli/1.0 (+https://nus-talentconnect.app.kinobi.asia)",
        )
        self.session.headers.setdefault("Accept", "application/json")
        self.detail_errors: dict[str, str] = {}
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def get_payload(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        allow_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        url = self._url(path)
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise KinobiTransportError(f"Kinobi request failed for {url}: {exc}") from exc
        if allow_statuses and response.status_code in allow_statuses:
            raise KinobiHTTPError(f"Kinobi returned HTTP {response.status_code} for {url}.")
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            if response.status_code == 401:
                message = (
                    f"Kinobi requires authentication for {url}. "
                    "Use `talent-connect auth login` first."
                )
            elif response.status_code == 404:
                message = f"Kinobi could not find {url}."
            else:
                message = f"Kinobi returned HTTP {response.status_code} for {url}."
            error_type = KinobiAuthError if response.status_code == 401 else KinobiHTTPError
            raise error_type(message) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise KinobiAPIError(f"Kinobi returned non-JSON content for {url}.") from exc
        if not isinstance(payload, dict):
            raise KinobiAPIError(
                f"Expected a JSON object from {url}, got {type(payload).__name__}."
            )
        return payload

    def iter_jobs(
        self,
        *,
        filters: Mapping[str, Any] | None = None,
        max_jobs: int | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        progress_callback: ProgressCallback | None = None,
    ) -> Iterator[dict[str, Any]]:
        if max_jobs is not None and max_jobs < 1:
            return
        if page_size < 1:
            raise ValueError("page_size must be at least 1.")
        params = encode_job_filters(filters)
        page = 1
        yielded = 0
        while True:
            page_params = dict(params)
            page_params.update({"page": page, "entries_per_page": page_size})
            payload = self.get_payload("/api/job/public", params=page_params)
            data = payload.get("data")
            pagination = payload.get("pagination")
            if not isinstance(data, list):
                raise KinobiAPIError("Kinobi job search response has no data list.")
            for record in data:
                if not isinstance(record, dict):
                    continue
                yield record
                yielded += 1
                if max_jobs is not None and yielded >= max_jobs:
                    if progress_callback:
                        progress_callback(yielded, max_jobs)
                    return
            if progress_callback:
                total = None
                if isinstance(pagination, dict):
                    raw_total = pagination.get("total") or pagination.get("total_entries")
                    if raw_total is not None:
                        total = int(raw_total)
                if max_jobs is not None:
                    total = min(total, max_jobs) if total is not None else max_jobs
                progress_callback(yielded, total)
            if not data:
                return
            if not isinstance(pagination, dict):
                return
            total_pages = int(pagination.get("total_pages") or page)
            if page >= total_pages:
                return
            page += 1

    def list_jobs(
        self,
        *,
        filters: Mapping[str, Any] | None = None,
        max_jobs: int | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        progress_callback: ProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        return list(
            self.iter_jobs(
                filters=filters,
                max_jobs=max_jobs,
                page_size=page_size,
                progress_callback=progress_callback,
            )
        )

    def get_job(self, identifier: str) -> dict[str, Any]:
        payload = self.get_payload(f"/api/job/{quote(identifier, safe='')}/public")
        data = payload.get("data")
        if not isinstance(data, dict) or not data.get("_id"):
            raise KinobiAPIError(f"Kinobi job response for {identifier!r} was invalid.")
        return data

    def get_jobs(
        self,
        identifiers: Sequence[str],
        *,
        max_workers: int = 6,
        progress_callback: ProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch public job details concurrently, retaining per-ID failures."""
        unique_identifiers = list(dict.fromkeys(identifiers))
        self.detail_errors = {}
        if not unique_identifiers:
            return []
        results_by_id: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.get_job, identifier): identifier
                for identifier in unique_identifiers
            }
            for future in as_completed(futures):
                identifier = futures[future]
                try:
                    results_by_id[identifier] = future.result()
                except Exception as exc:
                    self.detail_errors[identifier] = str(exc)
                if progress_callback:
                    completed = len(results_by_id) + len(self.detail_errors)
                    progress_callback(completed, len(unique_identifiers))
        return [
            results_by_id[identifier]
            for identifier in unique_identifiers
            if identifier in results_by_id
        ]

    def iter_companies(
        self,
        *,
        query: str | None = None,
        max_companies: int | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Iterator[dict[str, Any]]:
        if max_companies is not None and max_companies < 1:
            return
        page = 1
        yielded = 0
        while True:
            effective_page_size = page_size
            if max_companies is not None:
                effective_page_size = min(effective_page_size, max_companies - yielded)
            params: dict[str, Any] = {
                "page": page,
                "entries_per_page": effective_page_size,
            }
            if query:
                params["query"] = query
            payload = self.get_payload("/api/company", params=params)
            data = payload.get("data")
            pagination = payload.get("pagination")
            if not isinstance(data, list):
                raise KinobiAPIError("Kinobi company search response has no data list.")
            for record in data:
                if not isinstance(record, dict):
                    continue
                yield record
                yielded += 1
                if max_companies is not None and yielded >= max_companies:
                    return
            if not data or not isinstance(pagination, dict):
                return
            if page >= int(pagination.get("total_pages") or page):
                return
            page += 1

    def list_companies(
        self,
        *,
        query: str | None = None,
        max_companies: int | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> list[dict[str, Any]]:
        return list(
            self.iter_companies(
                query=query,
                max_companies=max_companies,
                page_size=page_size,
            )
        )

    def get_company(
        self,
        identifier: str,
        *,
        identifier_type: str | None = None,
    ) -> dict[str, Any]:
        quoted = quote(identifier, safe="")
        if identifier_type == "id":
            paths = [f"/api/company/by-id/{quoted}"]
        elif identifier_type == "company_id":
            paths = [f"/api/company/company-id/{quoted}"]
        elif identifier_type == "slug":
            paths = [f"/api/company/{quoted}"]
        else:
            paths = [
                f"/api/company/by-id/{quoted}",
                f"/api/company/company-id/{quoted}",
                f"/api/company/{quoted}",
            ]

        failures: list[str] = []
        for path in paths:
            try:
                payload = self.get_payload(path)
            except KinobiAPIError as exc:
                failures.append(str(exc))
                continue
            data = payload.get("data")
            if isinstance(data, dict) and data.get("_id"):
                return data
        joined = " ".join(failures[-2:])
        raise KinobiAPIError(f"Could not resolve Kinobi company {identifier!r}. {joined}".strip())


def utc_timestamp() -> str:
    """Return a stable UTC timestamp without importing storage internals."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
