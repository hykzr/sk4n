from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from .assignments import sync_assignments
    from .client import CanvasAPIError, CanvasClient
    from .files import sync_files
    from .modules import sync_modules
    from .pages import sync_pages
    from .people import sync_people
    from .syllabus import sync_syllabus
    from .topics import sync_topics
    from .utils import (
        CONTENT_TAB_IDS,
        CONTENT_TYPES,
        COURSE_METADATA_FILE,
        content_file_path,
        open_tab_ids,
        read_json,
        rel_path,
    )
except ImportError:
    from assignments import sync_assignments
    from client import CanvasAPIError, CanvasClient
    from files import sync_files
    from modules import sync_modules
    from pages import sync_pages
    from people import sync_people
    from syllabus import sync_syllabus
    from topics import sync_topics
    from utils import (
        CONTENT_TAB_IDS,
        CONTENT_TYPES,
        COURSE_METADATA_FILE,
        content_file_path,
        open_tab_ids,
        read_json,
        rel_path,
    )


def content_available(content_type: str, available_tabs: set[str]) -> bool:
    if content_type == "assignments":
        return "assignments" in available_tabs or "quizzes" in available_tabs
    if content_type == "files":
        return True
    return CONTENT_TAB_IDS[content_type] in available_tabs


BASIC_SYNCERS = {
    "people": sync_people,
    "pages": sync_pages,
    "syllabus": sync_syllabus,
    "modules": sync_modules,
}
TABBED_SYNCERS = {
    "assignments": sync_assignments,
    "files": sync_files,
}


def closed_summary(content_type: str) -> dict[str, Any]:
    return {
        "available": False,
        "checked": False,
        "fetched": False,
        "status": "closed",
        "path": None,
        "count": 0,
    }


def skipped_summary(*, course_dir: Path, content_type: str) -> dict[str, Any]:
    json_path = content_file_path(course_dir, content_type)
    existing = read_json(json_path)
    return {
        "available": True,
        "checked": False,
        "fetched": False,
        "status": "skipped",
        "reason": f"skipped by --skip-{content_type}",
        "path": rel_path(course_dir / COURSE_METADATA_FILE, json_path),
        "count": int(existing.get("count") or 0) if existing else 0,
    }


def run_content_syncer(
    *,
    client: CanvasClient,
    course_id: str,
    course_dir: Path,
    tabs: list[dict[str, Any]],
    synced_at: str,
    content_type: str,
    force: bool,
) -> dict[str, Any]:
    common_kwargs = {
        "client": client,
        "course_id": course_id,
        "course_dir": course_dir,
        "synced_at": synced_at,
        "force": force,
    }
    if content_type in {"announcements", "discussions"}:
        return sync_topics(
            **common_kwargs,
            content_type=content_type,
        )
    if content_type in BASIC_SYNCERS:
        return BASIC_SYNCERS[content_type](**common_kwargs)
    if content_type in TABBED_SYNCERS:
        return TABBED_SYNCERS[content_type](**common_kwargs, tabs=tabs)
    raise ValueError(f"Unsupported content type: {content_type}")


def sync_course_content(
    *,
    client: CanvasClient,
    course_id: str,
    course_dir: Path,
    tabs: list[dict[str, Any]],
    synced_at: str,
    options: Any,
) -> dict[str, Any]:
    available_tabs = open_tab_ids(tabs)
    sections: dict[str, dict[str, Any]] = {}

    for content_type in CONTENT_TYPES:
        if options.skip_content(content_type):
            sections[content_type] = skipped_summary(
                course_dir=course_dir,
                content_type=content_type,
            )
            continue

        if not content_available(content_type, available_tabs):
            sections[content_type] = closed_summary(content_type)
            continue

        force = options.force_content(content_type)
        try:
            summary = run_content_syncer(
                client=client,
                course_id=course_id,
                course_dir=course_dir,
                tabs=tabs,
                synced_at=synced_at,
                content_type=content_type,
                force=force,
            )
        except CanvasAPIError as exc:
            summary = {
                "available": True,
                "checked": True,
                "fetched": False,
                "status": "error",
                "path": rel_path(
                    course_dir / COURSE_METADATA_FILE,
                    content_file_path(course_dir, content_type),
                ),
                "count": 0,
                "error": str(exc),
            }
        sections[content_type] = summary

    return {
        "schema_version": 2,
        "synced_at": synced_at,
        "sections": sections,
    }
