from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

try:
    from .client import CanvasClient
    from .utils import (
        COURSE_METADATA_FILE,
        content_file_path,
        fingerprint,
        read_json,
        rel_path,
        write_html,
        write_json,
    )
except ImportError:
    from client import CanvasClient
    from utils import (
        COURSE_METADATA_FILE,
        content_file_path,
        fingerprint,
        read_json,
        rel_path,
        write_html,
        write_json,
    )


def sync_syllabus(
    *,
    client: CanvasClient,
    course_id: str,
    course_dir: Path,
    synced_at: str,
    force: bool,
) -> dict[str, Any]:
    json_path = content_file_path(course_dir, "syllabus")
    html_path = course_dir / "syllabus.html"
    existing = read_json(json_path)
    if existing and html_path.exists() and not force:
        return {
            "available": True,
            "checked": False,
            "fetched": False,
            "status": "skipped",
            "reason": "Canvas has no cheap syllabus body update check; use --refresh-syllabus",
            "path": rel_path(course_dir / COURSE_METADATA_FILE, json_path),
            "count": int(bool(existing.get("body_present"))),
        }

    syllabus = client.course_syllabus(course_id)
    body = (
        syllabus.get("syllabus_body")
        if isinstance(syllabus.get("syllabus_body"), str)
        else ""
    )
    write_html(html_path, f"{syllabus.get('course_code') or course_id} syllabus", body)
    html_rel = rel_path(json_path, html_path)
    raw = copy.deepcopy(syllabus)
    if "syllabus_body" in raw:
        raw["syllabus_body"] = html_rel
    payload = {
        "schema_version": 2,
        "synced_at": synced_at,
        "course_id": course_id,
        "content_type": "syllabus",
        "body": html_rel,
        "body_present": bool(body),
        "fingerprint": fingerprint(body),
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
        "raw": raw,
    }
    write_json(json_path, payload)
    return {
        "available": True,
        "checked": True,
        "fetched": True,
        "status": "updated" if existing else "created",
        "path": rel_path(course_dir / COURSE_METADATA_FILE, json_path),
        "count": int(bool(body)),
    }
