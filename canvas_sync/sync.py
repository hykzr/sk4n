from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

try:
    from .auth import DEFAULT_LOGIN_WAIT_SECONDS, ensure_canvas_session
    from .client import CanvasAPIError, CanvasClient
    from .content import sync_course_content
    from .models import (
        CourseRecord,
        merge_course_records,
        now_utc_iso,
        path_for_course,
        unique_course_folder_names_by_term,
    )
    from .utils import (
        COURSE_METADATA_FILE,
        DEFAULT_BASE_URL,
        DEFAULT_DATA_PATH,
        DEFAULT_SITE_NAME,
        INDEX_FILE,
        normalize_existing_path,
        read_json,
        rel_path,
        write_json,
    )
except ImportError:
    from auth import DEFAULT_LOGIN_WAIT_SECONDS, ensure_canvas_session
    from client import CanvasAPIError, CanvasClient
    from content import sync_course_content
    from models import (
        CourseRecord,
        merge_course_records,
        now_utc_iso,
        path_for_course,
        unique_course_folder_names_by_term,
    )
    from utils import (
        COURSE_METADATA_FILE,
        DEFAULT_BASE_URL,
        DEFAULT_DATA_PATH,
        DEFAULT_SITE_NAME,
        INDEX_FILE,
        normalize_existing_path,
        read_json,
        rel_path,
        write_json,
    )


@dataclass(frozen=True)
class CanvasSyncResult:
    data_path: Path
    course_count: int
    index_path: Path
    course_paths: list[Path]
    session_refreshed: bool
    updates: list[dict[str, Any]]


@dataclass(frozen=True)
class SyncOptions:
    refresh_course: bool = False
    refresh_people: bool = False
    refresh_content: bool = False
    refresh_announcements: bool = False
    refresh_discussions: bool = False
    refresh_pages: bool = False
    refresh_syllabus: bool = False
    refresh_modules: bool = False
    refresh_assignments: bool = False
    refresh_files: bool = False
    skip_announcements: bool = False
    skip_discussions: bool = False
    skip_people: bool = False
    skip_pages: bool = False
    skip_syllabus: bool = False
    skip_modules: bool = False
    skip_assignments: bool = False
    skip_files: bool = False

    def force_content(self, content_type: str) -> bool:
        if self.refresh_content:
            return True
        if content_type == "people":
            return self.refresh_people
        if content_type == "announcements":
            return self.refresh_announcements
        if content_type == "discussions":
            return self.refresh_discussions
        if content_type == "pages":
            return self.refresh_pages
        if content_type == "syllabus":
            return self.refresh_syllabus or self.refresh_course
        if content_type == "modules":
            return self.refresh_modules
        if content_type == "assignments":
            return self.refresh_assignments
        if content_type == "files":
            return self.refresh_files
        return False

    def skip_content(self, content_type: str) -> bool:
        if content_type == "announcements":
            return self.skip_announcements
        if content_type == "discussions":
            return self.skip_discussions
        if content_type == "people":
            return self.skip_people
        if content_type == "pages":
            return self.skip_pages
        if content_type == "syllabus":
            return self.skip_syllabus
        if content_type == "modules":
            return self.skip_modules
        if content_type == "assignments":
            return self.skip_assignments
        if content_type == "files":
            return self.skip_files
        return False


def build_course_metadata(
    *,
    client: CanvasClient,
    record: CourseRecord,
    base_url: str,
    term_folder_name: str,
    folder_name: str,
    course_dir: Path,
    synced_at: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    tabs = client.course_tabs(record.id)
    metadata = record.to_json_record(
        base_url=base_url,
        term_folder_name=term_folder_name,
        folder_name=folder_name,
        tabs=tabs,
        synced_at=synced_at,
    )
    cover_result = client.download_cover_image(
        metadata["cover_image"]["url"], course_dir
    )
    course_json_path = course_dir / COURSE_METADATA_FILE
    if cover_result.get("path"):
        cover_path = Path(str(cover_result["path"]))
        cover_result["path"] = rel_path(course_json_path, cover_path)
    metadata["cover_image"].update(cover_result)
    return metadata, tabs, True


def load_or_build_course_metadata(
    *,
    client: CanvasClient,
    record: CourseRecord,
    base_url: str,
    term_folder_name: str,
    folder_name: str,
    course_dir: Path,
    synced_at: str,
    refresh_course: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    course_json_path = course_dir / COURSE_METADATA_FILE
    existing = read_json(course_json_path)
    if existing and not refresh_course:
        metadata = copy.deepcopy(existing)
        metadata["cover_image"] = (
            metadata.get("cover_image")
            if isinstance(metadata.get("cover_image"), dict)
            else {}
        ) or {}
        metadata["cover_image"]["path"] = normalize_existing_path(
            json_path=course_json_path,
            target_path=metadata["cover_image"].get("path"),
            course_dir=course_dir,
        )
        tabs = (
            metadata.get("all_tabs")
            if isinstance(metadata.get("all_tabs"), list)
            else []
        )
        if not tabs:
            tabs = client.course_tabs(record.id)
            metadata["all_tabs"] = tabs
            metadata["available_sections"] = [
                tab
                for tab in tabs
                if isinstance(tab, dict) and tab.get("hidden") is not True
            ]
        return metadata, [tab for tab in tabs if isinstance(tab, dict)], "unchanged"

    metadata, tabs, _ = build_course_metadata(
        client=client,
        record=record,
        base_url=base_url,
        term_folder_name=term_folder_name,
        folder_name=folder_name,
        course_dir=course_dir,
        synced_at=synced_at,
    )
    return metadata, tabs, "updated" if existing else "created"


def load_existing_index(root: Path) -> dict[str, dict[str, Any]]:
    index = read_json(root / INDEX_FILE)
    courses = index.get("courses") if index else None
    if not isinstance(courses, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for course in courses:
        if isinstance(course, dict) and course.get("id") is not None:
            result[str(course["id"])] = course
    return result


def course_dir_for_record(
    *,
    root: Path,
    record: CourseRecord,
    folder_names: dict[str, str],
    existing_index: dict[str, dict[str, Any]],
) -> tuple[Path, str, str]:
    existing = existing_index.get(record.id)
    if existing and existing.get("term_folder_name") and existing.get("folder_name"):
        term_folder_name = str(existing["term_folder_name"])
        folder_name = str(existing["folder_name"])
    else:
        term_folder_name = record.term_folder_base_name()
        folder_name = folder_names[record.id]
    return (
        path_for_course(root, term_folder_name, folder_name),
        term_folder_name,
        folder_name,
    )


def filter_records(
    records: list[CourseRecord], selectors: list[str]
) -> list[CourseRecord]:
    if not selectors:
        return records
    normalized = {selector.casefold() for selector in selectors}
    matched: list[CourseRecord] = []
    for record in records:
        values = {
            record.id.casefold(),
            (record.course_code or "").casefold(),
        }
        if values & normalized:
            matched.append(record)
    found_ids = {record.id.casefold() for record in matched}
    found_codes = {(record.course_code or "").casefold() for record in matched}
    missing = [
        selector
        for selector in selectors
        if selector.casefold() not in found_ids | found_codes
    ]
    if missing:
        raise CanvasAPIError(f"No accessible course matched: {', '.join(missing)}")
    return matched


def relative_index_course(course: dict[str, Any], index_path: Path) -> dict[str, Any]:
    result = copy.deepcopy(course)
    for key in ("metadata_path", "cover_image_path"):
        value = result.get(key)
        if isinstance(value, str) and value:
            path = Path(value)
            result[key] = (
                rel_path(index_path, path)
                if path.is_absolute()
                else value.replace("\\", "/")
            )
    return result


def render_update_table(console: Console, updates: list[dict[str, Any]]) -> None:
    table = Table(title="Canvas sync updates")
    table.add_column("Course")
    table.add_column("Course JSON")
    table.add_column("Updated")
    table.add_column("Unchanged")
    table.add_column("Skipped")
    table.add_column("Errors")
    for update in updates:
        sections = update.get("content", {})
        updated = [
            key
            for key, value in sections.items()
            if isinstance(value, dict) and value.get("status") in {"created", "updated"}
        ]
        unchanged = [
            key
            for key, value in sections.items()
            if isinstance(value, dict) and value.get("status") == "unchanged"
        ]
        skipped = [
            key
            for key, value in sections.items()
            if isinstance(value, dict) and value.get("status") in {"skipped", "closed"}
        ]
        errors = [
            key
            for key, value in sections.items()
            if isinstance(value, dict) and value.get("status") == "error"
        ]
        table.add_row(
            str(update.get("course_code") or update.get("id")),
            str(update.get("course_status")),
            ", ".join(updated) or "-",
            ", ".join(unchanged) or "-",
            ", ".join(skipped) or "-",
            ", ".join(errors) or "-",
        )
    console.print(table)


def sync_canvas(
    *,
    data_path: str | Path = DEFAULT_DATA_PATH,
    base_url: str = DEFAULT_BASE_URL,
    site_name: str = DEFAULT_SITE_NAME,
    max_courses: int | None = None,
    login_wait_seconds: int = DEFAULT_LOGIN_WAIT_SECONDS,
    course_selectors: list[str] | None = None,
    refresh_course: bool = False,
    refresh_people: bool = False,
    refresh_content: bool = False,
    refresh_announcements: bool = False,
    refresh_discussions: bool = False,
    refresh_pages: bool = False,
    refresh_syllabus: bool = False,
    refresh_modules: bool = False,
    refresh_assignments: bool = False,
    refresh_files: bool = False,
    skip_announcements: bool = False,
    skip_discussions: bool = False,
    skip_people: bool = False,
    skip_pages: bool = False,
    skip_syllabus: bool = False,
    skip_modules: bool = False,
    skip_assignments: bool = False,
    skip_files: bool = False,
    login_only: bool = False,
    show_progress: bool = False,
    console: Console | None = None,
) -> CanvasSyncResult:
    root = Path(data_path)
    console = console or Console()
    options = SyncOptions(
        refresh_course=refresh_course,
        refresh_people=refresh_people,
        refresh_content=refresh_content,
        refresh_announcements=refresh_announcements,
        refresh_discussions=refresh_discussions,
        refresh_pages=refresh_pages,
        refresh_syllabus=refresh_syllabus,
        refresh_modules=refresh_modules,
        refresh_assignments=refresh_assignments,
        refresh_files=refresh_files,
        skip_announcements=skip_announcements,
        skip_discussions=skip_discussions,
        skip_people=skip_people,
        skip_pages=skip_pages,
        skip_syllabus=skip_syllabus,
        skip_modules=skip_modules,
        skip_assignments=skip_assignments,
        skip_files=skip_files,
    )

    session_refreshed = ensure_canvas_session(
        base_url=base_url,
        site_name=site_name,
        login_wait_seconds=login_wait_seconds,
    )
    if login_only:
        return CanvasSyncResult(
            data_path=root,
            course_count=0,
            index_path=root / INDEX_FILE,
            course_paths=[],
            session_refreshed=session_refreshed,
            updates=[],
        )

    root.mkdir(parents=True, exist_ok=True)
    client = CanvasClient(base_url=base_url, site_name=site_name)
    profile = client.profile()
    courses = client.active_courses() + client.past_courses()
    favorite_courses = client.favorite_courses()
    dashboard_cards = client.dashboard_cards()
    records = merge_course_records(courses, favorite_courses, dashboard_cards)
    records = filter_records(records, course_selectors or [])
    if max_courses is not None:
        records = records[:max_courses]

    synced_at = now_utc_iso()
    folder_names = unique_course_folder_names_by_term(records)
    existing_index = load_existing_index(root)
    course_paths: list[Path] = []
    index_courses: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []

    progress: Progress | None = None
    if show_progress:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        )
        progress.start()
        task_id = progress.add_task("Syncing Canvas courses", total=len(records))
    else:
        task_id = None

    try:
        for record in records:
            course_dir, term_folder_name, folder_name = course_dir_for_record(
                root=root,
                record=record,
                folder_names=folder_names,
                existing_index=existing_index,
            )
            course_dir.mkdir(parents=True, exist_ok=True)
            metadata_path = course_dir / COURSE_METADATA_FILE

            if progress is not None and task_id is not None:
                progress.update(
                    task_id, description=f"Syncing {record.course_code or record.id}"
                )

            metadata, tabs, course_status = load_or_build_course_metadata(
                client=client,
                record=record,
                base_url=base_url,
                term_folder_name=term_folder_name,
                folder_name=folder_name,
                course_dir=course_dir,
                synced_at=synced_at,
                refresh_course=refresh_course,
            )
            content_summary = sync_course_content(
                client=client,
                course_id=record.id,
                course_dir=course_dir,
                tabs=tabs,
                synced_at=synced_at,
                options=options,
            )
            content_changed = any(
                isinstance(section, dict)
                and section.get("status") in {"created", "updated", "error"}
                for section in content_summary["sections"].values()
            )
            if course_status != "unchanged" or content_changed:
                metadata["synced_at"] = synced_at
                metadata["content"] = content_summary
                write_json(metadata_path, metadata)
            course_paths.append(course_dir)

            cover_path = metadata.get("cover_image", {}).get("path")
            cover_image_path = (
                (metadata_path.parent / cover_path).resolve().as_posix()
                if isinstance(cover_path, str) and cover_path
                else None
            )
            index_course = {
                "id": record.id,
                "name": metadata.get("course", {}).get("name"),
                "course_code": metadata.get("course", {}).get("course_code"),
                "enrollment_state": metadata.get("course", {}).get("enrollment_state"),
                "term_name": record.term_name
                or metadata.get("course", {}).get("term", {}).get("name"),
                "term_folder_name": term_folder_name,
                "folder_name": folder_name,
                "metadata_path": metadata_path.resolve().as_posix(),
                "cover_image_present": metadata.get("cover_image", {}).get("present"),
                "cover_image_downloaded": metadata.get("cover_image", {}).get(
                    "downloaded"
                ),
                "cover_image_path": cover_image_path,
                "available_section_count": len(
                    metadata.get("available_sections") or []
                ),
                "available_sections": [
                    {
                        "id": section.get("id"),
                        "label": section.get("label"),
                        "type": section.get("type"),
                    }
                    for section in metadata.get("available_sections") or []
                    if isinstance(section, dict)
                ],
                "content": content_summary["sections"],
            }
            index_courses.append(index_course)
            updates.append(
                {
                    "id": record.id,
                    "course_code": metadata.get("course", {}).get("course_code"),
                    "course_status": course_status,
                    "content": content_summary["sections"],
                }
            )
            if progress is not None and task_id is not None:
                progress.advance(task_id)
    finally:
        if progress is not None:
            progress.stop()

    index_path = root / INDEX_FILE
    index = {
        "schema_version": 2,
        "synced_at": synced_at,
        "base_url": base_url,
        "site_name": site_name,
        "session_refreshed": session_refreshed,
        "student": {
            "id": profile.get("id"),
            "name": profile.get("name"),
            "short_name": profile.get("short_name"),
        },
        "course_count": len(index_courses),
        "courses": [
            relative_index_course(course, index_path) for course in index_courses
        ],
    }
    write_json(index_path, index)
    if show_progress:
        render_update_table(console, updates)
    return CanvasSyncResult(
        data_path=root,
        course_count=len(index_courses),
        index_path=index_path,
        course_paths=course_paths,
        session_refreshed=session_refreshed,
        updates=updates,
    )
