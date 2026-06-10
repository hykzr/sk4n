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
from urllib.parse import unquote, urljoin, urlparse
from bs4 import BeautifulSoup
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
    "assignments": Path("assignments") / "assignments.json",
    "files": Path("files") / "files.json",
}
CONTENT_TAB_IDS = {
    "announcements": "announcements",
    "discussions": "discussions",
    "people": "people",
    "pages": "pages",
    "syllabus": "syllabus",
    "modules": "modules",
    "assignments": "assignments",
    "files": "files",
}
CONTENT_TYPES = tuple(CONTENT_FILES)
BODY_FIELD_NAMES = {"body", "content", "message"}
FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._ -]+")
MULTISPACE = re.compile(r"\s+")
CANVAS_FILE_LINK = re.compile(
    r"(?:/courses/\d+)?/files/(\d+)(?:/(?:download|preview))?[^\"'<>\s)]*"
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
    *,
    items_key: str = "items",
) -> dict[str, dict[str, Any]]:
    if not existing_payload:
        return {}
    items = existing_payload.get(items_key)
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


def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def safe_path_segment(value: Any, fallback: str) -> str:
    raw = unquote(str(value or fallback))
    cleaned = FILENAME_CHARS.sub("_", raw)
    cleaned = MULTISPACE.sub(" ", cleaned).strip(" ._")
    return cleaned or fallback


def folder_relative_path(folder: dict[str, Any] | None) -> Path:
    if not folder:
        return Path("referenced")
    full_name = str(folder.get("full_name") or folder.get("name") or "")
    parts = [part for part in full_name.replace("\\", "/").split("/") if part]
    if parts and parts[0].casefold() == "course files":
        parts = parts[1:]
    if not parts:
        return Path()
    return Path(*[safe_path_segment(part, "folder") for part in parts])


def file_display_name(file_item: dict[str, Any]) -> str:
    for key in ("display_name", "filename"):
        value = file_item.get(key)
        if value:
            return safe_path_segment(value, f"file-{file_item.get('id')}")
    return f"file-{file_item.get('id') or 'unknown'}"


def unique_download_path(
    *,
    files_root: Path,
    folder_path: Path,
    file_item: dict[str, Any],
    used_paths: set[Path],
) -> Path:
    file_id = str(file_item.get("id") or "file")
    base_name = file_display_name(file_item)
    candidate = files_root / folder_path / base_name
    if candidate.resolve() not in used_paths:
        used_paths.add(candidate.resolve())
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    deduped = candidate.with_name(f"{stem} - {file_id}{suffix}")
    counter = 2
    while deduped.resolve() in used_paths:
        deduped = candidate.with_name(f"{stem} - {file_id} ({counter}){suffix}")
        counter += 1
    used_paths.add(deduped.resolve())
    return deduped


def file_signature(file_item: dict[str, Any]) -> str:
    return item_signature(
        file_item,
        (
            "id",
            "folder_id",
            "display_name",
            "filename",
            "content-type",
            "size",
            "updated_at",
            "modified_at",
            "unlock_at",
            "lock_at",
            "locked",
            "hidden",
            "locked_for_user",
            "hidden_for_user",
        ),
    )


def extract_file_ids_from_text(text: str) -> set[str]:
    return {match.group(1) for match in CANVAS_FILE_LINK.finditer(text)}


def extract_file_ids_from_json_value(value: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(value, dict):
        if value.get("type") == "File" and value.get("content_id") is not None:
            ids.add(str(value["content_id"]))
        for item in value.values():
            ids.update(extract_file_ids_from_json_value(item))
    elif isinstance(value, list):
        for item in value:
            ids.update(extract_file_ids_from_json_value(item))
    elif isinstance(value, str):
        ids.update(extract_file_ids_from_text(value))
    return ids


def source_name_for_path(course_dir: Path, path: Path) -> str:
    relative = path.relative_to(course_dir)
    parts = relative.parts
    if not parts:
        return "content"
    if parts[0] in {"announcements", "discussions", "pages"}:
        return parts[0]
    if parts[0] == "modules.json":
        return "modules"
    if parts[0] == "syllabus.json" or parts[0] == "syllabus.html":
        return "syllabus"
    return parts[0]


def collect_referenced_file_ids(course_dir: Path) -> dict[str, set[str]]:
    files_root = course_dir / "files"
    references: dict[str, set[str]] = {}
    for path in course_dir.rglob("*"):
        if not path.is_file() or path_is_relative_to(path, files_root):
            continue
        if path.suffix.lower() not in {".json", ".html", ".htm"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        ids = extract_file_ids_from_text(text)
        if path.suffix.lower() == ".json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
            ids.update(extract_file_ids_from_json_value(data))
        if not ids:
            continue
        source = source_name_for_path(course_dir, path)
        for file_id in ids:
            references.setdefault(file_id, set()).add(source)
    return references


def download_with_fallbacks(
    client: CanvasClient,
    file_item: dict[str, Any],
    output_path: Path,
) -> dict[str, Any]:
    urls: list[str] = []
    if isinstance(file_item.get("url"), str):
        urls.append(file_item["url"])
    for url in file_item.get("_canvas_sync_reference_urls") or []:
        if isinstance(url, str) and url not in urls:
            urls.append(url)
    last_error: str | None = None
    for url in urls:
        try:
            result = client.download_file(url, output_path)
        except CanvasAPIError as exc:
            last_error = str(exc)
            continue
        result["download_url"] = url
        return result
    raise CanvasAPIError(last_error or f"No download URL for file {file_item.get('id')}")


def file_record_from_existing(
    existing_item: dict[str, Any],
    *,
    sources: set[str],
    json_path: Path,
) -> dict[str, Any]:
    item = copy.deepcopy(existing_item)
    item["sources"] = sorted(set(item.get("sources") or []) | sources)
    item["path"] = normalize_existing_path(
        json_path=json_path,
        target_path=item.get("path"),
        course_dir=json_path.parent,
    )
    item["_canvas_sync"] = (
        item.get("_canvas_sync")
        if isinstance(item.get("_canvas_sync"), dict)
        else {}
    )
    item["_canvas_sync"]["status"] = "unchanged"
    return item


def assignment_item_key(kind: str, item_id: Any) -> str:
    return f"{kind}:{item_id}"


def safe_folder_name(value: Any, fallback: str) -> str:
    name = safe_path_segment(value, fallback)
    if len(name) > 100:
        name = name[:100].strip(" ._")
    return name or fallback


def assignment_folder_for_item(
    *,
    assignments_root: Path,
    title: Any,
    item_id: str,
    used_dirs: set[Path],
) -> Path:
    base = safe_folder_name(title, f"assignment-{item_id}")
    candidate = assignments_root / base
    if candidate.resolve() not in used_dirs:
        used_dirs.add(candidate.resolve())
        return candidate
    candidate = assignments_root / f"{base} - {item_id}"
    counter = 2
    while candidate.resolve() in used_dirs:
        candidate = assignments_root / f"{base} - {item_id} ({counter})"
        counter += 1
    used_dirs.add(candidate.resolve())
    return candidate


def submission_signature_value(submission: Any) -> Any:
    if not isinstance(submission, dict):
        return submission
    selected = {
        key: submission.get(key)
        for key in (
            "id",
            "score",
            "grade",
            "attempt",
            "submission_type",
            "submitted_at",
            "body",
            "assignment_id",
            "workflow_state",
            "cached_due_date",
            "grade_matches_current_submission",
            "missing",
            "late",
            "attachments",
        )
        if key in submission
    }
    if isinstance(selected.get("attachments"), list):
        selected["attachments"] = [
            submitted_file_record_base(item)
            for item in selected["attachments"]
            if isinstance(item, dict)
        ]
    return selected


def assignment_summary_signature(
    assignment: dict[str, Any],
    quiz_summary: dict[str, Any] | None = None,
) -> str:
    keys = (
        "id",
        "name",
        "description",
        "points_possible",
        "grading_type",
        "created_at",
        "updated_at",
        "due_at",
        "lock_at",
        "unlock_at",
        "assignment_group_id",
        "submission_types",
        "workflow_state",
        "published",
        "quiz_id",
        "is_quiz_assignment",
        "is_quiz_lti_assignment",
        "external_tool_tag_attributes",
        "has_submitted_submissions",
        "all_dates",
        "overrides",
        "score_statistics",
    )
    selected = {key: assignment.get(key) for key in keys if key in assignment}
    selected["submission"] = submission_signature_value(assignment.get("submission"))
    if quiz_summary:
        selected["quiz_summary"] = {
            key: quiz_summary.get(key)
            for key in (
                "id",
                "title",
                "description",
                "question_count",
                "points_possible",
                "due_at",
                "lock_at",
                "unlock_at",
                "published",
                "locked_for_user",
                "assignment_id",
            )
            if key in quiz_summary
        }
    return fingerprint(selected)


def quiz_summary_signature(quiz: dict[str, Any]) -> str:
    return fingerprint(
        {
            key: quiz.get(key)
            for key in (
                "id",
                "title",
                "description",
                "question_count",
                "points_possible",
                "due_at",
                "lock_at",
                "unlock_at",
                "published",
                "locked_for_user",
                "assignment_id",
                "assignment_group_id",
            )
            if key in quiz
        }
    )


def existing_assignment_payload_ok(
    existing_item: dict[str, Any] | None,
    assignments_json_path: Path,
) -> bool:
    if not existing_item:
        return False
    assignment_json_path = resolve_relative_path(
        assignments_json_path, existing_item.get("path")
    )
    if not assignment_json_path or not assignment_json_path.exists():
        return False
    payload = read_json(assignment_json_path)
    if not payload:
        return False
    content_path = resolve_relative_path(assignment_json_path, payload.get("content"))
    if payload.get("content_present") and not (content_path and content_path.exists()):
        return False
    for image in payload.get("images") or []:
        if not isinstance(image, dict) or not image.get("downloaded"):
            continue
        image_path = resolve_relative_path(assignment_json_path, image.get("path"))
        if not (image_path and image_path.exists() and image.get("sha256")):
            return False
    for submitted_file in payload.get("submitted_files") or []:
        if not isinstance(submitted_file, dict) or not submitted_file.get("downloaded"):
            continue
        file_path = resolve_relative_path(
            assignment_json_path, submitted_file.get("path")
        )
        if not (file_path and file_path.exists() and submitted_file.get("sha256")):
            return False
    quiz = payload.get("quiz")
    if isinstance(quiz, dict) and quiz.get("path"):
        quiz_path = resolve_relative_path(assignment_json_path, quiz.get("path"))
        if not (quiz_path and quiz_path.exists()):
            return False
    return True


def normalize_existing_assignment_item(
    existing_item: dict[str, Any],
    assignments_json_path: Path,
) -> dict[str, Any]:
    item = copy.deepcopy(existing_item)
    item["path"] = normalize_existing_path(
        json_path=assignments_json_path,
        target_path=item.get("path"),
        course_dir=assignments_json_path.parent,
    )
    if item.get("content"):
        item["content"] = normalize_existing_path(
            json_path=assignments_json_path,
            target_path=item.get("content"),
            course_dir=assignments_json_path.parent,
        )
    item["_canvas_sync"] = (
        item.get("_canvas_sync")
        if isinstance(item.get("_canvas_sync"), dict)
        else {}
    )
    item["_canvas_sync"]["status"] = "unchanged"
    return item


def existing_assignment_dir(
    existing_item: dict[str, Any] | None,
    assignments_json_path: Path,
) -> Path | None:
    if not existing_item:
        return None
    assignment_json_path = resolve_relative_path(
        assignments_json_path, existing_item.get("path")
    )
    if not assignment_json_path:
        return None
    return assignment_json_path.parent


def absolute_canvas_url(client: CanvasClient, url: str) -> str:
    return urljoin(client.base_url.rstrip("/") + "/", url)


def image_output_path(
    *,
    assignment_dir: Path,
    src: str,
    index: int,
    used_paths: set[Path],
) -> Path:
    parsed = urlparse(src)
    original_name = Path(unquote(parsed.path)).name
    if not original_name or "." not in original_name:
        original_name = f"image-{index}.bin"
    base_name = safe_path_segment(original_name, f"image-{index}.bin")
    candidate = assignment_dir / "images" / base_name
    if candidate.resolve() not in used_paths:
        used_paths.add(candidate.resolve())
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        deduped = candidate.with_name(f"{stem}-{counter}{suffix}")
        if deduped.resolve() not in used_paths:
            used_paths.add(deduped.resolve())
            return deduped
        counter += 1


def download_assignment_images(
    *,
    client: CanvasClient,
    body: str,
    assignment_json_path: Path,
    assignment_dir: Path,
    existing_images: list[dict[str, Any]],
    force: bool,
) -> tuple[str, list[dict[str, Any]]]:
    if not body:
        return body, []
    soup = BeautifulSoup(body, "html.parser")
    existing_by_src = {
        str(image.get("original_src")): image
        for image in existing_images
        if isinstance(image, dict) and image.get("original_src")
    }
    used_paths: set[Path] = set()
    images: list[dict[str, Any]] = []
    for index, image in enumerate(soup.select("img[src]"), start=1):
        src = str(image.get("src") or "")
        if not src:
            continue
        existing = existing_by_src.get(src)
        existing_path = (
            resolve_relative_path(assignment_json_path, existing.get("path"))
            if existing and isinstance(existing.get("path"), str)
            else None
        )
        if (
            existing
            and not force
            and existing_path
            and existing_path.exists()
            and existing.get("sha256")
        ):
            record = copy.deepcopy(existing)
            record["_canvas_sync"] = (
                record.get("_canvas_sync")
                if isinstance(record.get("_canvas_sync"), dict)
                else {}
            )
            record["_canvas_sync"]["status"] = "unchanged"
            image["src"] = record["path"]
            used_paths.add(existing_path.resolve())
            images.append(record)
            continue

        output_path = image_output_path(
            assignment_dir=assignment_dir,
            src=src,
            index=index,
            used_paths=used_paths,
        )
        record = {"original_src": src, "path": rel_path(assignment_json_path, output_path)}
        try:
            result = client.download_file(absolute_canvas_url(client, src), output_path)
        except CanvasAPIError as exc:
            record.update({"downloaded": False, "error": str(exc)})
        else:
            record.update(
                {
                    "downloaded": True,
                    "sha256": result["sha256"],
                    "bytes_downloaded": result["bytes"],
                    "download_content_type": result.get("content_type"),
                }
            )
            image["src"] = record["path"]
        record["_canvas_sync"] = {
            "signature": fingerprint(src),
            "status": topic_status(existing),
        }
        images.append(record)
    return str(soup), images


def submitted_attachment_key(attachment: dict[str, Any], index: int) -> str:
    if attachment.get("id") is not None:
        return str(attachment["id"])
    if attachment.get("url"):
        return str(attachment["url"])
    return f"attachment-{index}"


def submitted_file_record_base(attachment: dict[str, Any]) -> dict[str, Any]:
    return {
        key: attachment.get(key)
        for key in (
            "id",
            "folder_id",
            "display_name",
            "filename",
            "content-type",
            "size",
            "created_at",
            "updated_at",
            "modified_at",
            "unlock_at",
            "lock_at",
            "locked",
            "hidden",
            "locked_for_user",
            "hidden_for_user",
        )
        if key in attachment
    }


def collect_submission_attachments(value: Any) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    if isinstance(value, dict):
        raw_attachments = value.get("attachments")
        if isinstance(raw_attachments, list):
            attachments.extend(
                item for item in raw_attachments if isinstance(item, dict)
            )
        for item in value.values():
            attachments.extend(collect_submission_attachments(item))
    elif isinstance(value, list):
        for item in value:
            attachments.extend(collect_submission_attachments(item))
    return attachments


def annotate_submission_attachments(value: Any, records_by_key: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, list):
        return [annotate_submission_attachments(item, records_by_key) for item in value]
    if not isinstance(value, dict):
        return value

    result: dict[str, Any] = {}
    for key, item in value.items():
        if key == "attachments" and isinstance(item, list):
            annotated = []
            for index, attachment in enumerate(item, start=1):
                if not isinstance(attachment, dict):
                    annotated.append(attachment)
                    continue
                attachment_key = submitted_attachment_key(attachment, index)
                local_record = records_by_key.get(attachment_key)
                if local_record:
                    annotated.append(copy.deepcopy(local_record))
                else:
                    fallback = submitted_file_record_base(attachment)
                    fallback["downloaded"] = False
                    annotated.append(fallback)
            result[key] = annotated
        else:
            result[key] = annotate_submission_attachments(item, records_by_key)
    return result


def download_submitted_files(
    *,
    client: CanvasClient,
    submission: dict[str, Any] | None,
    assignment_json_path: Path,
    assignment_dir: Path,
    existing_files: list[dict[str, Any]],
    force: bool,
) -> tuple[Any, list[dict[str, Any]]]:
    if not isinstance(submission, dict):
        return submission, []
    existing_by_key = {
        str(item.get("id") or item.get("_canvas_sync", {}).get("attachment_key")): item
        for item in existing_files
        if isinstance(item, dict)
    }
    used_paths: set[Path] = set()
    records: list[dict[str, Any]] = []
    records_by_key: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for index, attachment in enumerate(collect_submission_attachments(submission), start=1):
        attachment_key = submitted_attachment_key(attachment, index)
        if attachment_key in seen:
            continue
        seen.add(attachment_key)
        signature = file_signature(attachment)
        existing = existing_by_key.get(attachment_key)
        existing_signature = (
            existing.get("_canvas_sync", {}).get("signature")
            if isinstance(existing, dict)
            else None
        )
        existing_path = (
            resolve_relative_path(assignment_json_path, existing.get("path"))
            if existing and isinstance(existing.get("path"), str)
            else None
        )
        if (
            existing
            and not force
            and existing_signature == signature
            and existing_path
            and existing_path.exists()
            and existing.get("sha256")
        ):
            record = copy.deepcopy(existing)
            record["_canvas_sync"]["status"] = "unchanged"
            used_paths.add(existing_path.resolve())
            records.append(record)
            records_by_key[attachment_key] = record
            continue

        output_path = unique_download_path(
            files_root=assignment_dir / "submitted_files",
            folder_path=Path(),
            file_item=attachment,
            used_paths=used_paths,
        )
        record = submitted_file_record_base(attachment)
        record["path"] = rel_path(assignment_json_path, output_path)
        try:
            result = download_with_fallbacks(client, attachment, output_path)
        except CanvasAPIError as exc:
            record.update(
                {
                    "downloaded": False,
                    "download_error": str(exc),
                    "sha256": existing.get("sha256") if existing else None,
                    "bytes_downloaded": 0,
                }
            )
            status = "error"
        else:
            record.update(
                {
                    "downloaded": True,
                    "sha256": result["sha256"],
                    "bytes_downloaded": result["bytes"],
                    "download_content_type": result.get("content_type"),
                }
            )
            status = topic_status(existing)
        record["_canvas_sync"] = {
            "attachment_key": attachment_key,
            "signature": signature,
            "status": status,
        }
        records.append(record)
        records_by_key[attachment_key] = record
    return annotate_submission_attachments(submission, records_by_key), records


def quiz_payload_for_assignment(
    *,
    client: CanvasClient,
    course_id: str,
    quiz_id: str,
    assignment_json_path: Path,
    force: bool,
    existing_quiz_path: str | None,
) -> dict[str, Any]:
    quiz_path = assignment_json_path.parent / "quiz.json"
    quiz_error = None
    questions_error = None
    quiz_detail: dict[str, Any] | None = None
    questions: list[dict[str, Any]] = []
    try:
        quiz_detail = client.course_quiz_detail(course_id, quiz_id)
    except CanvasAPIError as exc:
        quiz_error = str(exc)
    try:
        questions = client.course_quiz_questions(course_id, quiz_id)
    except CanvasAPIError as exc:
        questions_error = str(exc)

    payload = {
        "schema_version": 2,
        "content_type": "quiz",
        "quiz_id": quiz_id,
        "quiz": quiz_detail,
        "quiz_error": quiz_error,
        "questions_available": questions_error is None,
        "question_count": len(questions),
        "questions_error": questions_error,
        "questions": questions,
        "fingerprint": fingerprint(
            {
                "quiz": quiz_detail,
                "quiz_error": quiz_error,
                "questions": questions,
                "questions_error": questions_error,
            }
        ),
    }
    write_json(quiz_path, payload)
    return {
        "path": rel_path(assignment_json_path, quiz_path),
        "status": "updated" if existing_quiz_path else "created",
        "question_count": len(questions),
        "questions_available": questions_error is None,
        "questions_error": questions_error,
    }


def build_assignment_record(
    *,
    client: CanvasClient,
    course_id: str,
    assignments_json_path: Path,
    assignment_dir: Path,
    summary: dict[str, Any],
    existing_item: dict[str, Any] | None,
    synced_at: str,
    force: bool,
    quiz_summary: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    assignment_id = str(summary["id"])
    assignment_json_path = assignment_dir / "assignment.json"
    existing_payload = read_json(assignment_json_path)
    detail = copy.deepcopy(summary)
    detail_error = None
    try:
        detail = client.course_assignment_detail(course_id, assignment_id)
    except CanvasAPIError as exc:
        detail_error = str(exc)

    quiz_id = detail.get("quiz_id") or summary.get("quiz_id")
    quiz_detail_for_body = None
    if quiz_id is not None:
        try:
            quiz_detail_for_body = client.course_quiz_detail(course_id, quiz_id)
        except CanvasAPIError:
            quiz_detail_for_body = None

    body_parts = []
    assignment_body = detail.get("description")
    if isinstance(assignment_body, str) and assignment_body:
        body_parts.append(assignment_body)
    quiz_body = (
        quiz_detail_for_body.get("description")
        if isinstance(quiz_detail_for_body, dict)
        else None
    )
    if isinstance(quiz_body, str) and quiz_body and quiz_body not in body_parts:
        body_parts.append(quiz_body)
    body = "\n".join(body_parts)

    existing_images = (
        existing_payload.get("images")
        if isinstance(existing_payload, dict) and isinstance(existing_payload.get("images"), list)
        else []
    )
    body, images = download_assignment_images(
        client=client,
        body=body,
        assignment_json_path=assignment_json_path,
        assignment_dir=assignment_dir,
        existing_images=existing_images,
        force=force,
    )
    content_path = assignment_dir / "content.html"
    content_rel = rel_path(assignment_json_path, content_path)
    if body:
        write_html(content_path, str(detail.get("name") or summary.get("name")), body)
        detail["description"] = content_rel
        if isinstance(quiz_detail_for_body, dict) and quiz_detail_for_body.get("description"):
            quiz_detail_for_body["description"] = content_rel
    else:
        content_rel = None
        detail["description"] = None

    self_submission = None
    self_submission_error = None
    try:
        self_submission = client.assignment_self_submission(course_id, assignment_id)
    except CanvasAPIError as exc:
        self_submission_error = str(exc)

    submission_source = self_submission
    if not isinstance(submission_source, dict) and isinstance(detail.get("submission"), dict):
        submission_source = detail["submission"]
    existing_submitted = (
        existing_payload.get("submitted_files")
        if isinstance(existing_payload, dict)
        and isinstance(existing_payload.get("submitted_files"), list)
        else []
    )
    sanitized_submission, submitted_files = download_submitted_files(
        client=client,
        submission=submission_source,
        assignment_json_path=assignment_json_path,
        assignment_dir=assignment_dir,
        existing_files=existing_submitted,
        force=force,
    )
    if isinstance(self_submission, dict):
        self_submission = sanitized_submission
    if isinstance(detail.get("submission"), dict):
        detail["submission"] = annotate_submission_attachments(
            detail["submission"],
            {
                str(item.get("id") or item.get("_canvas_sync", {}).get("attachment_key")): item
                for item in submitted_files
            },
        )

    quiz_info = None
    if quiz_id is not None:
        existing_quiz_path = (
            existing_payload.get("quiz", {}).get("path")
            if isinstance(existing_payload, dict)
            and isinstance(existing_payload.get("quiz"), dict)
            else None
        )
        quiz_info = quiz_payload_for_assignment(
            client=client,
            course_id=course_id,
            quiz_id=str(quiz_id),
            assignment_json_path=assignment_json_path,
            force=force,
            existing_quiz_path=existing_quiz_path,
        )

    signature = assignment_summary_signature(summary, quiz_summary)
    file_ids = sorted(extract_file_ids_from_text(body))
    payload = {
        "schema_version": 2,
        "synced_at": synced_at,
        "course_id": course_id,
        "content_type": "assignment",
        "kind": "assignment",
        "id": int(assignment_id) if assignment_id.isdigit() else assignment_id,
        "name": detail.get("name") or summary.get("name"),
        "content": content_rel,
        "content_present": bool(body),
        "images": images,
        "referenced_file_ids": file_ids,
        "submitted_files": submitted_files,
        "self_submission": self_submission,
        "self_submission_error": self_submission_error,
        "assignment": detail,
        "assignment_detail_error": detail_error,
        "quiz": quiz_info,
        "change_detection": {
            "canvas_fields": "assignment list metadata, included submission summary, and quiz summary when available",
            "strategy": "skip detail fetch when signature matches and local files exist",
        },
        "fingerprint": fingerprint(
            {
                "signature": signature,
                "file_ids": file_ids,
                "submitted_files": [
                    item.get("sha256") for item in submitted_files if item.get("downloaded")
                ],
                "images": [item.get("sha256") for item in images if item.get("downloaded")],
                "quiz": quiz_info,
            }
        ),
        "_canvas_sync": {
            "signature": signature,
            "status": topic_status(existing_item),
        },
    }
    write_json(assignment_json_path, payload)
    item = {
        "key": assignment_item_key("assignment", assignment_id),
        "kind": "assignment",
        "id": payload["id"],
        "name": payload["name"],
        "path": rel_path(assignments_json_path, assignment_json_path),
        "content": rel_path(assignments_json_path, content_path) if body else None,
        "submission_types": detail.get("submission_types") or summary.get("submission_types"),
        "due_at": detail.get("due_at") or summary.get("due_at"),
        "points_possible": detail.get("points_possible") or summary.get("points_possible"),
        "quiz_id": quiz_id,
        "submitted_file_count": len(submitted_files),
        "referenced_file_ids": file_ids,
        "_canvas_sync": {
            "signature": signature,
            "status": topic_status(existing_item),
        },
    }
    return item, True


def build_standalone_quiz_record(
    *,
    client: CanvasClient,
    course_id: str,
    assignments_json_path: Path,
    assignment_dir: Path,
    quiz: dict[str, Any],
    existing_item: dict[str, Any] | None,
    synced_at: str,
    force: bool,
) -> tuple[dict[str, Any], bool]:
    quiz_id = str(quiz["id"])
    assignment_json_path = assignment_dir / "assignment.json"
    existing_payload = read_json(assignment_json_path)
    detail = copy.deepcopy(quiz)
    detail_error = None
    try:
        detail = client.course_quiz_detail(course_id, quiz_id)
    except CanvasAPIError as exc:
        detail_error = str(exc)

    body = detail.get("description") if isinstance(detail.get("description"), str) else ""
    existing_images = (
        existing_payload.get("images")
        if isinstance(existing_payload, dict) and isinstance(existing_payload.get("images"), list)
        else []
    )
    body, images = download_assignment_images(
        client=client,
        body=body,
        assignment_json_path=assignment_json_path,
        assignment_dir=assignment_dir,
        existing_images=existing_images,
        force=force,
    )
    content_path = assignment_dir / "content.html"
    content_rel = rel_path(assignment_json_path, content_path)
    if body:
        write_html(content_path, str(detail.get("title") or quiz.get("title")), body)
        detail["description"] = content_rel
    else:
        content_rel = None
        detail["description"] = None

    existing_quiz_path = (
        existing_payload.get("quiz", {}).get("path")
        if isinstance(existing_payload, dict)
        and isinstance(existing_payload.get("quiz"), dict)
        else None
    )
    quiz_info = quiz_payload_for_assignment(
        client=client,
        course_id=course_id,
        quiz_id=quiz_id,
        assignment_json_path=assignment_json_path,
        force=force,
        existing_quiz_path=existing_quiz_path,
    )
    signature = quiz_summary_signature(quiz)
    file_ids = sorted(extract_file_ids_from_text(body))
    payload = {
        "schema_version": 2,
        "synced_at": synced_at,
        "course_id": course_id,
        "content_type": "assignment",
        "kind": "quiz",
        "id": int(quiz_id) if quiz_id.isdigit() else quiz_id,
        "name": detail.get("title") or quiz.get("title"),
        "content": content_rel,
        "content_present": bool(body),
        "images": images,
        "referenced_file_ids": file_ids,
        "submitted_files": [],
        "quiz_detail_error": detail_error,
        "quiz_detail": detail,
        "quiz": quiz_info,
        "change_detection": {
            "canvas_fields": "quiz list metadata",
            "strategy": "skip detail fetch when signature matches and local files exist",
        },
        "fingerprint": fingerprint(
            {
                "signature": signature,
                "file_ids": file_ids,
                "images": [item.get("sha256") for item in images if item.get("downloaded")],
                "quiz": quiz_info,
            }
        ),
        "_canvas_sync": {
            "signature": signature,
            "status": topic_status(existing_item),
        },
    }
    write_json(assignment_json_path, payload)
    item = {
        "key": assignment_item_key("quiz", quiz_id),
        "kind": "quiz",
        "id": payload["id"],
        "name": payload["name"],
        "path": rel_path(assignments_json_path, assignment_json_path),
        "content": rel_path(assignments_json_path, content_path) if body else None,
        "due_at": detail.get("due_at") or quiz.get("due_at"),
        "points_possible": detail.get("points_possible") or quiz.get("points_possible"),
        "quiz_id": payload["id"],
        "submitted_file_count": 0,
        "referenced_file_ids": file_ids,
        "_canvas_sync": {
            "signature": signature,
            "status": topic_status(existing_item),
        },
    }
    return item, True


def sync_assignments(
    *,
    client: CanvasClient,
    course_id: str,
    course_dir: Path,
    tabs: list[dict[str, Any]],
    synced_at: str,
    force: bool,
) -> dict[str, Any]:
    json_path = content_file_path(course_dir, "assignments")
    assignments_root = json_path.parent
    existing = read_json(json_path)
    existing_by_key = existing_items_by_key(existing, "key")
    available_tabs = open_tab_ids(tabs)
    has_assignments_tab = "assignments" in available_tabs
    has_quizzes_tab = "quizzes" in available_tabs

    assignments: list[dict[str, Any]] = []
    assignments_error = None
    if has_assignments_tab:
        try:
            assignments = [
                item
                for item in client.course_assignments(course_id)
                if item.get("name") != "Roll Call Attendance"
            ]
        except CanvasAPIError as exc:
            assignments_error = str(exc)

    quizzes: list[dict[str, Any]] = []
    quizzes_error = None
    if has_quizzes_tab:
        try:
            quizzes = client.course_quizzes(course_id)
        except CanvasAPIError as exc:
            quizzes_error = str(exc)

    if not assignments and not quizzes:
        return {
            "available": has_assignments_tab or has_quizzes_tab,
            "checked": True,
            "fetched": False,
            "status": "closed" if not (has_assignments_tab or has_quizzes_tab) else "unchanged",
            "path": rel_path(course_dir / COURSE_METADATA_FILE, json_path),
            "count": 0,
            "assignments_error": assignments_error,
            "quizzes_error": quizzes_error,
        }

    quiz_by_id = {str(quiz.get("id")): quiz for quiz in quizzes if quiz.get("id") is not None}
    assignment_ids = {str(item.get("id")) for item in assignments if item.get("id") is not None}
    linked_quiz_ids = {
        str(item.get("quiz_id"))
        for item in assignments
        if item.get("quiz_id") is not None
    }
    changed = force or existing is None
    changed_items = 0
    submitted_file_count = 0
    image_count = 0
    items: list[dict[str, Any]] = []
    used_dirs: set[Path] = set()

    for assignment in assignments:
        assignment_id = str(assignment.get("id"))
        key = assignment_item_key("assignment", assignment_id)
        quiz_summary = quiz_by_id.get(str(assignment.get("quiz_id")))
        signature = assignment_summary_signature(assignment, quiz_summary)
        existing_item = existing_by_key.get(key)
        existing_signature = (
            existing_item.get("_canvas_sync", {}).get("signature")
            if isinstance(existing_item, dict)
            else None
        )
        if (
            existing_item
            and not force
            and existing_signature == signature
            and existing_assignment_payload_ok(existing_item, json_path)
        ):
            item = normalize_existing_assignment_item(existing_item, json_path)
            existing_dir = existing_assignment_dir(existing_item, json_path)
            if existing_dir:
                used_dirs.add(existing_dir.resolve())
            items.append(item)
            submitted_file_count += int(item.get("submitted_file_count") or 0)
            continue

        assignment_dir = existing_assignment_dir(existing_item, json_path)
        if assignment_dir:
            used_dirs.add(assignment_dir.resolve())
        else:
            assignment_dir = assignment_folder_for_item(
                assignments_root=assignments_root,
                title=assignment.get("name"),
                item_id=assignment_id,
                used_dirs=used_dirs,
            )
        item, _ = build_assignment_record(
            client=client,
            course_id=course_id,
            assignments_json_path=json_path,
            assignment_dir=assignment_dir,
            summary=assignment,
            existing_item=existing_item,
            synced_at=synced_at,
            force=force,
            quiz_summary=quiz_summary,
        )
        items.append(item)
        submitted_file_count += int(item.get("submitted_file_count") or 0)
        changed = True
        changed_items += 1

    for quiz in quizzes:
        quiz_id = str(quiz.get("id"))
        assignment_id = str(quiz.get("assignment_id") or "")
        if quiz_id in linked_quiz_ids or assignment_id in assignment_ids:
            continue
        key = assignment_item_key("quiz", quiz_id)
        signature = quiz_summary_signature(quiz)
        existing_item = existing_by_key.get(key)
        existing_signature = (
            existing_item.get("_canvas_sync", {}).get("signature")
            if isinstance(existing_item, dict)
            else None
        )
        if (
            existing_item
            and not force
            and existing_signature == signature
            and existing_assignment_payload_ok(existing_item, json_path)
        ):
            existing_dir = existing_assignment_dir(existing_item, json_path)
            if existing_dir:
                used_dirs.add(existing_dir.resolve())
            items.append(normalize_existing_assignment_item(existing_item, json_path))
            continue

        assignment_dir = existing_assignment_dir(existing_item, json_path)
        if assignment_dir:
            used_dirs.add(assignment_dir.resolve())
        else:
            assignment_dir = assignment_folder_for_item(
                assignments_root=assignments_root,
                title=quiz.get("title"),
                item_id=quiz_id,
                used_dirs=used_dirs,
            )
        item, _ = build_standalone_quiz_record(
            client=client,
            course_id=course_id,
            assignments_json_path=json_path,
            assignment_dir=assignment_dir,
            quiz=quiz,
            existing_item=existing_item,
            synced_at=synced_at,
            force=force,
        )
        items.append(item)
        changed = True
        changed_items += 1

    for item in items:
        assignment_json_path = resolve_relative_path(json_path, item.get("path"))
        payload = read_json(assignment_json_path) if assignment_json_path else None
        if payload:
            image_count += sum(1 for image in payload.get("images") or [] if image.get("downloaded"))

    fingerprint_value = fingerprint(
        [item.get("_canvas_sync", {}).get("signature") for item in items]
    )
    if existing and existing.get("fingerprint") != fingerprint_value:
        changed = True
    payload = {
        "schema_version": 2,
        "synced_at": synced_at,
        "course_id": course_id,
        "content_type": "assignments",
        "count": len(items),
        "assignment_count": len(assignments),
        "standalone_quiz_count": sum(1 for item in items if item.get("kind") == "quiz"),
        "ignored_roll_call": True,
        "submitted_file_count": submitted_file_count,
        "image_count": image_count,
        "assignments_available": has_assignments_tab,
        "quizzes_available": has_quizzes_tab,
        "assignments_error": assignments_error,
        "quizzes_error": quizzes_error,
        "fingerprint": fingerprint_value,
        "change_detection": {
            "canvas_fields": [
                "assignment updated_at/dates/points/submission_types/description",
                "included self-submission summary",
                "quiz list metadata when available",
            ],
            "strategy": "skip detail, quiz question, image, and submitted-file fetches when signatures match local files",
        },
        "items": items,
    }
    if changed:
        write_json(json_path, payload)
    return {
        "available": True,
        "checked": True,
        "fetched": changed,
        "status": "created" if existing is None else ("updated" if changed else "unchanged"),
        "path": rel_path(course_dir / COURSE_METADATA_FILE, json_path),
        "count": len(items),
        "changed_items": changed_items,
        "submitted_file_count": submitted_file_count,
    }


def sync_files(
    *,
    client: CanvasClient,
    course_id: str,
    course_dir: Path,
    tabs: list[dict[str, Any]],
    synced_at: str,
    force: bool,
) -> dict[str, Any]:
    json_path = content_file_path(course_dir, "files")
    files_root = json_path.parent
    existing = read_json(json_path)
    existing_by_id = existing_items_by_key(existing, "id", items_key="files")
    available_tabs = open_tab_ids(tabs)
    has_files_tab = "files" in available_tabs

    try:
        folders = client.course_folders(course_id)
    except CanvasAPIError:
        folders = []
    folders_by_id = {
        str(folder.get("id")): folder
        for folder in folders
        if folder.get("id") is not None
    }

    references = collect_referenced_file_ids(course_dir)
    discovered: dict[str, dict[str, Any]] = {}
    sources_by_id: dict[str, set[str]] = {
        file_id: set(sources) for file_id, sources in references.items()
    }
    course_files_error: str | None = None

    if has_files_tab:
        try:
            for file_item in client.course_files(course_id):
                if file_item.get("id") is None:
                    continue
                file_id = str(file_item["id"])
                discovered[file_id] = file_item
                sources_by_id.setdefault(file_id, set()).add("files_tab")
        except CanvasAPIError as exc:
            course_files_error = str(exc)

    for file_id in references:
        if file_id in discovered:
            continue
        try:
            discovered[file_id] = client.file_details(file_id)
        except CanvasAPIError:
            if file_id in existing_by_id:
                discovered[file_id] = copy.deepcopy(existing_by_id[file_id])
                sources_by_id.setdefault(file_id, set()).add("cached_reference")

    if not discovered:
        return {
            "available": has_files_tab,
            "checked": True,
            "fetched": False,
            "status": "closed" if not has_files_tab else "unchanged",
            "path": rel_path(course_dir / COURSE_METADATA_FILE, json_path),
            "count": 0,
        }

    used_paths = {json_path.resolve()}
    changed = force or existing is None
    changed_files = 0
    downloaded_files = 0
    items: list[dict[str, Any]] = []

    for file_id, file_item in sorted(
        discovered.items(),
        key=lambda pair: (str(pair[1].get("display_name") or ""), pair[0]),
    ):
        if file_item.get("id") is None:
            file_item["id"] = int(file_id) if file_id.isdigit() else file_id
        existing_item = existing_by_id.get(file_id)
        signature = file_signature(file_item)
        existing_signature = (
            existing_item.get("_canvas_sync", {}).get("signature")
            if isinstance(existing_item, dict)
            else None
        )
        sources = sources_by_id.get(file_id, set())

        existing_path = (
            resolve_relative_path(json_path, existing_item.get("path"))
            if existing_item and isinstance(existing_item.get("path"), str)
            else None
        )
        existing_ok = (
            existing_item is not None
            and existing_signature == signature
            and existing_path is not None
            and existing_path.exists()
            and bool(existing_item.get("sha256"))
        )
        if existing_ok and not force:
            items.append(
                file_record_from_existing(
                    existing_item,
                    sources=sources,
                    json_path=json_path,
                )
            )
            used_paths.add(existing_path.resolve())
            continue

        folder = folders_by_id.get(str(file_item.get("folder_id")))
        folder_path = folder_relative_path(folder)
        output_path = unique_download_path(
            files_root=files_root,
            folder_path=folder_path,
            file_item=file_item,
            used_paths=used_paths,
        )
        record = copy.deepcopy(file_item)
        record["sources"] = sorted(sources or {"file_reference"})
        record["canvas_folder"] = folder.get("full_name") if folder else None
        record["canvas_path"] = (
            (
                f"{record['canvas_folder']}/"
                f"{record.get('display_name') or record.get('filename')}"
            )
            if record.get("canvas_folder")
            else None
        )
        try:
            download_result = download_with_fallbacks(client, file_item, output_path)
        except CanvasAPIError as exc:
            record["downloaded"] = False
            record["download_error"] = str(exc)
            record["path"] = rel_path(json_path, output_path)
            record["sha256"] = existing_item.get("sha256") if existing_item else None
            record["bytes_downloaded"] = 0
            status = "error"
        else:
            record["downloaded"] = True
            record["path"] = rel_path(json_path, output_path)
            record["sha256"] = download_result["sha256"]
            record["bytes_downloaded"] = download_result["bytes"]
            record["download_content_type"] = download_result.get("content_type")
            record["download_url"] = download_result.get("download_url")
            downloaded_files += 1
            status = topic_status(existing_item)
        record["_canvas_sync"] = {
            "signature": signature,
            "status": status,
        }
        items.append(record)
        changed = True
        changed_files += 1

    fingerprint_value = fingerprint(
        [
            {
                "id": item.get("id"),
                "signature": item.get("_canvas_sync", {}).get("signature"),
                "sha256": item.get("sha256"),
                "path": item.get("path"),
            }
            for item in items
        ]
    )
    if existing and existing.get("fingerprint") != fingerprint_value:
        changed = True

    payload = {
        "schema_version": 2,
        "synced_at": synced_at,
        "course_id": course_id,
        "content_type": "files",
        "count": len(items),
        "downloaded_count": sum(1 for item in items if item.get("downloaded") is True),
        "course_files_available": has_files_tab,
        "course_files_error": course_files_error,
        "referenced_file_count": len(references),
        "fingerprint": fingerprint_value,
        "change_detection": {
            "canvas_fields": [
                "id",
                "folder_id",
                "display_name",
                "filename",
                "content-type",
                "size",
                "updated_at",
                "modified_at",
                "unlock_at",
                "lock_at",
                "locked",
                "hidden",
                "locked_for_user",
                "hidden_for_user",
            ],
            "local_hash": "sha256",
            "strategy": (
                "reuse local file when Canvas metadata signature and local "
                "SHA-256 are present"
            ),
        },
        "folders": folders,
        "files": items,
    }
    if changed:
        write_json(json_path, payload)
    return {
        "available": True,
        "checked": True,
        "fetched": changed,
        "status": "created"
        if existing is None
        else ("updated" if changed else "unchanged"),
        "path": rel_path(course_dir / COURSE_METADATA_FILE, json_path),
        "count": len(items),
        "changed_items": changed_files,
        "downloaded_items": downloaded_files,
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
        if (
            content_type == "assignments"
            and "assignments" not in available_tabs
            and "quizzes" not in available_tabs
        ):
            sections[content_type] = {
                "available": False,
                "checked": False,
                "fetched": False,
                "status": "closed",
                "path": None,
                "count": 0,
            }
            continue
        if content_type not in {"files", "assignments"} and tab_id not in available_tabs:
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
            elif content_type == "assignments":
                summary = sync_assignments(
                    client=client,
                    course_id=course_id,
                    course_dir=course_dir,
                    tabs=tabs,
                    synced_at=synced_at,
                    force=force,
                )
            elif content_type == "files":
                summary = sync_files(
                    client=client,
                    course_id=course_id,
                    course_dir=course_dir,
                    tabs=tabs,
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
    refresh_assignments: bool = False,
    refresh_files: bool = False,
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
        refresh_assignments=refresh_assignments,
        refresh_files=refresh_files,
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
