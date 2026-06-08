from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyrootutils

pyroot = pyrootutils.setup_root(__file__, dotenv=True, pythonpath=True, cwd=True)

try:
    from .auth import DEFAULT_LOGIN_WAIT_SECONDS, ensure_canvas_session
    from .client import CanvasAPIError, CanvasClient
    from .models import (
        merge_course_records,
        now_utc_iso,
        path_for_course,
        unique_course_folder_names_by_term,
    )
except ImportError:
    from auth import DEFAULT_LOGIN_WAIT_SECONDS, ensure_canvas_session
    from client import CanvasAPIError, CanvasClient
    from models import (
        merge_course_records,
        now_utc_iso,
        path_for_course,
        unique_course_folder_names_by_term,
    )


DEFAULT_BASE_URL = "https://canvas.nus.edu.sg"
DEFAULT_SITE_NAME = "nus_canvas"
DEFAULT_DATA_PATH = pyroot / "data" / "canvas"
COURSE_METADATA_FILE = "course.json"
INDEX_FILE = "index.json"
CONTENT_FILES = {
    "announcements": "announcements.json",
    "discussions": "discussions.json",
    "people": "people.json",
    "pages": "pages.json",
    "syllabus": "syllabus.json",
    "modules": "modules.json",
}
CONTENT_TAB_IDS = {
    "announcements": "announcements",
    "discussions": "discussions",
    "people": "people",
    "pages": "pages",
    "syllabus": "syllabus",
    "modules": "modules",
}


@dataclass(frozen=True)
class CanvasSyncResult:
    data_path: Path
    course_count: int
    index_path: Path
    course_paths: list[Path]
    session_refreshed: bool


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)


def open_tab_ids(tabs: list[dict[str, Any]]) -> set[str]:
    return {
        str(tab.get("id"))
        for tab in tabs
        if isinstance(tab, dict) and tab.get("hidden") is not True
    }


def content_payload(
    client: CanvasClient,
    *,
    course_id: str,
    content_type: str,
    synced_at: str,
) -> tuple[dict[str, Any], int]:
    if content_type == "announcements":
        items = client.course_announcements(course_id)
        return list_content_payload(course_id, content_type, synced_at, items), len(items)
    if content_type == "discussions":
        items = client.course_discussions(course_id)
        return list_content_payload(course_id, content_type, synced_at, items), len(items)
    if content_type == "people":
        items = client.course_people(course_id)
        return list_content_payload(course_id, content_type, synced_at, items), len(items)
    if content_type == "pages":
        items = client.course_pages(course_id)
        return list_content_payload(course_id, content_type, synced_at, items), len(items)
    if content_type == "modules":
        items = client.course_modules(course_id)
        return list_content_payload(course_id, content_type, synced_at, items), len(items)
    if content_type == "syllabus":
        syllabus = client.course_syllabus(course_id)
        payload = {
            "schema_version": 1,
            "synced_at": synced_at,
            "course_id": course_id,
            "content_type": content_type,
            "body": syllabus.get("syllabus_body"),
            "body_present": bool(syllabus.get("syllabus_body")),
            "course": {
                key: syllabus.get(key)
                for key in (
                    "id",
                    "name",
                    "course_code",
                    "workflow_state",
                    "default_view",
                    "start_at",
                    "end_at",
                    "time_zone",
                    "public_syllabus",
                    "public_syllabus_to_auth",
                )
                if key in syllabus
            },
            "raw": syllabus,
        }
        return payload, int(bool(syllabus.get("syllabus_body")))
    raise ValueError(f"Unsupported course content type: {content_type}")


def list_content_payload(
    course_id: str,
    content_type: str,
    synced_at: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "synced_at": synced_at,
        "course_id": course_id,
        "content_type": content_type,
        "count": len(items),
        "items": items,
    }


def sync_course_content(
    *,
    client: CanvasClient,
    course_id: str,
    course_dir: Path,
    tabs: list[dict[str, Any]],
    synced_at: str,
) -> dict[str, Any]:
    available_tabs = open_tab_ids(tabs)
    sections: dict[str, dict[str, Any]] = {}

    for content_type, file_name in CONTENT_FILES.items():
        content_path = course_dir / file_name
        tab_id = CONTENT_TAB_IDS[content_type]
        if tab_id not in available_tabs:
            if content_path.exists():
                content_path.unlink()
            sections[content_type] = {
                "available": False,
                "fetched": False,
                "path": None,
                "count": 0,
            }
            continue

        try:
            payload, count = content_payload(
                client,
                course_id=course_id,
                content_type=content_type,
                synced_at=synced_at,
            )
        except CanvasAPIError as exc:
            payload = {
                "schema_version": 1,
                "synced_at": synced_at,
                "course_id": course_id,
                "content_type": content_type,
                "available": True,
                "fetched": False,
                "error": str(exc),
            }
            count = 0
            fetched = False
        else:
            fetched = True

        write_json(content_path, payload)
        summary = {
            "available": True,
            "fetched": fetched,
            "path": content_path.as_posix(),
            "count": count,
        }
        if not fetched:
            summary["error"] = payload["error"]
        sections[content_type] = summary

    return {
        "schema_version": 1,
        "synced_at": synced_at,
        "sections": sections,
    }


def sync_canvas(
    *,
    data_path: str | Path = DEFAULT_DATA_PATH,
    base_url: str = DEFAULT_BASE_URL,
    site_name: str = DEFAULT_SITE_NAME,
    max_courses: int | None = None,
    login_wait_seconds: int = DEFAULT_LOGIN_WAIT_SECONDS,
) -> CanvasSyncResult:
    root = Path(data_path)
    root.mkdir(parents=True, exist_ok=True)

    session_refreshed = ensure_canvas_session(
        base_url=base_url,
        site_name=site_name,
        login_wait_seconds=login_wait_seconds,
    )
    client = CanvasClient(base_url=base_url, site_name=site_name)
    profile = client.profile()
    courses = client.active_courses() + client.past_courses()
    favorite_courses = client.favorite_courses()
    dashboard_cards = client.dashboard_cards()
    records = merge_course_records(courses, favorite_courses, dashboard_cards)
    if max_courses is not None:
        records = records[:max_courses]

    synced_at = now_utc_iso()
    folder_names = unique_course_folder_names_by_term(records)
    course_paths: list[Path] = []
    index_courses: list[dict[str, Any]] = []

    for record in records:
        term_folder_name = record.term_folder_base_name()
        folder_name = folder_names[record.id]
        course_dir = path_for_course(root, term_folder_name, folder_name)
        course_dir.mkdir(parents=True, exist_ok=True)

        tabs = client.course_tabs(record.id)
        metadata = record.to_json_record(
            base_url=base_url,
            term_folder_name=term_folder_name,
            folder_name=folder_name,
            tabs=tabs,
            synced_at=synced_at,
        )
        metadata["content"] = sync_course_content(
            client=client,
            course_id=record.id,
            course_dir=course_dir,
            tabs=tabs,
            synced_at=synced_at,
        )
        metadata["cover_image"].update(
            client.download_cover_image(metadata["cover_image"]["url"], course_dir)
        )
        metadata_path = course_dir / COURSE_METADATA_FILE
        write_json(metadata_path, metadata)
        course_paths.append(course_dir)
        index_courses.append(
            {
                "id": record.id,
                "name": metadata["course"]["name"],
                "course_code": metadata["course"]["course_code"],
                "enrollment_state": metadata["course"]["enrollment_state"],
                "term_name": record.term_name,
                "term_folder_name": term_folder_name,
                "folder_name": folder_name,
                "metadata_path": str(metadata_path.as_posix()),
                "cover_image_present": metadata["cover_image"]["present"],
                "cover_image_downloaded": metadata["cover_image"]["downloaded"],
                "cover_image_path": metadata["cover_image"]["path"],
                "available_section_count": len(metadata["available_sections"]),
                "available_sections": [
                    {
                        "id": section["id"],
                        "label": section["label"],
                        "type": section["type"],
                    }
                    for section in metadata["available_sections"]
                ],
                "content": metadata["content"]["sections"],
            }
        )

    index = {
        "schema_version": 1,
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
        "courses": index_courses,
    }
    index_path = root / INDEX_FILE
    write_json(index_path, index)
    return CanvasSyncResult(
        data_path=root,
        course_count=len(index_courses),
        index_path=index_path,
        course_paths=course_paths,
        session_refreshed=session_refreshed,
    )
