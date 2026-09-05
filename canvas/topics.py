from __future__ import annotations

import copy
import html
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
        replace_message_fields_with_path,
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
        replace_message_fields_with_path,
        safe_html_filename,
        write_html,
        write_json,
    )


def discussion_entries_html(entries: Any) -> str:
    if not isinstance(entries, list):
        return ""
    parts: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_id = html.escape(str(entry.get("id") or ""))
        author_id = entry.get("user_id")
        created_at = html.escape(str(entry.get("created_at") or ""))
        message = entry.get("message") if isinstance(entry.get("message"), str) else ""
        parts.append(
            f'<article id="entry-{entry_id}">'
            f"<h2>Entry {entry_id}</h2>"
            f"<p>Author: {html.escape(str(author_id or ''))} "
            f"{created_at}</p>"
            f"{message}"
        )
        parts.append(discussion_entries_html(entry.get("replies")))
        parts.append("</article>")
    return "\n".join(parts)


def discussion_html_body(topic: dict[str, Any], view: dict[str, Any] | None) -> str:
    parts = [topic.get("message") if isinstance(topic.get("message"), str) else ""]
    if view:
        parts.append(discussion_entries_html(view.get("view")))
        forced_entries = discussion_entries_html(view.get("forced_entries"))
        if forced_entries:
            parts.append("<h2>Forced entries</h2>")
            parts.append(forced_entries)
    return "\n".join(part for part in parts if part)


def sync_topics(
    *,
    client: CanvasClient,
    course_id: str,
    course_dir: Path,
    content_type: str,
    synced_at: str,
    force: bool,
) -> dict[str, Any]:
    json_path = content_file_path(course_dir, content_type)
    existing = read_json(json_path)
    existing_by_id = existing_items_by_key(existing, "id")
    html_dir = json_path.parent

    only_announcements = content_type == "announcements"
    topics = client.course_discussion_topics(course_id, only_announcements=only_announcements)
    if content_type == "discussions":
        topics = [topic for topic in topics if topic.get("is_announcement") is not True]

    changed = force or existing is None
    items: list[dict[str, Any]] = []
    changed_items = 0
    topic_keys = (
        "id",
        "title",
        "posted_at",
        "last_reply_at",
        "discussion_subentry_count",
        "published",
        "locked",
        "attachments",
        "message",
    )

    for topic in topics:
        topic_id = str(topic.get("id") or "")
        existing_item = existing_by_id.get(topic_id)
        signature = item_signature(topic, topic_keys)
        html_path = html_dir / safe_html_filename(topic_id, topic.get("title"), content_type)
        html_rel = rel_path(json_path, html_path)
        existing_signature = (
            existing_item.get("_canvas", {}).get("signature")
            if isinstance(existing_item, dict)
            else None
        )
        unchanged = (
            not force
            and existing_signature == signature
            and item_has_html(existing_item, json_path, "message")
        )
        if unchanged and existing_item:
            items.append(existing_item)
            continue

        topic_copy = copy.deepcopy(topic)
        view: dict[str, Any] | None = None
        if content_type == "discussions" and topic.get("id") is not None:
            try:
                view = client.course_discussion_view(course_id, topic["id"])
            except CanvasAPIError as exc:
                topic_copy["_canvas_view_error"] = str(exc)

        body = (
            discussion_html_body(topic_copy, view)
            if content_type == "discussions"
            else topic_copy.get("message")
        )
        write_html(
            html_path,
            str(topic.get("title") or content_type),
            body if isinstance(body, str) else "",
        )
        topic_copy["message"] = html_rel
        if view:
            topic_copy["view"] = replace_message_fields_with_path(
                view,
                html_rel,
                with_fragments=True,
            )
        topic_copy["_canvas"] = {
            "signature": signature,
        }
        items.append(topic_copy)
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
