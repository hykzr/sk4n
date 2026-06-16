from __future__ import annotations

from pathlib import Path
from typing import Any

try:
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
except ImportError:
    from client import CanvasClient
    from utils import (
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
    if existing and not force:
        return {
            "available": True,
            "checked": False,
            "fetched": False,
            "status": "skipped",
            "reason": "people are assumed unchanged; use --refresh-people to force",
            "path": rel_path(course_dir / COURSE_METADATA_FILE, json_path),
            "count": int(existing.get("count") or 0),
        }

    items = client.course_people(course_id)
    payload = list_payload(
        course_id=course_id,
        synced_at=synced_at,
        items=items,
        fingerprint_value=fingerprint(items),
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
