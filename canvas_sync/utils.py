from __future__ import annotations

import hashlib
import html
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from agent_for_nus.paths import canvas_data_dir
from tools.shared import atomic_write_text

try:
    from .client import CanvasAPIError, CanvasClient
except ImportError:
    from client import CanvasAPIError, CanvasClient

DEFAULT_BASE_URL = "https://canvas.nus.edu.sg"
DEFAULT_SITE_NAME = "nus_canvas"
DEFAULT_DATA_PATH = canvas_data_dir()
COURSE_METADATA_FILE = "course.json"
INDEX_FILE = "index.json"
STUDENT_FILE = "student.json"
CONTENT_FILES = {
    "announcements": Path("announcements") / "announcements.json",
    "discussions": Path("discussions") / "discussions.json",
    "people": Path("people.json"),
    "groups": Path("groups.json"),
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
    "groups": "people",
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
    r"(?P<url>(?:/api/v1)?(?:/courses/(?P<course_id>\d+))?/files/"
    r"(?P<file_id>\d+)(?:/(?:download|preview))?[^\"'<>\s)]*)"
)


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
    atomic_write_text(path, text)
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
    atomic_write_text(path, html_text)
    return True


def rel_path(from_json_path: Path, target_path: Path) -> str:
    return os.path.relpath(target_path, start=from_json_path.parent).replace(os.sep, "/")


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
    synced_at: str,
    items: list[dict[str, Any]],
    fingerprint_value: str,
) -> dict[str, Any]:
    return {
        "synced_at": synced_at,
        "course_id": course_id,
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


def item_has_html(existing_item: dict[str, Any] | None, json_path: Path, field: str) -> bool:
    if not existing_item:
        return False
    path = resolve_relative_path(json_path, str(existing_item.get(field) or ""))
    return bool(path and path.exists())


def replace_message_fields_with_path(value: Any, path: str, *, with_fragments: bool = False) -> Any:
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


def extract_file_references_from_text(text: str) -> list[dict[str, str | None]]:
    return [
        {
            "file_id": match.group("file_id"),
            "course_id": match.group("course_id"),
            "url": html.unescape(match.group("url")),
        }
        for match in CANVAS_FILE_LINK.finditer(text)
    ]


def extract_file_ids_from_text(text: str) -> set[str]:
    return {str(reference["file_id"]) for reference in extract_file_references_from_text(text)}


def extract_file_references_from_json_value(value: Any) -> list[dict[str, str | None]]:
    references: list[dict[str, str | None]] = []
    if isinstance(value, dict):
        if value.get("type") == "File" and value.get("content_id") is not None:
            references.append(
                {
                    "file_id": str(value["content_id"]),
                    "course_id": None,
                    "url": None,
                }
            )
        for item in value.values():
            references.extend(extract_file_references_from_json_value(item))
    elif isinstance(value, list):
        for item in value:
            references.extend(extract_file_references_from_json_value(item))
    elif isinstance(value, str):
        references.extend(extract_file_references_from_text(value))
    return references


def extract_file_ids_from_json_value(value: Any) -> set[str]:
    return {
        str(reference["file_id"]) for reference in extract_file_references_from_json_value(value)
    }


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
