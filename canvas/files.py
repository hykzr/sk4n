from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

try:
    from .client import CanvasAPIError, CanvasClient
    from .utils import (
        COURSE_METADATA_FILE,
        content_file_path,
        download_with_fallbacks,
        existing_items_by_key,
        extract_file_references_from_json_value,
        extract_file_references_from_text,
        file_signature,
        fingerprint,
        normalize_existing_path,
        open_tab_ids,
        path_is_relative_to,
        read_json,
        rel_path,
        resolve_relative_path,
        safe_path_segment,
        unique_download_path,
        write_json,
    )
except ImportError:
    from client import CanvasAPIError, CanvasClient
    from utils import (
        COURSE_METADATA_FILE,
        content_file_path,
        download_with_fallbacks,
        existing_items_by_key,
        extract_file_references_from_json_value,
        extract_file_references_from_text,
        file_signature,
        fingerprint,
        normalize_existing_path,
        open_tab_ids,
        path_is_relative_to,
        read_json,
        rel_path,
        resolve_relative_path,
        safe_path_segment,
        unique_download_path,
        write_json,
    )


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


def collect_referenced_files(
    course_dir: Path,
    course_id: str,
) -> dict[str, dict[str, set[str]]]:
    files_root = course_dir / "files"
    references: dict[str, dict[str, set[str]]] = {}
    for path in course_dir.rglob("*"):
        if not path.is_file() or path_is_relative_to(path, files_root):
            continue
        if path.suffix.lower() not in {".json", ".html", ".htm"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        found = extract_file_references_from_text(text)
        if path.suffix.lower() == ".json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
            found.extend(extract_file_references_from_json_value(data))
        if not found:
            continue
        source = source_name_for_path(course_dir, path)
        for reference in found:
            referenced_course_id = reference.get("course_id")
            if referenced_course_id and str(referenced_course_id) != str(course_id):
                continue
            file_id = str(reference["file_id"])
            record = references.setdefault(file_id, {"sources": set(), "urls": set()})
            record["sources"].add(source)
            if reference.get("url"):
                record["urls"].add(str(reference["url"]))
    return references


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
    return item


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
        str(folder.get("id")): folder for folder in folders if folder.get("id") is not None
    }

    references = collect_referenced_files(course_dir, course_id)
    discovered: dict[str, dict[str, Any]] = {}
    sources_by_id: dict[str, set[str]] = {
        file_id: set(reference["sources"]) for file_id, reference in references.items()
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
        reference_urls = sorted(
            {client.api_url(url) for url in references[file_id]["urls"] if isinstance(url, str)}
        )
        if not reference_urls:
            reference_urls = [client.api_url(f"/courses/{course_id}/files/{file_id}")]
        try:
            detail = client.file_details(file_id)
        except CanvasAPIError as exc:
            if file_id in existing_by_id:
                detail = copy.deepcopy(existing_by_id[file_id])
            else:
                detail = {
                    "id": int(file_id) if file_id.isdigit() else file_id,
                    "display_name": f"Canvas file {file_id}",
                    "downloaded": False,
                }
            detail["inaccessible"] = True
            detail["access_error"] = str(exc)
            detail["reference_urls"] = reference_urls
            if reference_urls:
                detail["url"] = reference_urls[0]
        else:
            if reference_urls:
                detail["reference_urls"] = reference_urls
                detail["_canvas_reference_urls"] = reference_urls
        discovered[file_id] = detail

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
            existing_item.get("_canvas", {}).get("signature")
            if isinstance(existing_item, dict)
            else None
        )
        sources = sources_by_id.get(file_id, set())

        if file_item.get("inaccessible") is True:
            record = copy.deepcopy(existing_item) if existing_item else {}
            record.update(copy.deepcopy(file_item))
            record["sources"] = sorted(sources or {"file_reference"})
            existing_target_path = (
                str(existing_item["path"])
                if isinstance(existing_item, dict) and isinstance(existing_item.get("path"), str)
                else None
            )
            existing_path = resolve_relative_path(json_path, existing_target_path)
            if (
                existing_path is not None
                and existing_path.exists()
                and existing_target_path is not None
            ):
                record["path"] = normalize_existing_path(
                    json_path=json_path,
                    target_path=existing_target_path,
                    course_dir=json_path.parent,
                )
                record["downloaded"] = True
            else:
                record.pop("path", None)
                record["downloaded"] = False
            record["_canvas"] = {"signature": signature}
            items.append(record)
            if existing_signature != signature or existing_item != record:
                changed = True
                changed_files += 1
            continue

        existing_path = (
            resolve_relative_path(json_path, existing_item.get("path"))
            if existing_item and isinstance(existing_item.get("path"), str)
            else None
        )
        if (
            existing_item is not None
            and existing_signature == signature
            and existing_path is not None
            and existing_path.exists()
            and bool(existing_item.get("sha256"))
        ) and not force:
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
            (f"{record['canvas_folder']}/{record.get('display_name') or record.get('filename')}")
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
        else:
            record["downloaded"] = True
            record["path"] = rel_path(json_path, output_path)
            record["sha256"] = download_result["sha256"]
            record["bytes_downloaded"] = download_result["bytes"]
            record["download_content_type"] = download_result.get("content_type")
            record["download_url"] = download_result.get("download_url")
            downloaded_files += 1
        record["_canvas"] = {
            "signature": signature,
        }
        items.append(record)
        changed = True
        changed_files += 1

    fingerprint_value = fingerprint(
        [
            {
                "id": item.get("id"),
                "signature": item.get("_canvas", {}).get("signature"),
                "sha256": item.get("sha256"),
                "path": item.get("path"),
            }
            for item in items
        ]
    )
    if existing and existing.get("fingerprint") != fingerprint_value:
        changed = True

    payload = {
        "synced_at": synced_at,
        "course_id": course_id,
        "count": len(items),
        "downloaded_count": sum(1 for item in items if item.get("downloaded") is True),
        "course_files_available": has_files_tab,
        "course_files_error": course_files_error,
        "referenced_file_count": len(references),
        "fingerprint": fingerprint_value,
        "folders": folders,
        "files": items,
    }
    if changed:
        write_json(json_path, payload)
    return {
        "available": True,
        "checked": True,
        "fetched": changed,
        "status": ("created" if existing is None else ("updated" if changed else "unchanged")),
        "path": rel_path(course_dir / COURSE_METADATA_FILE, json_path),
        "count": len(items),
        "changed_items": changed_files,
        "downloaded_items": downloaded_files,
    }
