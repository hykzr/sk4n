from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from .client import CanvasAPIError, CanvasClient
    from .utils import (
        COURSE_METADATA_FILE,
        content_file_path,
        existing_items_by_key,
        fingerprint,
        item_has_html,
        item_signature,
        list_payload,
        read_json,
        rel_path,
        safe_html_filename,
        write_html,
        write_json,
    )
except ImportError:
    from client import CanvasAPIError, CanvasClient
    from utils import (
        COURSE_METADATA_FILE,
        content_file_path,
        existing_items_by_key,
        fingerprint,
        item_has_html,
        item_signature,
        list_payload,
        read_json,
        rel_path,
        safe_html_filename,
        write_html,
        write_json,
    )


def sync_pages(
    *,
    client: CanvasClient,
    course_id: str,
    course_dir: Path,
    synced_at: str,
    force: bool,
) -> dict[str, Any]:
    json_path = content_file_path(course_dir, "pages")
    existing = read_json(json_path)
    existing_by_url = existing_items_by_key(existing, "url")
    summaries = client.course_page_summaries(course_id)
    changed = force or existing is None
    changed_items = 0
    items: list[dict[str, Any]] = []

    for summary in summaries:
        page_url = str(summary.get("url") or "")
        existing_item = existing_by_url.get(page_url)
        signature = item_signature(
            summary,
            (
                "url",
                "title",
                "page_id",
                "updated_at",
                "published",
                "hide_from_students",
                "front_page",
                "locked_for_user",
            ),
        )
        html_path = json_path.parent / safe_html_filename(page_url, summary.get("title"), "page")
        html_rel = rel_path(json_path, html_path)
        existing_signature = (
            existing_item.get("_canvas", {}).get("signature")
            if isinstance(existing_item, dict)
            else None
        )
        unchanged = (
            not force
            and existing_signature == signature
            and item_has_html(existing_item, json_path, "body")
        )
        if unchanged and existing_item:
            items.append(existing_item)
            continue

        if page_url:
            try:
                detail = client.course_page_detail(course_id, page_url)
            except CanvasAPIError as exc:
                detail = dict(summary)
                detail["_canvas_detail_error"] = str(exc)
        else:
            detail = dict(summary)
        body = detail.get("body") if isinstance(detail.get("body"), str) else ""
        write_html(
            html_path,
            str(detail.get("title") or summary.get("title") or "Canvas page"),
            body,
        )
        detail["body"] = html_rel
        detail["_canvas"] = {
            "signature": signature,
        }
        items.append(detail)
        changed = True
        changed_items += 1

    fingerprint_value = fingerprint(
        [item.get("_canvas", {}).get("signature") for item in items]
    )
    if existing and existing.get("fingerprint") != fingerprint_value:
        changed = True
    payload = list_payload(
        course_id=course_id,
        synced_at=synced_at,
        items=items,
        fingerprint_value=fingerprint_value,
    )
    if changed:
        write_json(json_path, payload)
    return {
        "available": True,
        "checked": True,
        "fetched": changed,
        "status": ("created" if existing is None else ("updated" if changed else "unchanged")),
        "path": rel_path(course_dir / COURSE_METADATA_FILE, json_path),
        "count": len(items),
        "changed_items": changed_items,
    }
