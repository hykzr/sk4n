from __future__ import annotations

import mimetypes
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

from tools import RequestTools


class CanvasAPIError(RuntimeError):
    """Raised when Canvas returns an unexpected response."""


def parse_next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        url_match = re.search(r"<([^>]+)>", part)
        rel_match = re.search(r'rel="([^"]+)"', part)
        if url_match and rel_match and rel_match.group(1) == "next":
            return url_match.group(1)
    return None


class CanvasClient:
    def __init__(
        self,
        base_url: str,
        site_name: str,
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.site_name = site_name
        self.timeout = timeout
        self._rt = RequestTools(site_name=site_name, timeout=timeout)

    def api_url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def get_json(
        self,
        path_or_url: str,
        params: dict[str, Any] | Iterable[tuple[str, Any]] | None = None,
    ) -> Any:
        url = path_or_url if path_or_url.startswith("http") else self.api_url(path_or_url)
        try:
            response = self._rt.get(url, params=params)
        except requests.HTTPError as exc:
            response = exc.response
            if response is not None and response.status_code in {401, 403}:
                raise CanvasAPIError(
                    "Canvas rejected the saved session. Rerun the sync command "
                    "and complete the browser login when prompted."
                ) from exc
            raise CanvasAPIError(f"Canvas request failed: {url}") from exc
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise CanvasAPIError(
                f"Canvas returned non-JSON content for {url}. "
                "The saved session may be expired; rerun the sync command and log in."
            )
        return response.json()

    def get_paginated(
        self,
        path: str,
        params: dict[str, Any] | Iterable[tuple[str, Any]] | None = None,
        max_pages: int = 50,
    ) -> list[Any]:
        url = self.api_url(path)
        items: list[Any] = []
        current_params = params
        for _ in range(max_pages):
            try:
                response = self._rt.get(url, params=current_params)
            except requests.HTTPError as exc:
                response = exc.response
                if response is not None and response.status_code in {401, 403}:
                    raise CanvasAPIError(
                        "Canvas rejected the saved session. Rerun the sync command "
                        "and complete the browser login when prompted."
                    ) from exc
                raise CanvasAPIError(f"Canvas request failed: {url}") from exc
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type.lower():
                raise CanvasAPIError(
                    f"Canvas returned non-JSON content for {url}. "
                    "The saved session may be expired; rerun the sync command and log in."
                )
            data = response.json()
            if not isinstance(data, list):
                raise CanvasAPIError(f"Expected a JSON list from {url}, got {type(data).__name__}.")
            items.extend(data)
            next_url = parse_next_link(response.headers.get("link"))
            if not next_url:
                break
            url = next_url
            current_params = None
        return items

    def profile(self) -> dict[str, Any]:
        data = self.get_json("/api/v1/users/self/profile")
        if not isinstance(data, dict) or "id" not in data:
            raise CanvasAPIError("Canvas profile response did not look like a logged-in user.")
        return data

    def active_courses(self) -> list[dict[str, Any]]:
        data = self.get_paginated(
            "/api/v1/courses",
            params=[
                ("enrollment_state", "active"),
                ("include[]", "term"),
                ("include[]", "course_image"),
                ("include[]", "sections"),
                ("per_page", "100"),
            ],
        )
        courses = [item for item in data if is_accessible_course(item)]
        for course in courses:
            course["_canvas_sync_source"] = "active_course"
            course["_canvas_sync_enrollment_state"] = "active"
        return courses

    def course_details(self, course_id: str | int) -> dict[str, Any]:
        data = self.get_json(
            f"/api/v1/courses/{course_id}",
            params=[
                ("include[]", "term"),
                ("include[]", "course_image"),
                ("include[]", "sections"),
            ],
        )
        if not isinstance(data, dict):
            raise CanvasAPIError(f"Course details response for {course_id} was not a JSON object.")
        return data

    def past_course_ids_from_courses_page(self) -> list[str]:
        soup = self._rt.get_soup(self.api_url("/courses"))
        ids: list[str] = []
        seen: set[str] = set()
        for link in soup.select('#past_enrollments_table a[href*="/courses/"]'):
            href = link.get("href", "")
            if not isinstance(href, str):
                continue
            match = re.search(r"/courses/(\d+)", href)
            if match and match.group(1) not in seen:
                course_id = match.group(1)
                seen.add(course_id)
                ids.append(course_id)
        return ids

    def past_courses(self) -> list[dict[str, Any]]:
        courses: list[dict[str, Any]] = []
        for course_id in self.past_course_ids_from_courses_page():
            try:
                course = self.course_details(course_id)
            except CanvasAPIError:
                continue
            if not is_accessible_course(course):
                continue
            course["_canvas_sync_source"] = "past_course"
            course["_canvas_sync_enrollment_state"] = "completed"
            courses.append(course)
        return courses

    def favorite_courses(self) -> list[dict[str, Any]]:
        data = self.get_paginated(
            "/api/v1/users/self/favorites/courses",
            params=[
                ("include[]", "term"),
                ("include[]", "course_image"),
                ("per_page", "100"),
            ],
        )
        return [item for item in data if isinstance(item, dict)]

    def dashboard_cards(self) -> list[dict[str, Any]]:
        data = self.get_json("/api/v1/dashboard/dashboard_cards")
        if not isinstance(data, list):
            raise CanvasAPIError("Dashboard cards response was not a JSON list.")
        return [item for item in data if isinstance(item, dict)]

    def course_tabs(self, course_id: str | int) -> list[dict[str, Any]]:
        data = self.get_paginated(
            f"/api/v1/courses/{course_id}/tabs",
            params=[("include[]", "external")],
        )
        return [item for item in data if isinstance(item, dict)]

    def download_cover_image(self, url: str | None, output_dir: Path) -> dict[str, Any]:
        remove_existing_cover_images(output_dir)
        if not url:
            return {"downloaded": False, "path": None, "content_type": None, "bytes": 0}

        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            response = self._rt.session.get(url, timeout=self.timeout, stream=True)
            response.raise_for_status()
        except requests.RequestException as exc:
            return {
                "downloaded": False,
                "path": None,
                "content_type": None,
                "bytes": 0,
                "error": str(exc),
            }

        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        extension = image_extension(content_type, url)
        if not extension:
            return {
                "downloaded": False,
                "path": None,
                "content_type": content_type or None,
                "bytes": 0,
                "error": "Cover URL did not return a supported image type.",
            }

        output_path = output_dir / f"cover_image{extension}"
        tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
        total = 0
        with tmp_path.open("wb") as file:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                file.write(chunk)
        tmp_path.replace(output_path)
        return {
            "downloaded": True,
            "path": output_path.as_posix(),
            "content_type": content_type or None,
            "bytes": total,
        }


def is_accessible_course(course: dict[str, Any]) -> bool:
    return (
        isinstance(course, dict)
        and course.get("id") is not None
        and bool(course.get("name"))
        and bool(course.get("course_code"))
        and course.get("workflow_state") == "available"
        and course.get("access_restricted_by_date") is not True
    )


def image_extension(content_type: str, url: str) -> str | None:
    content_type = content_type.lower()
    if content_type in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    if content_type == "image/png":
        return ".png"
    if content_type == "image/gif":
        return ".gif"
    if content_type == "image/webp":
        return ".webp"
    guessed = mimetypes.guess_extension(content_type)
    if guessed in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return ".jpg" if guessed == ".jpeg" else guessed
    path = url.split("?", 1)[0].lower()
    for extension in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        if path.endswith(extension):
            return ".jpg" if extension == ".jpeg" else extension
    return None


def remove_existing_cover_images(output_dir: Path) -> None:
    for path in output_dir.glob("cover_image.*"):
        if path.is_file():
            path.unlink()
