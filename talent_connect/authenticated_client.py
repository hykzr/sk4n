from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

from tools._shared import load_session

from .auth import DEFAULT_SITE_NAME
from .client import (
    DEFAULT_APP_BASE_URL,
    DEFAULT_PAGE_SIZE,
    KinobiAPIError,
    ProgressCallback,
    encode_job_filters,
)


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
        from playwright.async_api import async_playwright

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
        result = await page.evaluate(
            """
            async (path) => {
                try {
                    const payload = await window.$nuxt.$axios.$get(path);
                    return {ok: true, payload};
                } catch (error) {
                    return {
                        ok: false,
                        status: error.response && error.response.status,
                        message: String(error.message || error)
                    };
                }
            }
            """,
            path,
        )
        if not isinstance(result, dict) or not result.get("ok"):
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
