from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from agent_for_nus.paths import nusmods_data_dir

DEFAULT_API_BASE_URL = "https://api.nusmods.com/v2"
DEFAULT_DISQUS_URL = "https://disqus.com/embed/comments/"
DEFAULT_CALENDAR_URL = (
    "https://raw.githubusercontent.com/nusmodifications/nusmods/master/"
    "packages/nusmods-academic-calendar/academic-calendar.json"
)
DEFAULT_HOLIDAYS_URL = (
    "https://raw.githubusercontent.com/nusmodifications/nusmods/master/"
    "website/src/data/holidays.json"
)
DEFAULT_DATA_DIR = nusmods_data_dir()
DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60


class NUSModsAPIError(RuntimeError):
    """Raised when NUSMods or its public comments feed returns unusable data."""


def normalize_academic_year(value: str) -> tuple[str, str]:
    """Return an academic year as (display form, API path form)."""
    candidate = value.strip().replace("-", "/")
    parts = candidate.split("/")
    if len(parts) != 2 or not all(part.isdigit() and len(part) == 4 for part in parts):
        raise ValueError("Academic year must look like 2026/2027 or 2026-2027.")
    start, end = (int(part) for part in parts)
    if end != start + 1:
        raise ValueError("Academic year must contain consecutive years.")
    return f"{start:04d}/{end:04d}", f"{start:04d}-{end:04d}"


class NUSModsClient:
    """Read-only client for NUSMods' public static API and review feed."""

    def __init__(
        self,
        *,
        academic_year: str,
        base_url: str = DEFAULT_API_BASE_URL,
        data_dir: Path = DEFAULT_DATA_DIR,
        timeout: float = 30,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        refresh: bool = False,
        cache_only: bool = False,
        session: requests.Session | None = None,
    ) -> None:
        if refresh and cache_only:
            raise ValueError("refresh and cache_only are mutually exclusive.")
        self.academic_year, self.api_academic_year = normalize_academic_year(academic_year)
        self.base_url = base_url.rstrip("/")
        self.data_dir = Path(data_dir)
        self.cache_dir = self.data_dir / "cache"
        self.timeout = timeout
        self.cache_ttl_seconds = cache_ttl_seconds
        self.refresh = refresh
        self.cache_only = cache_only
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent",
            "agent-for-nus-nusmods-cli/1.0 (+https://nusmods.com)",
        )
        self.session.headers.setdefault("Accept", "application/json, text/html;q=0.9")
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

    def _cache_path(self, key: str) -> Path:
        safe_key = key.replace("/", "_").replace("\\", "_")
        return self.cache_dir / f"{safe_key}.json"

    def _read_cache(self, key: str, *, allow_stale: bool = False) -> Any | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        if not allow_stale and time.time() - path.stat().st_mtime > self.cache_ttl_seconds:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, key: str, value: Any) -> None:
        path = self._cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _cached_before_request(self, key: str, expected_type: type) -> Any | None:
        if self.cache_only:
            cached = self._read_cache(key, allow_stale=True)
            if isinstance(cached, expected_type):
                return cached
            raise NUSModsAPIError(
                f"No usable cached data for {key!r}. "
                "Remove --no-refresh to allow a network request."
            )
        if self.refresh:
            return None
        cached = self._read_cache(key)
        return cached if isinstance(cached, expected_type) else None

    def _stale_fallback(self, key: str, expected_type: type) -> Any | None:
        if self.refresh or self.cache_only:
            return None
        cached = self._read_cache(key, allow_stale=True)
        return cached if isinstance(cached, expected_type) else None

    def _get_json_url(
        self,
        url: str,
        *,
        cache_key: str | None = None,
        expected_type: type = dict,
    ) -> Any:
        if cache_key:
            cached = self._cached_before_request(cache_key, expected_type)
            if cached is not None:
                return cached
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            if cache_key:
                stale = self._stale_fallback(cache_key, expected_type)
                if stale is not None:
                    return stale
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 404:
                message = f"NUSMods could not find {url}."
            elif status:
                message = f"NUSMods returned HTTP {status} for {url}."
            else:
                message = f"NUSMods request failed for {url}: {exc}"
            raise NUSModsAPIError(message) from exc
        if not isinstance(payload, expected_type):
            raise NUSModsAPIError(
                f"Expected {expected_type.__name__} from {url}, got {type(payload).__name__}."
            )
        if cache_key:
            self._write_cache(cache_key, payload)
        return payload

    def _api_url(self, path: str) -> str:
        return f"{self.base_url}/{self.api_academic_year}/{path.lstrip('/')}"

    def list_modules(self) -> list[dict[str, Any]]:
        payload = self._get_json_url(
            self._api_url("moduleInformation.json"),
            cache_key=f"{self.api_academic_year}-moduleInformation",
            expected_type=list,
        )
        return [item for item in payload if isinstance(item, dict)]

    def get_module(self, module_code: str) -> dict[str, Any]:
        code = module_code.strip().upper()
        if not code:
            raise ValueError("Course code cannot be empty.")
        payload = self._get_json_url(
            self._api_url(f"modules/{quote(code, safe='')}.json"),
            cache_key=f"{self.api_academic_year}-module-{code}",
            expected_type=dict,
        )
        if str(payload.get("moduleCode", "")).upper() != code:
            raise NUSModsAPIError(f"NUSMods returned invalid course data for {code}.")
        return payload

    def get_academic_calendar(self) -> dict[str, Any]:
        return self._get_json_url(
            DEFAULT_CALENDAR_URL,
            cache_key="academic-calendar",
            expected_type=dict,
        )

    def get_holidays(self) -> list[str]:
        payload = self._get_json_url(
            DEFAULT_HOLIDAYS_URL,
            cache_key="holidays",
            expected_type=list,
        )
        return [item for item in payload if isinstance(item, str)]

    def get_comments(self, module_code: str, title: str) -> dict[str, Any]:
        """Fetch the public reviews initially embedded by Disqus.

        Disqus may paginate unusually large threads. The returned ``hasMore`` flag
        makes that explicit instead of silently claiming the first page is complete.
        """
        code = module_code.strip().upper()
        cache_key = f"comments-{code}"
        cached = self._cached_before_request(cache_key, dict)
        if cached is not None:
            return cached
        page_title = f"{code} {title}".strip()
        params = {
            "base": "default",
            "f": "nusmods-prod",
            "t_i": code,
            "t_u": f"https://nusmods.com/courses/{code}/reviews",
            "t_d": page_title,
            "t_t": page_title,
            "s_o": "default",
        }
        url = f"{DEFAULT_DISQUS_URL}?{urlencode(params)}"
        try:
            response = self.session.get(
                url,
                timeout=self.timeout,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            stale = self._stale_fallback(cache_key, dict)
            if stale is not None:
                return stale
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status else ""
            raise NUSModsAPIError(f"Could not fetch NUSMods reviews{suffix}.") from exc

        soup = BeautifulSoup(response.text, "html.parser")
        thread_script = soup.find("script", id="disqus-threadData")
        if thread_script is None or not thread_script.string:
            stale = self._stale_fallback(cache_key, dict)
            if stale is not None:
                return stale
            raise NUSModsAPIError("Disqus returned no public review payload.")
        try:
            thread_data = json.loads(thread_script.string)
        except json.JSONDecodeError as exc:
            stale = self._stale_fallback(cache_key, dict)
            if stale is not None:
                return stale
            raise NUSModsAPIError("Disqus returned malformed review data.") from exc

        response_data = thread_data.get("response")
        cursor = thread_data.get("cursor")
        if not isinstance(response_data, Mapping):
            response_data = {}
        if not isinstance(cursor, Mapping):
            cursor = {}
        raw_posts = response_data.get("posts")
        if not isinstance(raw_posts, list):
            raw_posts = []

        comments: list[dict[str, Any]] = []
        for post in raw_posts:
            if not isinstance(post, Mapping):
                continue
            author = post.get("author")
            author_name = author.get("name") if isinstance(author, Mapping) else None
            message_html = str(post.get("message") or "")
            message = BeautifulSoup(message_html, "html.parser").get_text("\n", strip=True)
            comments.append(
                {
                    "id": post.get("id"),
                    "parent": post.get("parent"),
                    "createdAt": post.get("createdAt"),
                    "author": author_name,
                    "likes": post.get("likes", 0),
                    "dislikes": post.get("dislikes", 0),
                    "message": message,
                }
            )

        result = {
            "count": int(cursor.get("total") or len(comments)),
            "returned": len(comments),
            "hasMore": bool(cursor.get("hasNext")),
            "comments": comments,
        }
        self._write_cache(cache_key, result)
        return result
