from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import async_playwright

from tools.shared import load_session

from .auth import DEFAULT_SITE_NAME
from .client import (
    DEFAULT_APP_BASE_URL,
    DEFAULT_PAGE_SIZE,
    KinobiAPIError,
    ProgressCallback,
    encode_job_filters,
)

WORKFLOW_STATUSES = (
    "withdrawn",
    "interviewing",
    "declined",
    "offered",
    "accepted-offer",
    "job-history",
    "declined-offer",
)

APPLICATION_WORKFLOW_STATUSES = {
    "withdrawn": "withdrawn",
    "interviewing": "interviewing",
    "declined": "rejected",
}

OFFER_WORKFLOW_FILTERS = {
    "offered": {
        "statuses": "sent,terminated,expired",
        "responses": "pending",
    },
    "accepted-offer": {
        "statuses": "sent,expired",
        "responses": "accepted",
        "exclude_past": "true",
    },
    "job-history": {
        "statuses": "sent,expired",
        "responses": "accepted",
        "is_only_past": "true",
    },
    "declined-offer": {
        "statuses": "sent,expired",
        "responses": "rejected",
    },
}

HTTP_METHOD_PATTERN = re.compile(r"^[A-Z]+$")


def _without_keys(record: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    excluded = set(keys)
    return {key: value for key, value in record.items() if key not in excluded}


def workflow_record_to_job(
    record: Mapping[str, Any],
    *,
    workflow_status: str,
    source: str,
) -> dict[str, Any] | None:
    """Convert a profile application/offer record into a persistable job record."""
    nested_job = record.get("job")
    if not isinstance(nested_job, Mapping) or not nested_job.get("_id"):
        return None
    job = dict(nested_job)
    if not isinstance(job.get("company"), Mapping):
        job.pop("company", None)
    job["talent_connect_statuses"] = [workflow_status]
    if source == "application":
        application = _without_keys(record, "job", "user", "applicant")
        job["job_application"] = application
        job["job_application_id"] = application.get("_id")
        job["user_has_applied"] = application.get("status") != "draft"
        job["is_draft"] = application.get("status") == "draft"
    elif source == "offer":
        job["job_offer"] = _without_keys(record, "job", "applicant")
    else:
        raise ValueError(f"Unknown workflow source: {source}")
    return job


def _merge_workflow_jobs(
    existing: dict[str, Any],
    incoming: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if key == "talent_connect_statuses":
            old_statuses = existing.get(key)
            old = old_statuses if isinstance(old_statuses, list) else []
            new = value if isinstance(value, list) else []
            merged[key] = list(dict.fromkeys([*old, *new]))
        elif isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = {**dict(merged[key]), **dict(value)}
        else:
            merged[key] = value
    return merged


class AuthenticatedKinobiClient:
    """Use Kinobi's in-page Axios client without extracting browser tokens."""

    def __init__(
        self,
        *,
        app_base_url: str = DEFAULT_APP_BASE_URL,
        site_name: str = DEFAULT_SITE_NAME,
        timeout: float = 30,
    ) -> None:
        self.app_base_url = app_base_url.rstrip("/")
        self.site_name = site_name
        self.timeout = timeout
        self.detail_errors: dict[str, str] = {}

    def _storage_state(self) -> Any:
        session = load_session(self.site_name)
        storage_state = session.get("storage_state") if isinstance(session, dict) else None
        if not isinstance(storage_state, dict):
            raise KinobiAPIError("No saved TalentConnect login. Run `talent-connect auth login`.")
        return storage_state

    async def _open(self):

        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            storage_state=self._storage_state(),
            viewport={"width": 1440, "height": 950},
        )
        page = await context.new_page()
        page.set_default_timeout(int(self.timeout * 1000))
        await page.goto(
            f"{self.app_base_url}/jobs",
            wait_until="domcontentloaded",
            timeout=int(self.timeout * 1000),
        )
        await page.wait_for_function(
            "() => Boolean(window.$nuxt && window.$nuxt.$axios)",
            timeout=int(self.timeout * 1000),
        )
        return playwright, browser, page

    async def _get(self, page: Any, path: str) -> dict[str, Any]:
        payload = await self._request(page, "GET", path)
        if not isinstance(payload, dict):
            raise KinobiAPIError(f"Authenticated Kinobi response for {path} was not a JSON object.")
        return payload

    async def _get_many(self, page: Any, paths: Sequence[str]) -> list[dict[str, Any]]:
        results = await page.evaluate(
            """
            async (paths) => Promise.all(paths.map(async (path) => {
                try {
                    const payload = await window.$nuxt.$axios.$get(path);
                    return {path, ok: true, payload};
                } catch (error) {
                    return {
                        path,
                        ok: false,
                        status: error.response && error.response.status,
                        message: String(error.message || error)
                    };
                }
            }))
            """,
            list(paths),
        )
        if not isinstance(results, list):
            raise KinobiAPIError("Authenticated Kinobi batch response was invalid.")
        payloads: list[dict[str, Any]] = []
        for result in results:
            if not isinstance(result, dict) or not result.get("ok"):
                path = result.get("path") if isinstance(result, dict) else "unknown path"
                status = result.get("status") if isinstance(result, dict) else None
                message = result.get("message") if isinstance(result, dict) else "unknown error"
                if status == 401:
                    raise KinobiAPIError(
                        "The saved TalentConnect login expired. "
                        "Run `talent-connect auth login --refresh`."
                    )
                raise KinobiAPIError(
                    f"Authenticated Kinobi request failed for {path}"
                    f"{f' (HTTP {status})' if status else ''}: {message}"
                )
            payload = result.get("payload")
            if not isinstance(payload, dict):
                raise KinobiAPIError(
                    f"Authenticated Kinobi response for {result.get('path')} was not a JSON object."
                )
            payloads.append(payload)
        return payloads

    @staticmethod
    def _validate_request(method: str, path: str) -> tuple[str, str]:
        normalized_method = method.strip().upper()
        if not HTTP_METHOD_PATTERN.fullmatch(normalized_method):
            raise ValueError(f"Invalid HTTP method: {method!r}")
        return normalized_method, path

    async def _request(
        self,
        page: Any,
        method: str,
        path: str,
        data: Any = None,
    ) -> Any:
        method, path = self._validate_request(method, path)
        result = await page.evaluate(
            """
            async ({method, path, data}) => {
                try {
                    const payload = await window.$nuxt.$axios.$request({
                        method,
                        url: path,
                        data
                    });
                    return {ok: true, payload};
                } catch (error) {
                    return {
                        ok: false,
                        status: error.response && error.response.status,
                        message: String(error.message || error),
                        payload: error.response && error.response.data
                    };
                }
            }
            """,
            {"method": method, "path": path, "data": data},
        )
        if isinstance(result, dict) and result.get("ok"):
            return result.get("payload")

        status = result.get("status") if isinstance(result, dict) else None
        message = result.get("message") if isinstance(result, dict) else "unknown error"
        if status == 401:
            raise KinobiAPIError(
                "The saved TalentConnect login expired. Run `talent-connect auth login --refresh`."
            )
        response_payload = result.get("payload") if isinstance(result, dict) else None
        response_detail = ""
        if response_payload is not None:
            response_detail = f": {json.dumps(response_payload, ensure_ascii=False)}"
        raise KinobiAPIError(
            f"Authenticated Kinobi {method} request failed for {path}"
            f"{f' (HTTP {status})' if status else ''}: {message}{response_detail}"
        )

    async def _request_async(
        self,
        method: str,
        path: str,
        data: Any = None,
    ) -> Any:
        method, path = self._validate_request(method, path)
        playwright, browser, page = await self._open()
        try:
            return await self._request(page, method, path, data)
        finally:
            await browser.close()
            await playwright.stop()

    def request(
        self,
        method: str,
        path: str,
        data: Any = None,
    ) -> Any:
        """Send one request through Kinobi's authenticated in-page Axios client."""
        return asyncio.run(self._request_async(method, path, data))

    async def _list_jobs_async(
        self,
        *,
        filters: Mapping[str, Any] | None,
        max_jobs: int | None,
        page_size: int,
        recommended: bool,
        progress_callback: ProgressCallback | None,
    ) -> list[dict[str, Any]]:
        if max_jobs is not None and max_jobs < 1:
            return []
        params = encode_job_filters(filters)
        playwright, browser, page = await self._open()
        jobs: list[dict[str, Any]] = []
        try:
            endpoint = "/api/job/recommendation?" if recommended else "/api/job?"

            async def fetch_page(page_number: int) -> dict[str, Any]:
                page_params = dict(params)
                page_params.update({"page": page_number, "entries_per_page": page_size})
                return await self._get(page, endpoint + urlencode(page_params))

            async def append_payload(payload: dict[str, Any]) -> bool:
                data = payload.get("data")
                pagination = payload.get("pagination")
                if not isinstance(data, list):
                    raise KinobiAPIError("Authenticated Kinobi job response has no data list.")
                for record in data:
                    if isinstance(record, dict):
                        jobs.append(record)
                        if max_jobs is not None and len(jobs) >= max_jobs:
                            if progress_callback:
                                progress_callback(len(jobs), max_jobs)
                            return True
                if progress_callback:
                    total = None
                    if isinstance(pagination, dict):
                        raw_total = pagination.get("total") or pagination.get("total_entries")
                        if raw_total is not None:
                            total = int(raw_total)
                    if max_jobs is not None:
                        total = min(total, max_jobs) if total is not None else max_jobs
                    progress_callback(len(jobs), total)
                return not data

            first_payload = await fetch_page(1)
            if await append_payload(first_payload):
                return jobs
            pagination = first_payload.get("pagination")
            if not isinstance(pagination, dict):
                return jobs
            total_pages = int(pagination.get("total_pages") or 1)
            if max_jobs is not None:
                total_pages = min(total_pages, math.ceil(max_jobs / page_size))

            concurrent_pages = 4
            for first_page in range(2, total_pages + 1, concurrent_pages):
                page_numbers = range(
                    first_page,
                    min(first_page + concurrent_pages, total_pages + 1),
                )
                paths = []
                for page_number in page_numbers:
                    page_params = dict(params)
                    page_params.update({"page": page_number, "entries_per_page": page_size})
                    paths.append(endpoint + urlencode(page_params))
                payloads = await self._get_many(
                    page,
                    paths,
                )
                for payload in payloads:
                    if await append_payload(payload):
                        return jobs
            return jobs
        finally:
            await browser.close()
            await playwright.stop()

    def list_jobs(
        self,
        *,
        filters: Mapping[str, Any] | None = None,
        max_jobs: int | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        recommended: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        return asyncio.run(
            self._list_jobs_async(
                filters=filters,
                max_jobs=max_jobs,
                page_size=page_size,
                recommended=recommended,
                progress_callback=progress_callback,
            )
        )

    async def _list_workflow_jobs_async(
        self,
        *,
        statuses: Sequence[str],
        query: str | None,
        page_size: int,
        progress_callback: ProgressCallback | None,
    ) -> list[dict[str, Any]]:
        requested = list(dict.fromkeys(statuses))
        unsupported = [status for status in requested if status not in WORKFLOW_STATUSES]
        if unsupported:
            raise ValueError(f"Unsupported TalentConnect status: {unsupported[0]}")

        playwright, browser, page = await self._open()
        workflow_jobs: dict[str, dict[str, Any]] = {}
        self.detail_errors = {}
        try:

            async def fetch_all(
                endpoint: str,
                base_params: Mapping[str, Any],
            ) -> list[dict[str, Any]]:
                records: list[dict[str, Any]] = []
                page_number = 1
                while True:
                    params = dict(base_params)
                    params.update(
                        {
                            "page": page_number,
                            "entries_per_page": page_size,
                        }
                    )
                    payload = await self._get(page, endpoint + "?" + urlencode(params))
                    data = payload.get("data")
                    pagination = payload.get("pagination")
                    if not isinstance(data, list):
                        raise KinobiAPIError(
                            f"Authenticated Kinobi response for {endpoint} has no data list."
                        )
                    records.extend(record for record in data if isinstance(record, dict))
                    if not data or not isinstance(pagination, Mapping):
                        return records
                    total_pages = int(pagination.get("total_pages") or page_number)
                    if page_number >= total_pages:
                        return records
                    page_number += 1

            application_statuses = [
                APPLICATION_WORKFLOW_STATUSES[status]
                for status in requested
                if status in APPLICATION_WORKFLOW_STATUSES
            ]
            if application_statuses:
                params: dict[str, Any] = {
                    "statuses": ",".join(application_statuses),
                }
                if query:
                    params["query"] = query
                applications = await fetch_all(
                    "/api/job-application/by-user-and-job-paginated",
                    params,
                )
                reverse_status = {
                    value: key for key, value in APPLICATION_WORKFLOW_STATUSES.items()
                }
                for application in applications:
                    canonical = reverse_status.get(str(application.get("status") or ""))
                    if canonical is None:
                        continue
                    job = workflow_record_to_job(
                        application,
                        workflow_status=canonical,
                        source="application",
                    )
                    if job is None:
                        continue
                    job_id = str(job["_id"])
                    workflow_jobs[job_id] = _merge_workflow_jobs(
                        workflow_jobs.get(job_id, {}),
                        job,
                    )

            offer_statuses = [status for status in requested if status in OFFER_WORKFLOW_FILTERS]
            if offer_statuses:
                user_id = await page.evaluate(
                    """
                    () => {
                      const user = window.$nuxt?.$store?.state?.auth?.user || {};
                      return user._id || user.user_id || null;
                    }
                    """
                )
                if not user_id:
                    auth_payload = await self._get(page, "/api/auth/")
                    auth_user = auth_payload.get("data")
                    if isinstance(auth_user, Mapping):
                        user_id = auth_user.get("_id") or auth_user.get("user_id")
                if not user_id:
                    raise KinobiAPIError(
                        "The authenticated Kinobi profile has no user ID for offer filtering."
                    )
                for status in offer_statuses:
                    params = {
                        **OFFER_WORKFLOW_FILTERS[status],
                        "applicant_ids": str(user_id),
                    }
                    if query:
                        params["query"] = query
                    offers = await fetch_all("/api/job-offer/all-paginated", params)
                    for offer in offers:
                        job = workflow_record_to_job(
                            offer,
                            workflow_status=status,
                            source="offer",
                        )
                        if job is None:
                            continue
                        job_id = str(job["_id"])
                        workflow_jobs[job_id] = _merge_workflow_jobs(
                            workflow_jobs.get(job_id, {}),
                            job,
                        )

            identifiers = list(workflow_jobs)
            details_by_id: dict[str, dict[str, Any]] = {}
            batch_size = 8
            for offset in range(0, len(identifiers), batch_size):
                batch = identifiers[offset : offset + batch_size]
                paths = [f"/api/job/{identifier}" for identifier in batch]
                try:
                    payloads = await self._get_many(page, paths)
                except KinobiAPIError:
                    payloads = []
                    for identifier, path in zip(batch, paths, strict=True):
                        try:
                            payloads.append(await self._get(page, path))
                        except KinobiAPIError as exc:
                            self.detail_errors[identifier] = str(exc)
                            payloads.append({})
                for identifier, payload in zip(batch, payloads, strict=True):
                    data = payload.get("data")
                    if isinstance(data, dict) and data.get("_id"):
                        details_by_id[identifier] = data
                    elif identifier not in self.detail_errors:
                        self.detail_errors[identifier] = "invalid job detail response"
                if progress_callback:
                    progress_callback(
                        min(offset + len(batch), len(identifiers)),
                        len(identifiers),
                    )

            return [
                _merge_workflow_jobs(
                    details_by_id.get(identifier, {}),
                    workflow_jobs[identifier],
                )
                for identifier in identifiers
            ]
        finally:
            await browser.close()
            await playwright.stop()

    def list_workflow_jobs(
        self,
        *,
        statuses: Sequence[str],
        query: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        progress_callback: ProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        return asyncio.run(
            self._list_workflow_jobs_async(
                statuses=statuses,
                query=query,
                page_size=page_size,
                progress_callback=progress_callback,
            )
        )

    async def _get_job_async(self, identifier: str) -> dict[str, Any]:
        playwright, browser, page = await self._open()
        try:
            payload = await self._get(page, f"/api/job/{identifier}")
            data = payload.get("data")
            if not isinstance(data, dict) or not data.get("_id"):
                raise KinobiAPIError(
                    f"Authenticated Kinobi job response for {identifier!r} was invalid."
                )
            return data
        finally:
            await browser.close()
            await playwright.stop()

    def get_job(self, identifier: str) -> dict[str, Any]:
        return asyncio.run(self._get_job_async(identifier))

    async def _get_jobs_async(
        self,
        identifiers: Sequence[str],
        *,
        batch_size: int,
        progress_callback: ProgressCallback | None,
    ) -> list[dict[str, Any]]:
        unique_identifiers = list(dict.fromkeys(identifiers))
        self.detail_errors = {}
        if not unique_identifiers:
            return []
        playwright, browser, page = await self._open()
        details: list[dict[str, Any]] = []
        try:
            for offset in range(0, len(unique_identifiers), batch_size):
                batch = unique_identifiers[offset : offset + batch_size]
                results = await page.evaluate(
                    """
                    async (identifiers) => Promise.all(identifiers.map(async (identifier) => {
                        try {
                            const payload = await window.$nuxt.$axios.$get(
                                '/api/job/' + encodeURIComponent(identifier)
                            );
                            return {identifier, ok: true, data: payload.data || null};
                        } catch (error) {
                            return {
                                identifier,
                                ok: false,
                                status: error.response && error.response.status,
                                message: String(error.message || error)
                            };
                        }
                    }))
                    """,
                    batch,
                )
                if not isinstance(results, list):
                    raise KinobiAPIError(
                        "Authenticated Kinobi detail batch returned an invalid response."
                    )
                for result in results:
                    if not isinstance(result, dict):
                        continue
                    identifier = str(result.get("identifier") or "")
                    data = result.get("data")
                    if result.get("ok") and isinstance(data, dict) and data.get("_id"):
                        details.append(data)
                    else:
                        self.detail_errors[identifier] = str(
                            result.get("message") or f"HTTP {result.get('status')}"
                        )
                if progress_callback:
                    progress_callback(
                        min(offset + len(batch), len(unique_identifiers)),
                        len(unique_identifiers),
                    )
            return details
        finally:
            await browser.close()
            await playwright.stop()

    def get_jobs(
        self,
        identifiers: Sequence[str],
        *,
        batch_size: int = 8,
        progress_callback: ProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        return asyncio.run(
            self._get_jobs_async(
                identifiers,
                batch_size=batch_size,
                progress_callback=progress_callback,
            )
        )
