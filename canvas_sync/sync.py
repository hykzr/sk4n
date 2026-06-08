from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyrootutils

pyroot = pyrootutils.setup_root(__file__, dotenv=True, pythonpath=True, cwd=True)

from auth import DEFAULT_LOGIN_WAIT_SECONDS, ensure_canvas_session
from client import CanvasClient
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
