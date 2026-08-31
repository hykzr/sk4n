from __future__ import annotations

import hashlib
import mimetypes
import re
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import requests

from agent_for_nus.errors import ExitCode
from tools import RequestTools
from tools.shared import atomic_output_path


class CanvasAPIError(RuntimeError):
    """Raised when Canvas returns an unexpected response."""

    exit_code = ExitCode.REMOTE


class CanvasAuthError(CanvasAPIError):
    exit_code = ExitCode.AUTH


class CanvasTransportError(CanvasAPIError):
    exit_code = ExitCode.TRANSPORT


class CanvasHTTPError(CanvasAPIError):
    exit_code = ExitCode.REMOTE


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

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int | None]:
        parsed = urlsplit(url)
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"Canvas URL must use HTTP(S) and include a host: {url!r}")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Canvas URLs must not include user credentials.")
        port = parsed.port
        if port is None:
            port = 443 if scheme == "https" else 80
        return scheme, parsed.hostname.casefold(), port

    def resolve_url(self, path_or_url: str) -> str:
        """Resolve a Canvas path while refusing to send saved cookies cross-origin."""
        parsed = urlsplit(path_or_url)
        url = path_or_url if parsed.scheme else urljoin(self.base_url + "/", path_or_url)
        if self._origin(url) != self._origin(self.base_url):
            raise ValueError(
                "Authenticated Canvas requests must target the configured Canvas origin "
                f"{self.base_url!r}; rejected {path_or_url!r}."
            )
        return url

    def request_error(self, url: str, exc: requests.HTTPError) -> CanvasAPIError:
        response = exc.response
        status = response.status_code if response is not None else None
        if status == 401:
            return CanvasAuthError(
                "Canvas rejected the saved session. Run `canvas auth login` and try again."
            )
        if status == 403:
            return CanvasHTTPError(
                f"Canvas denied access to {url} (HTTP 403). The item may be "
                "locked, hidden, or unavailable for this enrollment."
            )
        if status:
            return CanvasHTTPError(f"Canvas request failed for {url} (HTTP {status}).")
        return CanvasTransportError(f"Canvas request failed: {url}")

    def get_response(
        self,
        path_or_url: str,
        params: dict[str, Any] | Iterable[tuple[str, Any]] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        url = self.resolve_url(path_or_url)
        try:
            return self._rt.get(url, params=params, **kwargs)
        except requests.HTTPError as exc:
            raise self.request_error(url, exc) from exc
        except requests.RequestException as exc:
            raise CanvasTransportError(f"Canvas request failed: {url}") from exc

    def get_json(
        self,
        path_or_url: str,
        params: dict[str, Any] | Iterable[tuple[str, Any]] | None = None,
    ) -> Any:
        url = self.resolve_url(path_or_url)
        response = self.get_response(url, params=params)
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise CanvasAPIError(
                f"Canvas returned non-JSON content for {url}. "
                "The saved session may be expired; rerun the sync command and log in."
            )
        return response.json()

    def request(
        self,
        method: str,
        path_or_url: str,
        data: Any = None,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Send a direct authenticated request and decode JSON responses when possible."""
        url = self.resolve_url(path_or_url)
        try:
            response = self._rt.session.request(
                method.upper(),
                url,
                params=params,
                json=data,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise self.request_error(url, exc) from exc
        except requests.RequestException as exc:
            raise CanvasTransportError(f"Canvas request failed: {url}") from exc
        if response.status_code == 204 or not response.content:
            return None
        content_type = response.headers.get("content-type", "").casefold()
        if "json" in content_type:
            return response.json()
        return response.text

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
            response = self.get_response(url, params=current_params)
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
            url = self.resolve_url(next_url)
            current_params = None
        return items

    def profile(self) -> dict[str, Any]:
        data = self.get_json("/api/v1/users/self/profile")
        if not isinstance(data, dict) or "id" not in data:
            raise CanvasAPIError("Canvas profile response did not look like a logged-in user.")
        return data

    def user(self) -> dict[str, Any]:
        data = self.get_json("/api/v1/users/self")
        if not isinstance(data, dict) or "id" not in data:
            raise CanvasAPIError("Canvas user response did not look like a logged-in user.")
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

    def calendar_events(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        event_type: str = "event",
    ) -> list[dict[str, Any]]:
        params: list[tuple[str, Any]] = [
            ("type", event_type),
            ("include[]", "context"),
            ("per_page", "100"),
        ]
        if start is not None:
            params.append(("start_date", start.isoformat()))
        if end is not None:
            params.append(("end_date", end.isoformat()))
        data = self.get_paginated("/api/v1/calendar_events", params=params)
        return [item for item in data if isinstance(item, dict)]

    def todo(self) -> list[dict[str, Any]]:
        data = self.get_paginated(
            "/api/v1/users/self/todo",
            params=[("per_page", "100")],
        )
        return [item for item in data if isinstance(item, dict)]

    def upcoming_events(self) -> list[dict[str, Any]]:
        data = self.get_paginated(
            "/api/v1/users/self/upcoming_events",
            params=[("per_page", "100")],
        )
        return [item for item in data if isinstance(item, dict)]

    def course_tabs(self, course_id: str | int) -> list[dict[str, Any]]:
        data = self.get_paginated(
            f"/api/v1/courses/{course_id}/tabs",
            params=[("include[]", "external")],
        )
        return [item for item in data if isinstance(item, dict)]

    def course_announcements(self, course_id: str | int) -> list[dict[str, Any]]:
        return self.course_discussion_topics(course_id, only_announcements=True)

    def course_discussion_topics(
        self,
        course_id: str | int,
        *,
        only_announcements: bool | None = None,
    ) -> list[dict[str, Any]]:
        params: list[tuple[str, Any]] = [("per_page", "100")]
        if only_announcements is True:
            params.insert(0, ("only_announcements", "true"))
        data = self.get_paginated(f"/api/v1/courses/{course_id}/discussion_topics", params=params)
        return [item for item in data if isinstance(item, dict)]

    def course_discussion_view(self, course_id: str | int, topic_id: str | int) -> dict[str, Any]:
        data = self.get_json(f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/view")
        if not isinstance(data, dict):
            raise CanvasAPIError(
                f"Discussion view response for {course_id}/{topic_id} was not a JSON object."
            )
        return data

    def course_discussions(
        self,
        course_id: str | int,
        *,
        include_entries: bool = True,
    ) -> list[dict[str, Any]]:
        data = self.course_discussion_topics(course_id, only_announcements=False)
        topics = [
            dict(item)
            for item in data
            if isinstance(item, dict) and item.get("is_announcement") is not True
        ]
        if include_entries:
            for topic in topics:
                topic_id = topic.get("id")
                if topic_id is None:
                    continue
                try:
                    view = self.course_discussion_view(course_id, topic_id)
                except CanvasAPIError as exc:
                    topic["_canvas_sync_view_error"] = str(exc)
                    continue
                topic["view"] = view
        return topics

    def course_people(self, course_id: str | int) -> list[dict[str, Any]]:
        data = self.get_paginated(
            f"/api/v1/courses/{course_id}/users",
            params=[
                ("include[]", "enrollments"),
                ("per_page", "100"),
            ],
        )
        return [item for item in data if isinstance(item, dict)]

    def user_groups(self) -> list[dict[str, Any]]:
        data = self.get_paginated(
            "/api/v1/users/self/groups",
            params=[
                ("context_type", "Course"),
                ("per_page", "100"),
            ],
        )
        return [item for item in data if isinstance(item, dict)]

    def course_groups(self, course_id: str | int) -> list[dict[str, Any]]:
        data = self.get_paginated(
            f"/api/v1/courses/{course_id}/groups",
            params=[
                ("include[]", "users"),
                ("include[]", "group_category"),
                ("include[]", "permissions"),
                ("include_inactive_users", "true"),
                ("section_restricted", "true"),
                ("per_page", "100"),
            ],
        )
        memberships = {
            str(item.get("id"))
            for item in self.user_groups()
            if str(item.get("course_id")) == str(course_id) and item.get("id") is not None
        }
        groups: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            group = dict(item)
            group_id = group.get("id")
            is_member = group_id is not None and str(group_id) in memberships
            group["is_current_user_member"] = is_member
            if is_member and not group.get("html_url"):
                group["html_url"] = f"{self.base_url}/groups/{group_id}"
            groups.append(group)
        return groups

    def course_page_summaries(self, course_id: str | int) -> list[dict[str, Any]]:
        data = self.get_paginated(
            f"/api/v1/courses/{course_id}/pages",
            params=[("per_page", "100")],
        )
        return [item for item in data if isinstance(item, dict)]

    def course_page_detail(self, course_id: str | int, page_url: str) -> dict[str, Any]:
        encoded_page_url = quote(str(page_url), safe="")
        data = self.get_json(f"/api/v1/courses/{course_id}/pages/{encoded_page_url}")
        if not isinstance(data, dict):
            raise CanvasAPIError(
                f"Page detail response for {course_id}/{page_url} was not a JSON object."
            )
        return data

    def course_pages(self, course_id: str | int) -> list[dict[str, Any]]:
        pages = self.course_page_summaries(course_id)
        detailed_pages: list[dict[str, Any]] = []
        for page in pages:
            page_url = page.get("url")
            if not page_url:
                detailed_pages.append(dict(page))
                continue
            try:
                detail = self.course_page_detail(course_id, str(page_url))
            except CanvasAPIError as exc:
                fallback = dict(page)
                fallback["_canvas_sync_detail_error"] = str(exc)
                detailed_pages.append(fallback)
                continue
            detailed_pages.append(detail)
        return detailed_pages

    def course_syllabus(self, course_id: str | int) -> dict[str, Any]:
        data = self.get_json(
            f"/api/v1/courses/{course_id}",
            params=[
                ("include[]", "syllabus_body"),
                ("include[]", "term"),
            ],
        )
        if not isinstance(data, dict):
            raise CanvasAPIError(f"Syllabus response for {course_id} was not a JSON object.")
        return data

    def course_modules(self, course_id: str | int) -> list[dict[str, Any]]:
        data = self.get_paginated(
            f"/api/v1/courses/{course_id}/modules",
            params=[
                ("include[]", "items"),
                ("per_page", "100"),
            ],
        )
        return [item for item in data if isinstance(item, dict)]

    def course_activity_stream(self, course_id: str | int) -> list[dict[str, Any]]:
        data = self.get_paginated(
            f"/api/v1/courses/{course_id}/activity_stream",
            params=[("per_page", "100")],
        )
        return [item for item in data if isinstance(item, dict)]

    def course_assignments(self, course_id: str | int) -> list[dict[str, Any]]:
        data = self.get_paginated(
            f"/api/v1/courses/{course_id}/assignments",
            params=[
                ("include[]", "submission"),
                ("include[]", "all_dates"),
                ("include[]", "overrides"),
                ("include[]", "score_statistics"),
                ("per_page", "100"),
            ],
        )
        return [item for item in data if isinstance(item, dict)]

    def course_assignment_detail(
        self, course_id: str | int, assignment_id: str | int
    ) -> dict[str, Any]:
        data = self.get_json(
            f"/api/v1/courses/{course_id}/assignments/{assignment_id}",
            params=[
                ("include[]", "submission"),
                ("include[]", "all_dates"),
                ("include[]", "overrides"),
                ("include[]", "score_statistics"),
                ("include[]", "rubric"),
            ],
        )
        if not isinstance(data, dict):
            raise CanvasAPIError(
                f"Assignment detail response for {course_id}/{assignment_id} was not a JSON object."
            )
        return data

    def assignment_self_submission(
        self, course_id: str | int, assignment_id: str | int
    ) -> dict[str, Any]:
        data = self.get_json(
            f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/self",
            params=[
                ("include[]", "submission_history"),
                ("include[]", "submission_comments"),
                ("include[]", "rubric_assessment"),
                ("include[]", "full_rubric_assessment"),
                ("include[]", "visibility"),
            ],
        )
        if not isinstance(data, dict):
            raise CanvasAPIError(
                f"Self submission response for {course_id}/{assignment_id} was not a JSON object."
            )
        return data

    def course_quizzes(self, course_id: str | int) -> list[dict[str, Any]]:
        data = self.get_paginated(
            f"/api/v1/courses/{course_id}/quizzes",
            params=[("per_page", "100")],
        )
        return [item for item in data if isinstance(item, dict)]

    def course_quiz_detail(self, course_id: str | int, quiz_id: str | int) -> dict[str, Any]:
        data = self.get_json(f"/api/v1/courses/{course_id}/quizzes/{quiz_id}")
        if not isinstance(data, dict):
            raise CanvasAPIError(
                f"Quiz detail response for {course_id}/{quiz_id} was not a JSON object."
            )
        return data

    def course_quiz_questions(
        self, course_id: str | int, quiz_id: str | int
    ) -> list[dict[str, Any]]:
        data = self.get_paginated(
            f"/api/v1/courses/{course_id}/quizzes/{quiz_id}/questions",
            params=[("per_page", "100")],
        )
        return [item for item in data if isinstance(item, dict)]

    def course_quiz_self_submission(
        self, course_id: str | int, quiz_id: str | int
    ) -> dict[str, Any]:
        data = self.get_json(f"/api/v1/courses/{course_id}/quizzes/{quiz_id}/submissions/self")
        if not isinstance(data, dict):
            raise CanvasAPIError(
                f"Quiz self-submission response for {course_id}/{quiz_id} was not a JSON object."
            )
        return data

    def course_folders(self, course_id: str | int) -> list[dict[str, Any]]:
        data = self.get_paginated(
            f"/api/v1/courses/{course_id}/folders",
            params=[("per_page", "100")],
        )
        return [item for item in data if isinstance(item, dict)]

    def course_files(self, course_id: str | int) -> list[dict[str, Any]]:
        data = self.get_paginated(
            f"/api/v1/courses/{course_id}/files",
            params=[("per_page", "100")],
        )
        return [item for item in data if isinstance(item, dict)]

    def file_details(self, file_id: str | int) -> dict[str, Any]:
        data = self.get_json(f"/api/v1/files/{file_id}")
        if not isinstance(data, dict):
            raise CanvasAPIError(f"File detail response for {file_id} was not a JSON object.")
        return data

    def download_file(self, url: str, output_path: Path) -> dict[str, Any]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        total = 0
        try:
            response = self._rt.session.get(url, timeout=self.timeout, stream=True)
            response.raise_for_status()
            with (
                atomic_output_path(output_path) as temporary_path,
                temporary_path.open("wb") as file,
            ):
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    digest.update(chunk)
                    file.write(chunk)
        except requests.RequestException as exc:
            raise CanvasAPIError(f"Canvas file download failed: {url}") from exc
        return {
            "path": output_path.as_posix(),
            "bytes": total,
            "sha256": digest.hexdigest(),
            "content_type": response.headers.get("content-type"),
        }

    def download_cover_image(self, url: str | None, output_dir: Path) -> dict[str, Any]:
        if not url:
            remove_existing_cover_images(output_dir)
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
        total = 0
        with (
            atomic_output_path(output_path) as temporary_path,
            temporary_path.open("wb") as file,
        ):
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                file.write(chunk)
        remove_existing_cover_images(output_dir, keep=output_path)
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


def remove_existing_cover_images(output_dir: Path, *, keep: Path | None = None) -> None:
    for extension in (".jpg", ".png", ".gif", ".webp"):
        path = output_dir / f"cover_image{extension}"
        if path.is_file() and path != keep:
            path.unlink()
