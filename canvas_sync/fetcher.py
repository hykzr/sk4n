from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from .auth import DEFAULT_LOGIN_WAIT_SECONDS, ensure_canvas_session
from .client import CanvasAPIError, CanvasClient
from .content import content_available, sync_course_content
from .models import (
    CourseRecord,
    infer_enrollment_academic_year,
    merge_course_records,
    now_utc_iso,
    student_record,
    unique_course_folder_names_by_term,
)
from .sync import SyncOptions, course_dir_for_record, load_existing_index, relative_index_course
from .utils import (
    COURSE_METADATA_FILE,
    DEFAULT_BASE_URL,
    DEFAULT_DATA_PATH,
    DEFAULT_SITE_NAME,
    INDEX_FILE,
    STUDENT_FILE,
    content_file_path,
    fingerprint,
    list_payload,
    normalize_existing_path,
    open_tab_ids,
    read_json,
    rel_path,
    resolve_relative_path,
    write_json,
)

SEMESTER_PATTERN = re.compile(r"^(?:AY)?(?P<year>\d{4})S(?P<semester>[1-4])$", re.IGNORECASE)
STUDY_SEMESTER_PATTERN = re.compile(r"^Y(?P<year>\d+)S(?P<semester>[1-4])$", re.IGNORECASE)

RESOURCE_ALIASES = {
    "announcement": "announcements",
    "announcements": "announcements",
    "assignment": "assignments",
    "assignments": "assignments",
    "discussion": "discussions",
    "discussions": "discussions",
    "file": "files",
    "files": "files",
    "home": "home",
    "module": "modules",
    "modules": "modules",
    "page": "pages",
    "pages": "pages",
    "people": "people",
    "person": "people",
    "quiz": "quizzes",
    "quizzes": "quizzes",
    "syllabus": "syllabus",
}
COURSE_RESOURCE_NAMES = frozenset(RESOURCE_ALIASES.values())

LOCAL_PATH_KEYS = {
    "body",
    "content",
    "description",
    "file",
    "local_path",
    "message",
    "metadata_path",
    "path",
    "quiz_path",
    "syllabus_body",
}
ALWAYS_LOCAL_PATH_KEYS = {"local_path", "metadata_path", "path", "quiz_path"}


def canonical_semester(value: str) -> str | None:
    match = SEMESTER_PATTERN.fullmatch(value.strip())
    if not match:
        return None
    return f"{match.group('year')}S{match.group('semester')}".upper()


def semester_sort_key(value: str | None) -> tuple[int, int, int]:
    canonical = canonical_semester(value or "")
    if not canonical:
        return (0, 0, 0)
    return (1, int(canonical[:2]), int(canonical[-1]))


def resolve_semester_filter(
    value: str | None, student: dict[str, Any], courses: list[dict[str, Any]]
) -> str | None:
    if value is None:
        return None
    candidate = value.strip().upper()
    if candidate == "LATEST":
        semesters = [
            canonical
            for course in courses
            if (canonical := canonical_semester(str(course.get("term_folder_name") or "")))
        ]
        if not semesters:
            raise ValueError("No academic-semester courses are available.")
        return max(semesters, key=semester_sort_key)
    canonical = canonical_semester(candidate)
    if canonical:
        return canonical
    study_match = STUDY_SEMESTER_PATTERN.fullmatch(candidate)
    if study_match:
        enrollment_year = str(student.get("enrollment_academic_year") or "")
        if not re.fullmatch(r"\d{4}", enrollment_year):
            raise ValueError(
                f"Cannot resolve {value!r}: the student's enrollment academic year is unavailable."
            )
        year_number = int(study_match.group("year"))
        if year_number < 1:
            raise ValueError("Study year must be at least Y1.")
        start = int(enrollment_year[:2]) + year_number - 1
        end = start + 1
        return f"{start % 100:02d}{end % 100:02d}S{study_match.group('semester')}"
    irregular_terms = sorted(
        {
            str(course.get("term_folder_name") or course.get("term_name") or "")
            for course in courses
            if course.get("term_folder_name")
            and not canonical_semester(str(course.get("term_folder_name")))
        },
        key=str.casefold,
    )
    for term in irregular_terms:
        if term.casefold() == candidate.casefold():
            return term
    raise ValueError(
        "Semester must be 'latest', AY2526S1/2526S1, a study semester such as Y3S1, "
        f"or an available non-academic term ({', '.join(irregular_terms) or 'none'})."
    )


def _without_synced_at(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result.pop("synced_at", None)
    return result


def absolutize_local_paths(value: Any, json_path: Path) -> Any:
    """Return a copy with existing local path fields resolved against their JSON file."""
    if isinstance(value, list):
        return [absolutize_local_paths(item, json_path) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, str) and (key in LOCAL_PATH_KEYS or key.endswith("_path")):
            path_text, separator, fragment = item.partition("#")
            candidate = Path(path_text)
            if not candidate.is_absolute():
                candidate = json_path.parent / candidate
            if candidate.exists() or key in ALWAYS_LOCAL_PATH_KEYS:
                resolved = candidate.resolve().as_posix()
                result[key] = f"{resolved}{separator}{fragment}" if separator else resolved
                continue
        result[key] = absolutize_local_paths(item, json_path)
    return result


class CanvasFetcher:
    """Authenticated, incremental Canvas fetcher with targeted public read methods."""

    def __init__(
        self,
        *,
        data_path: str | Path = DEFAULT_DATA_PATH,
        base_url: str = DEFAULT_BASE_URL,
        site_name: str = DEFAULT_SITE_NAME,
        timeout: int = 30,
        login_wait_seconds: int = DEFAULT_LOGIN_WAIT_SECONDS,
    ) -> None:
        self.data_path = Path(data_path).expanduser().resolve()
        self.base_url = base_url.rstrip("/")
        self.site_name = site_name
        self.timeout = timeout
        self.login_wait_seconds = login_wait_seconds
        self.client = CanvasClient(base_url=self.base_url, site_name=site_name, timeout=timeout)
        self._records: list[CourseRecord] = []
        self._student: dict[str, Any] = {}

    @property
    def index_path(self) -> Path:
        return self.data_path / INDEX_FILE

    @property
    def student_path(self) -> Path:
        return self.data_path / STUDENT_FILE

    def _ensure_session(self) -> bool:
        return ensure_canvas_session(
            base_url=self.base_url,
            site_name=self.site_name,
            login_wait_seconds=self.login_wait_seconds,
        )

    def student(self, *, refresh: bool = True) -> dict[str, Any]:
        if not refresh:
            cached = read_json(self.student_path)
            if cached:
                return absolutize_local_paths(cached, self.student_path)
            index = read_json(self.index_path)
            student = index.get("student") if index else None
            if isinstance(student, dict) and student:
                return student
            raise CanvasAPIError("No cached Canvas student information is available.")

        self._ensure_session()
        student = student_record(self.client.profile(), self.client.user())
        payload = {"synced_at": now_utc_iso(), **student}
        self.data_path.mkdir(parents=True, exist_ok=True)
        write_json(self.student_path, payload)
        self._student = payload
        return payload

    def _fetch_records(self) -> list[CourseRecord]:
        courses = self.client.active_courses() + self.client.past_courses()
        records = merge_course_records(
            courses,
            self.client.favorite_courses(),
            self.client.dashboard_cards(),
        )
        self._records = records
        return records

    def _index_entry(
        self,
        record: CourseRecord,
        *,
        course_dir: Path,
        term_folder_name: str,
        folder_name: str,
        metadata: dict[str, Any] | None,
        previous: dict[str, Any] | None,
    ) -> dict[str, Any]:
        metadata_value: dict[str, Any] = metadata or {}
        course_value = metadata_value.get("course")
        course: dict[str, Any] = course_value if isinstance(course_value, dict) else {}
        cover_value = metadata_value.get("cover_image")
        cover: dict[str, Any] = cover_value if isinstance(cover_value, dict) else {}
        metadata_path = course_dir / COURSE_METADATA_FILE
        cover_path = resolve_relative_path(metadata_path, cover.get("path"))
        sections = metadata_value.get("available_sections")
        if not isinstance(sections, list):
            sections = []
        previous_value: dict[str, Any] = previous or {}
        term_value = course.get("term")
        term: dict[str, Any] = term_value if isinstance(term_value, dict) else {}
        enrollment_roles = course.get("enrollment_roles")
        if not isinstance(enrollment_roles, list):
            enrolled_sections = course.get("enrolled_sections")
            enrollment_roles = [
                section.get("enrollment_role")
                for section in enrolled_sections or []
                if isinstance(section, dict) and section.get("enrollment_role")
            ]
        enrollment_roles = list(
            dict.fromkeys(
                [str(role) for role in enrollment_roles]
                + [str(role) for role in record.enrollment_roles]
            )
        )
        content_value = metadata_value.get("content")
        content: dict[str, Any] = content_value if isinstance(content_value, dict) else {}
        return {
            "id": record.id,
            "name": course.get("name") or record.name,
            "course_code": course.get("course_code") or record.course_code,
            "enrollment_state": course.get("enrollment_state") or record.enrollment_state,
            "enrollment_roles": enrollment_roles,
            "term_name": record.term_name or term.get("name"),
            "term_folder_name": term_folder_name,
            "folder_name": folder_name,
            "metadata_path": metadata_path.resolve().as_posix(),
            "cover_image_present": cover.get("present"),
            "cover_image_downloaded": cover.get("downloaded"),
            "cover_image_path": cover_path.resolve().as_posix() if cover_path else None,
            "available_section_count": len(sections),
            "available_sections": [
                {key: section.get(key) for key in ("id", "label", "type")}
                for section in sections
                if isinstance(section, dict)
            ],
            "content": content.get("sections") or previous_value.get("content") or {},
        }

    def courses(self, *, semester: str | None = None, refresh: bool = True) -> list[dict[str, Any]]:
        if not refresh:
            index = read_json(self.index_path)
            courses = index.get("courses") if index else None
            if not isinstance(courses, list):
                raise CanvasAPIError("No cached Canvas course index is available.")
            student_value = index.get("student") if index else None
            student: dict[str, Any] = student_value if isinstance(student_value, dict) else {}
            result = [
                self._absolute_index_course(item) for item in courses if isinstance(item, dict)
            ]
            selected = resolve_semester_filter(semester, student, result)
            return self._filter_semester(result, selected)

        session_refreshed = self._ensure_session()
        profile = self.client.profile()
        user = self.client.user()
        student = {"synced_at": now_utc_iso(), **student_record(profile, user)}
        records = self._fetch_records()
        self.data_path.mkdir(parents=True, exist_ok=True)
        write_json(self.student_path, student)
        self._student = student

        existing = load_existing_index(self.data_path)
        folder_names = unique_course_folder_names_by_term(records)
        index_courses: list[dict[str, Any]] = []
        for record in records:
            course_dir, term_folder_name, folder_name = course_dir_for_record(
                root=self.data_path,
                record=record,
                folder_names=folder_names,
                existing_index=existing,
            )
            metadata = read_json(course_dir / COURSE_METADATA_FILE)
            entry = self._index_entry(
                record,
                course_dir=course_dir,
                term_folder_name=term_folder_name,
                folder_name=folder_name,
                metadata=metadata,
                previous=existing.get(record.id),
            )
            index_courses.append(relative_index_course(entry, self.index_path))

        index_payload = {
            "synced_at": now_utc_iso(),
            "base_url": self.base_url,
            "site_name": self.site_name,
            "session_refreshed": session_refreshed,
            "student": student,
            "course_count": len(index_courses),
            "courses": index_courses,
        }
        write_json(self.index_path, index_payload)
        absolute = [self._absolute_index_course(item) for item in index_courses]
        selected = resolve_semester_filter(semester, student, absolute)
        return self._filter_semester(absolute, selected)

    def _filter_semester(
        self, courses: list[dict[str, Any]], semester: str | None
    ) -> list[dict[str, Any]]:
        if semester is None:
            return sorted(courses, key=self._course_sort_key, reverse=True)
        return sorted(
            [
                course
                for course in courses
                if (
                    canonical_semester(str(course.get("term_folder_name") or "")) == semester
                    or str(course.get("term_folder_name") or "").casefold() == semester.casefold()
                )
            ],
            key=self._course_sort_key,
            reverse=True,
        )

    def _absolute_index_course(self, course: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(course)
        for key in ("metadata_path", "cover_image_path"):
            value = result.get(key)
            if not isinstance(value, str) or not value:
                continue
            path = Path(value)
            if not path.is_absolute():
                path = self.index_path.parent / path
            result[key] = path.resolve().as_posix()
        metadata_path = result.get("metadata_path")
        if isinstance(metadata_path, str):
            metadata = Path(metadata_path)
            result["course_path"] = metadata.parent.resolve().as_posix()
            if not result.get("enrollment_roles"):
                cached_metadata = read_json(metadata)
                cached_course = cached_metadata.get("course") if cached_metadata else None
                if isinstance(cached_course, dict):
                    roles = cached_course.get("enrollment_roles")
                    if not isinstance(roles, list):
                        sections = cached_course.get("enrolled_sections")
                        roles = [
                            section.get("enrollment_role")
                            for section in sections or []
                            if isinstance(section, dict) and section.get("enrollment_role")
                        ]
                    result["enrollment_roles"] = list(
                        dict.fromkeys(str(role) for role in roles or [])
                    )
        result.pop("content", None)
        return result

    @staticmethod
    def _course_sort_key(course: dict[str, Any]) -> tuple[tuple[int, int, int], str, str]:
        return (
            semester_sort_key(str(course.get("term_folder_name") or "")),
            str(course.get("course_code") or "").casefold(),
            str(course.get("id") or ""),
        )

    def _resolve_course(self, selector: str, courses: list[dict[str, Any]]) -> dict[str, Any]:
        normalized = selector.casefold()
        id_matches = [
            course for course in courses if str(course.get("id") or "").casefold() == normalized
        ]
        if id_matches:
            return id_matches[0]
        matches = [
            course
            for course in courses
            if str(course.get("course_code") or "").casefold() == normalized
        ]
        if not matches:
            raise CanvasAPIError(f"No accessible course matched {selector!r}.")
        if len(matches) > 1:
            lines = [f"Course code {selector!r} matched {len(matches)} courses:"]
            for course in sorted(matches, key=self._course_sort_key, reverse=True):
                roles = ", ".join(str(role) for role in course.get("enrollment_roles") or [])
                lines.append(
                    "- "
                    f"{course.get('term_folder_name') or 'Unknown Term'} | "
                    f"ID {course.get('id')} | {roles or 'unknown role'} | "
                    f"{course.get('name') or course.get('course_code')}"
                )
            lines.append("Use --semester SEM to narrow the matches, or use a numeric course ID.")
            raise CanvasAPIError("\n".join(lines))
        return matches[0]

    def _record_for_id(self, course_id: str) -> CourseRecord:
        for record in self._records:
            if record.id == course_id:
                return record
        raise CanvasAPIError(f"Course {course_id} was not present in the refreshed Canvas catalog.")

    def _refresh_course_metadata(
        self,
        *,
        record: CourseRecord,
        course_dir: Path,
        term_folder_name: str,
        folder_name: str,
        force: bool,
    ) -> tuple[dict[str, Any], str]:
        course_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = course_dir / COURSE_METADATA_FILE
        existing = read_json(metadata_path)
        tabs = self.client.course_tabs(record.id)
        synced_at = now_utc_iso()
        metadata = record.to_json_record(
            base_url=self.base_url,
            term_folder_name=term_folder_name,
            folder_name=folder_name,
            tabs=tabs,
            synced_at=synced_at,
        )
        cover_metadata_value = metadata.get("cover_image")
        cover_metadata: dict[str, Any] = (
            cover_metadata_value if isinstance(cover_metadata_value, dict) else {}
        )
        metadata["cover_image"] = cover_metadata

        old_cover_value = existing.get("cover_image") if existing else None
        old_cover: dict[str, Any] = old_cover_value if isinstance(old_cover_value, dict) else {}
        old_cover_path = resolve_relative_path(metadata_path, old_cover.get("path"))
        cover_unchanged = (
            not force
            and old_cover.get("url") == cover_metadata.get("url")
            and (not old_cover.get("url") or bool(old_cover_path and old_cover_path.exists()))
        )
        if cover_unchanged:
            cover_metadata = copy.deepcopy(old_cover)
            metadata["cover_image"] = cover_metadata
            cover_metadata["path"] = normalize_existing_path(
                json_path=metadata_path,
                target_path=cover_metadata.get("path"),
                course_dir=course_dir,
            )
        else:
            cover_result = self.client.download_cover_image(cover_metadata.get("url"), course_dir)
            if cover_result.get("path"):
                cover_result["path"] = rel_path(metadata_path, Path(cover_result["path"]))
            cover_metadata.update(cover_result)

        if existing and isinstance(existing.get("content"), dict):
            metadata["content"] = copy.deepcopy(existing["content"])
        if existing and _without_synced_at(existing) == _without_synced_at(metadata):
            return existing, "unchanged"
        write_json(metadata_path, metadata)
        return metadata, "updated" if existing else "created"

    def _prepare_course(
        self,
        selector: str,
        *,
        refresh: bool,
        force: bool,
        semester: str | None = None,
        content_type: str | None = None,
    ) -> tuple[dict[str, Any], Path, dict[str, Any] | None]:
        courses = self.courses(semester=semester, refresh=refresh)
        selected = self._resolve_course(selector, courses)
        metadata_path = Path(str(selected["metadata_path"]))
        course_dir = metadata_path.parent
        if not refresh:
            metadata = read_json(metadata_path)
            if not metadata:
                raise CanvasAPIError(f"Course {selector!r} is not present in the local cache.")
            return selected, course_dir, metadata

        record = self._record_for_id(str(selected["id"]))
        existing_index = load_existing_index(self.data_path)
        folder_names = unique_course_folder_names_by_term(self._records)
        course_dir, term_folder_name, folder_name = course_dir_for_record(
            root=self.data_path,
            record=record,
            folder_names=folder_names,
            existing_index=existing_index,
        )
        metadata, _ = self._refresh_course_metadata(
            record=record,
            course_dir=course_dir,
            term_folder_name=term_folder_name,
            folder_name=folder_name,
            force=force,
        )

        if content_type:
            synced_at = now_utc_iso()
            options = SyncOptions(refresh_content=force)
            summary = sync_course_content(
                client=self.client,
                course_id=record.id,
                course_dir=course_dir,
                tabs=metadata.get("all_tabs") or [],
                synced_at=synced_at,
                options=options,
                course_metadata=metadata,
                content_types=[content_type],
            )
            old_sections = metadata.get("content", {}).get("sections")
            merged_sections = copy.deepcopy(old_sections) if isinstance(old_sections, dict) else {}
            merged_sections.update(summary["sections"])
            metadata["content"] = {"synced_at": synced_at, "sections": merged_sections}
            if any(
                item.get("status") in {"created", "updated", "error"}
                for item in summary["sections"].values()
            ):
                metadata["synced_at"] = synced_at
                write_json(course_dir / COURSE_METADATA_FILE, metadata)

        entry = self._index_entry(
            record,
            course_dir=course_dir,
            term_folder_name=term_folder_name,
            folder_name=folder_name,
            metadata=metadata,
            previous=existing_index.get(record.id),
        )
        index = read_json(self.index_path) or {}
        index_courses_value = index.get("courses")
        index_courses: list[Any] = (
            index_courses_value if isinstance(index_courses_value, list) else []
        )
        index["courses"] = [
            relative_index_course(entry, self.index_path)
            if str(item.get("id")) == record.id
            else item
            for item in index_courses
            if isinstance(item, dict)
        ]
        index["course_count"] = len(index["courses"])
        write_json(self.index_path, index)
        return (
            self._absolute_index_course(relative_index_course(entry, self.index_path)),
            course_dir,
            metadata,
        )

    def course(
        self,
        selector: str,
        *,
        semester: str | None = None,
        refresh: bool = True,
        force: bool = False,
    ) -> dict[str, Any]:
        selected, course_dir, metadata = self._prepare_course(
            selector, refresh=refresh, force=force, semester=semester
        )
        assert metadata is not None
        result = {
            key: copy.deepcopy(metadata.get(key))
            for key in (
                "synced_at",
                "base_url",
                "term_folder_name",
                "folder_name",
                "sources",
                "course",
                "cover_image",
                "dashboard",
                "available_sections",
            )
            if key in metadata
        }
        metadata_path = course_dir / COURSE_METADATA_FILE
        result = absolutize_local_paths(result, metadata_path)
        course_value = result.get("course")
        if isinstance(course_value, dict) and not course_value.get("enrollment_roles"):
            sections = course_value.get("enrolled_sections")
            course_value["enrollment_roles"] = list(
                dict.fromkeys(
                    str(section.get("enrollment_role"))
                    for section in sections or []
                    if isinstance(section, dict) and section.get("enrollment_role")
                )
            )
        if isinstance(course_value, dict):
            course_value["enrollment_roles"] = list(
                dict.fromkeys(
                    [str(role) for role in course_value.get("enrollment_roles") or []]
                    + [str(role) for role in selected.get("enrollment_roles") or []]
                )
            )
        result["local_path"] = metadata_path.resolve().as_posix()
        result["course_path"] = course_dir.resolve().as_posix()
        return result

    def course_path(
        self,
        selector: str,
        *,
        semester: str | None = None,
        refresh: bool = True,
        force: bool = False,
    ) -> Path:
        _, course_dir, _ = self._prepare_course(
            selector, refresh=refresh, force=force, semester=semester
        )
        return course_dir.resolve()

    def content(
        self,
        selector: str,
        resource: str,
        item_selector: str = "list",
        *,
        semester: str | None = None,
        refresh: bool = True,
        force: bool = False,
    ) -> Any:
        resource_name = RESOURCE_ALIASES.get(resource.casefold())
        if not resource_name:
            raise ValueError(
                f"Unknown course resource {resource!r}. Available: {', '.join(sorted(RESOURCE_ALIASES))}."
            )
        if resource_name == "home":
            if item_selector.casefold() != "list":
                raise ValueError("`canvas course CODE home` does not accept an item selector.")
            return self.home(
                selector,
                semester=semester,
                refresh=refresh,
                force=force,
            )
        content_type = "assignments" if resource_name == "quizzes" else resource_name
        _, course_dir, metadata = self._prepare_course(
            selector,
            refresh=refresh,
            force=force,
            semester=semester,
            content_type=content_type,
        )
        all_tabs = metadata.get("all_tabs") if metadata else None
        content = metadata.get("content") if metadata else None
        sections = content.get("sections") if isinstance(content, dict) else None
        section = sections.get(content_type) if isinstance(sections, dict) else None
        section_closed = isinstance(section, dict) and section.get("status") == "closed"
        section_error = (
            str(section.get("error"))
            if isinstance(section, dict) and section.get("status") == "error"
            else None
        )
        course_value = metadata.get("course") if isinstance(metadata, dict) else None
        course = course_value if isinstance(course_value, dict) else {}
        default_view = str(course.get("default_view") or "")
        if isinstance(all_tabs, list):
            accessible_tabs = [
                tab for tab in all_tabs if isinstance(tab, dict) and tab.get("hidden") is not True
            ]
            if section_closed or not content_available(
                content_type,
                open_tab_ids(accessible_tabs),
                default_view=default_view,
            ):
                accessible_sections = list(
                    dict.fromkeys(
                        str(tab.get("label") or tab.get("id"))
                        for tab in accessible_tabs
                        if tab.get("label") or tab.get("id")
                    )
                )
                queryable_sections = list(
                    dict.fromkeys(
                        str(tab.get("id")).casefold()
                        for tab in accessible_tabs
                        if str(tab.get("id") or "").casefold() in COURSE_RESOURCE_NAMES
                    )
                )
                raise CanvasAPIError(
                    f"The {resource_name!r} section is not available for course {selector!r}. "
                    f"Accessible Canvas sections: {', '.join(accessible_sections) or 'none'}. "
                    "Queryable with `canvas course`: "
                    f"{', '.join(queryable_sections) or 'none'}."
                )
        if section_error:
            raise CanvasAPIError(
                f"Canvas could not refresh {resource_name} for course {selector!r}: {section_error}"
            )
        json_path = content_file_path(course_dir, content_type).resolve()
        if item_selector.casefold() == "path":
            if not json_path.exists():
                raise CanvasAPIError(f"{resource_name} is not available for course {selector!r}.")
            return json_path
        payload = read_json(json_path)
        if not payload:
            raise CanvasAPIError(f"{resource_name} is not available in the local course cache.")
        items = self._content_items(payload, resource_name)
        prepared = [self._prepare_item(item, json_path) for item in items]
        if item_selector.casefold() == "list":
            return prepared
        matched = self._find_item(prepared, item_selector)
        if matched is None and resource_name == "syllabus":
            matched = prepared[0] if prepared else None
        if matched is None:
            raise CanvasAPIError(f"No {resource_name} item matched {item_selector!r}.")
        detail_path = matched.get("path")
        if isinstance(detail_path, str) and Path(detail_path).suffix.casefold() == ".json":
            detail_json_path = Path(detail_path)
            detail = read_json(detail_json_path)
            if detail:
                result = absolutize_local_paths(detail, detail_json_path)
                result["local_path"] = detail_json_path.resolve().as_posix()
                return result
        return matched

    def home(
        self,
        selector: str,
        *,
        semester: str | None = None,
        refresh: bool = True,
        force: bool = False,
    ) -> dict[str, Any]:
        selected, course_dir, metadata = self._prepare_course(
            selector,
            refresh=refresh,
            force=force,
            semester=semester,
        )
        assert metadata is not None
        course_value = metadata.get("course")
        course = course_value if isinstance(course_value, dict) else {}
        default_view = str(course.get("default_view") or "feed").casefold()
        mapped_resource = {
            "assignments": "assignments",
            "modules": "modules",
            "syllabus": "syllabus",
            "wiki": "pages",
        }.get(default_view)
        if mapped_resource:
            items = self.content(
                selector,
                mapped_resource,
                semester=semester,
                refresh=refresh,
                force=force,
            )
            if default_view == "wiki":
                front_pages = [item for item in items if item.get("front_page") is True]
                items = front_pages or items
            return {
                "course_id": str(selected["id"]),
                "default_view": default_view,
                "resource": mapped_resource,
                "html_url": course.get("html_url"),
                "count": len(items),
                "items": items,
            }

        if default_view != "feed":
            raise CanvasAPIError(
                f"Course {selector!r} uses unsupported Canvas default view {default_view!r}."
            )
        json_path = (course_dir / "home.json").resolve()
        if refresh:
            items = self.client.course_activity_stream(str(selected["id"]))
            fingerprint_value = fingerprint(items)
            existing = read_json(json_path)
            if force or existing is None or existing.get("fingerprint") != fingerprint_value:
                write_json(
                    json_path,
                    list_payload(
                        course_id=str(selected["id"]),
                        synced_at=now_utc_iso(),
                        items=items,
                        fingerprint_value=fingerprint_value,
                    ),
                )
        payload = read_json(json_path)
        if not payload:
            raise CanvasAPIError(f"Home feed is not available in the local cache for {selector!r}.")
        prepared = [
            self._prepare_item(item, json_path)
            for item in payload.get("items") or []
            if isinstance(item, dict)
        ]
        return {
            "course_id": str(selected["id"]),
            "default_view": default_view,
            "resource": "activity_stream",
            "html_url": course.get("html_url"),
            "count": len(prepared),
            "items": prepared,
            "local_path": json_path.as_posix(),
        }

    @staticmethod
    def _content_items(payload: dict[str, Any], resource: str) -> list[dict[str, Any]]:
        if resource == "files":
            items = payload.get("files")
        elif resource == "syllabus":
            return [payload]
        else:
            items = payload.get("items")
        result = [item for item in items or [] if isinstance(item, dict)]
        if resource == "quizzes":
            return [
                item
                for item in result
                if item.get("kind") == "quiz" or item.get("quiz_id") is not None
            ]
        if resource == "assignments":
            return [item for item in result if item.get("kind") != "quiz"]
        return result

    @staticmethod
    def _prepare_item(item: dict[str, Any], json_path: Path) -> dict[str, Any]:
        result = absolutize_local_paths(item, json_path)
        for key in ("path", "body", "message", "content"):
            value = result.get(key)
            if isinstance(value, str) and Path(value.partition("#")[0]).exists():
                result.setdefault("local_path", value.partition("#")[0])
                break
        result.setdefault("local_path", json_path.resolve().as_posix())
        return result

    @staticmethod
    def _find_item(items: list[dict[str, Any]], selector: str) -> dict[str, Any] | None:
        normalized = selector.casefold()
        keys = ("id", "key", "url", "page_id", "quiz_id", "content_id")
        for item in items:
            if any(str(item.get(key) or "").casefold() == normalized for key in keys):
                return item
        return None


__all__ = [
    "RESOURCE_ALIASES",
    "CanvasFetcher",
    "absolutize_local_paths",
    "canonical_semester",
    "infer_enrollment_academic_year",
    "resolve_semester_filter",
]
