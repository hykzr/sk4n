from __future__ import annotations

from pathlib import Path
from typing import Any

from .client import CanvasClient
from .utils import (
    COURSE_METADATA_FILE,
    content_file_path,
    fingerprint,
    list_payload,
    read_json,
    rel_path,
    write_json,
)


def sync_people(
    *,
    client: CanvasClient,
    course_id: str,
    course_dir: Path,
    synced_at: str,
    force: bool,
) -> dict[str, Any]:
    json_path = content_file_path(course_dir, "people")
    existing = read_json(json_path)
    items = client.course_people(course_id)
    fingerprint_value = fingerprint(items)
    if existing and existing.get("fingerprint") == fingerprint_value and not force:
        return {
            "available": True,
            "checked": True,
            "fetched": False,
            "status": "unchanged",
            "path": rel_path(course_dir / COURSE_METADATA_FILE, json_path),
            "count": int(existing.get("count") or len(items)),
        }

    payload = list_payload(
        course_id=course_id,
        synced_at=synced_at,
        items=items,
        fingerprint_value=fingerprint_value,
    )
    write_json(json_path, payload)
    return {
        "available": True,
        "checked": True,
        "fetched": True,
        "status": "updated" if existing else "created",
        "path": rel_path(course_dir / COURSE_METADATA_FILE, json_path),
        "count": len(items),
    }
