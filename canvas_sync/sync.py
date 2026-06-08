from __future__ import annotations

import copy
import hashlib
import html
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import pyrootutils
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

pyroot = pyrootutils.setup_root(__file__, dotenv=True, pythonpath=True, cwd=True)

try:
    from .auth import DEFAULT_LOGIN_WAIT_SECONDS, ensure_canvas_session
    from .client import CanvasAPIError, CanvasClient
    from .models import (
        CourseRecord,
        merge_course_records,
        now_utc_iso,
        path_for_course,
        unique_course_folder_names_by_term,
    )
except ImportError:
    from auth import DEFAULT_LOGIN_WAIT_SECONDS, ensure_canvas_session
    from client import CanvasAPIError, CanvasClient
    from models import (
        CourseRecord,
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
    "announcements": Path("announcements") / "announcements.json",
    "discussions": Path("discussions") / "discussions.json",
    "people": Path("people.json"),
    "pages": Path("pages") / "pages.json",
    "syllabus": Path("syllabus.json"),
    "modules": Path("modules.json"),
}
CONTENT_TAB_IDS = {
    "announcements": "announcements",
    "discussions": "discussions",
    "people": "people",
    "pages": "pages",
    "syllabus": "syllabus",
    "modules": "modules",
}
CONTENT_TYPES = tuple(CONTENT_FILES)
BODY_FIELD_NAMES = {"body", "content", "message"}
FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._ -]+")
MULTISPACE = re.compile(r"\s+")


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
        return False


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def json_text(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def write_json(path: Path, data: dict[str, Any]) -> bool:
    text = json_text(data)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == text:
                return False
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)
    return True


def write_html(path: Path, title: str | None, body: str | None) -> bool:
    safe_title = html.escape(title or "Canvas content")
    html_text = (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        f"  <title>{safe_title}</title>\n"
        "</head>\n"
        "<body>\n"
        f"  <h1>{safe_title}</h1>\n"
        f"  <main>{body or ''}</main>\n"
        "</body>\n"
        "</html>\n"
    )
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == html_text:
                return False
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(html_text, encoding="utf-8")
    tmp_path.replace(path)
    return True


def rel_path(from_json_path: Path, target_path: Path) -> str:
    return os.path.relpath(target_path, start=from_json_path.parent).replace(
        os.sep, "/"
    )


def resolve_relative_path(from_json_path: Path, path_value: str | None) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value)
    if path.is_absolute():
        return path
    return from_json_path.parent / path


def normalize_existing_path(
    *,
    json_path: Path,
    target_path: str | None,
    course_dir: Path,
) -> str | None:
    if not target_path:
        return target_path
    path = Path(target_path)
    if not path.is_absolute():
        return target_path.replace("\\", "/")
    try:
        path.relative_to(course_dir)
    except ValueError:
        return target_path.replace("\\", "/")
    return rel_path(json_path, path)


def fingerprint(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_file_path(course_dir: Path, content_type: str) -> Path:
    return course_dir / CONTENT_FILES[content_type]


def open_tab_ids(tabs: list[dict[str, Any]]) -> set[str]:
    return {
        str(tab.get("id"))
        for tab in tabs
        if isinstance(tab, dict) and tab.get("hidden") is not True
    }


def safe_html_filename(id_value: Any, title: Any, fallback: str) -> str:
    raw = f"{id_value or fallback}-{title or fallback}"
    cleaned = FILENAME_CHARS.sub("_", str(raw))
    cleaned = MULTISPACE.sub(" ", cleaned).strip(" ._")
    if len(cleaned) > 120:
        cleaned = cleaned[:120].strip(" ._")
    return f"{cleaned or fallback}.html"


def list_payload(
    *,
    course_id: str,
    content_type: str,
    synced_at: str,
    items: list[dict[str, Any]],
    fingerprint_value: str,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "synced_at": synced_at,
        "course_id": course_id,
        "content_type": content_type,
        "count": len(items),
        "fingerprint": fingerprint_value,
        "items": items,
    }


def existing_items_by_key(
    existing_payload: dict[str, Any] | None,
    key: str,
) -> dict[str, dict[str, Any]]:
    if not existing_payload:
        return {}
    items = existing_payload.get("items")
    if not isinstance(items, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, dict) and item.get(key) is not None:
            result[str(item[key])] = item
    return result


def item_signature(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    selected = {key: item.get(key) for key in keys if key in item}
    return fingerprint(selected)


def item_has_html(
    existing_item: dict[str, Any] | None, json_path: Path, field: str
) -> bool:
    if not existing_item:
        return False
    path = resolve_relative_path(json_path, str(existing_item.get(field) or ""))
    return bool(path and path.exists())


def replace_message_fields_with_path(
    value: Any, path: str, *, with_fragments: bool = False
) -> Any:
    if isinstance(value, list):
        return [
            replace_message_fields_with_path(item, path, with_fragments=with_fragments)
            for item in value
        ]
    if not isinstance(value, dict):
        return value

    result: dict[str, Any] = {}
    fragment = ""
    if with_fragments and value.get("id") is not None:
        fragment = f"#entry-{value['id']}"
    for key, item in value.items():
        if key in BODY_FIELD_NAMES and isinstance(item, str):
            result[key] = f"{path}{fragment}"
        else:
            result[key] = replace_message_fields_with_path(
                item,
                path,
                with_fragments=with_fragments,
            )
    return result


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


def topic_status(existing_item: dict[str, Any] | None) -> str:
    return "updated" if existing_item else "created"


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
    topics = client.course_discussion_topics(
        course_id, only_announcements=only_announcements
    )
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
        html_path = html_dir / safe_html_filename(
            topic_id, topic.get("title"), content_type
        )
        html_rel = rel_path(json_path, html_path)
        existing_signature = (
            existing_item.get("_canvas_sync", {}).get("signature")
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
                topic_copy["_canvas_sync_view_error"] = str(exc)

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
        topic_copy["_canvas_sync"] = {
            "signature": signature,
            "html_path": html_rel,
            "status": topic_status(existing_item),
        }
        items.append(topic_copy)
        changed = True
        changed_items += 1

    fingerprint_value = fingerprint(
        [item.get("_canvas_sync", {}).get("signature") for item in items]
    )
    if existing and existing.get("fingerprint") != fingerprint_value:
        changed = True
    payload = list_payload(
        course_id=course_id,
        content_type=content_type,
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
        "status": (
            "created" if existing is None else ("updated" if changed else "unchanged")
        ),
        "path": rel_path(course_dir / COURSE_METADATA_FILE, json_path),
        "count": len(items),
        "changed_items": changed_items,
    }


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
        content_type="people",
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
        html_path = json_path.parent / safe_html_filename(
            page_url, summary.get("title"), "page"
        )
        html_rel = rel_path(json_path, html_path)
        existing_signature = (
            existing_item.get("_canvas_sync", {}).get("signature")
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
                detail["_canvas_sync_detail_error"] = str(exc)
        else:
            detail = dict(summary)
        body = detail.get("body") if isinstance(detail.get("body"), str) else ""
        write_html(
            html_path,
            str(detail.get("title") or summary.get("title") or "Canvas page"),
            body,
        )
        detail["body"] = html_rel
        detail["_canvas_sync"] = {
            "signature": signature,
            "html_path": html_rel,
            "status": topic_status(existing_item),
        }
        items.append(detail)
        changed = True
        changed_items += 1

    fingerprint_value = fingerprint(
        [item.get("_canvas_sync", {}).get("signature") for item in items]
    )
    if existing and existing.get("fingerprint") != fingerprint_value:
        changed = True
    payload = list_payload(
        course_id=course_id,
        content_type="pages",
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
        "status": (
            "created" if existing is None else ("updated" if changed else "unchanged")
        ),
        "path": rel_path(course_dir / COURSE_METADATA_FILE, json_path),
        "count": len(items),
        "changed_items": changed_items,
    }


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


def sync_modules(
    *,
    client: CanvasClient,
    course_id: str,
    course_dir: Path,
    synced_at: str,
    force: bool,
) -> dict[str, Any]:
    json_path = content_file_path(course_dir, "modules")
    existing = read_json(json_path)
    modules = client.course_modules(course_id)
    fingerprint_value = fingerprint(modules)
    if existing and existing.get("fingerprint") == fingerprint_value and not force:
        return {
            "available": True,
            "checked": True,
            "fetched": False,
            "status": "unchanged",
            "path": rel_path(course_dir / COURSE_METADATA_FILE, json_path),
            "count": int(existing.get("count") or len(modules)),
        }
    payload = list_payload(
        course_id=course_id,
        content_type="modules",
        synced_at=synced_at,
        items=modules,
        fingerprint_value=fingerprint_value,
    )
    write_json(json_path, payload)
    return {
        "available": True,
        "checked": True,
        "fetched": True,
        "status": "updated" if existing else "created",
        "path": rel_path(course_dir / COURSE_METADATA_FILE, json_path),
        "count": len(modules),
    }


def sync_course_content(
    *,
    client: CanvasClient,
    course_id: str,
    course_dir: Path,
    tabs: list[dict[str, Any]],
    synced_at: str,
    options: SyncOptions,
) -> dict[str, Any]:
    available_tabs = open_tab_ids(tabs)
    sections: dict[str, dict[str, Any]] = {}

    for content_type in CONTENT_TYPES:
        tab_id = CONTENT_TAB_IDS[content_type]
        if tab_id not in available_tabs:
            sections[content_type] = {
                "available": False,
                "checked": False,
                "fetched": False,
                "status": "closed",
                "path": None,
                "count": 0,
            }
            continue

        force = options.force_content(content_type)
        try:
            if content_type == "announcements":
                summary = sync_topics(
                    client=client,
                    course_id=course_id,
                    course_dir=course_dir,
                    content_type="announcements",
                    synced_at=synced_at,
                    force=force,
                )
            elif content_type == "discussions":
                summary = sync_topics(
                    client=client,
                    course_id=course_id,
                    course_dir=course_dir,
                    content_type="discussions",
                    synced_at=synced_at,
                    force=force,
                )
            elif content_type == "people":
                summary = sync_people(
                    client=client,
                    course_id=course_id,
                    course_dir=course_dir,
                    synced_at=synced_at,
                    force=force,
                )
            elif content_type == "pages":
                summary = sync_pages(
                    client=client,
                    course_id=course_id,
                    course_dir=course_dir,
                    synced_at=synced_at,
                    force=force,
                )
            elif content_type == "syllabus":
                summary = sync_syllabus(
                    client=client,
                    course_id=course_id,
                    course_dir=course_dir,
                    synced_at=synced_at,
                    force=force,
                )
            elif content_type == "modules":
                summary = sync_modules(
                    client=client,
                    course_id=course_id,
                    course_dir=course_dir,
                    synced_at=synced_at,
                    force=force,
                )
            else:
                raise ValueError(f"Unsupported content type: {content_type}")
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
    show_progress: bool = False,
    console: Console | None = None,
) -> CanvasSyncResult:
    root = Path(data_path)
    root.mkdir(parents=True, exist_ok=True)
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
    )

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
