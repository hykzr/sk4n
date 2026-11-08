from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Any

from .client import DISTINGUISHED_FOLDERS, OutlookClient, OutlookError
from .links import attachment_url, record_with_links, with_links
from .storage import OutlookStore, complete_message, message_id, revision, same_revision

FOLDER_ERRORS = frozenset(
    {"ErrorFolderNotFound", "ErrorItemNotFound", "ErrorInvalidIdMalformed", "HTTP404"}
)


def attachment_revision(attachment: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    if attachment.get("LastModifiedTime"):
        return {key: attachment.get(key) for key in ("LastModifiedTime", "Size", "__type")}
    return {"message": revision(message), "Size": attachment.get("Size")}


class OutlookFetcher:
    """Incremental mailbox reads; ``none`` never contacts the service or writes cache files."""

    def __init__(self, client: OutlookClient, store: OutlookStore):
        self.client, self.store = client, store

    def folders(self, *, refresh_mode: str = "default") -> list[dict[str, Any]]:
        if refresh_mode == "none":
            cached = self.store.folders()
            if cached is None:
                raise OutlookError("Folder mapping is not cached. Remove --no-refresh to fetch it.")
            return cached
        folders = self.client.folders()
        self.store.save_folders(folders)
        return folders

    def _folder_matches(self, value: str) -> list[str]:
        folders = self.store.folders() or []
        by_id = {f["FolderId"]["Id"]: f for f in folders if f.get("FolderId", {}).get("Id")}

        def folder_path(folder: dict[str, Any]) -> str:
            parts = [str(folder.get("DisplayName") or "")]
            seen = {folder["FolderId"]["Id"]}
            parent = folder.get("ParentFolderId", {}).get("Id")
            while parent in by_id and parent not in seen:
                seen.add(parent)
                ancestor = by_id[parent]
                parts.insert(0, str(ancestor.get("DisplayName") or ""))
                parent = ancestor.get("ParentFolderId", {}).get("Id")
            return "/".join(parts)

        return list(
            dict.fromkeys(
                f["FolderId"]["Id"]
                for f in by_id.values()
                if value.casefold()
                in {str(f.get("DisplayName") or "").casefold(), folder_path(f).casefold()}
            )
        )

    def _resolve_folder(self, value: str) -> str:
        matches = self._folder_matches(value)
        if len(matches) > 1:
            raise ValueError(
                f"Folder name {value!r} is ambiguous. Use its full folder path or --folder-id. "
                f"Matching IDs: {', '.join(matches)}"
            )
        if not matches:
            raise OutlookError(f"Folder {value!r} does not exist in the cached folder mapping.")
        return matches[0]

    def select_folder(
        self, folder: str | None, identifier: str | None, mode: str
    ) -> tuple[str, bool, bool]:
        if folder is not None and identifier is not None:
            raise ValueError("--folder and --folder-id are mutually exclusive.")
        if identifier is not None:
            if not identifier.strip():
                raise ValueError("Folder ID must not be empty.")
            return identifier, False, False
        folder = "inbox" if folder is None else folder
        if not folder.strip():
            raise ValueError("Folder must not be empty.")
        if mode == "force":
            self.folders()
        if folder.lower() in DISTINGUISHED_FOLDERS:
            return folder.lower(), False, mode == "force"
        matches = self._folder_matches(folder)
        if matches:
            return self._resolve_folder(folder), True, mode == "force"
        if mode == "default":
            self.folders()
            return self._resolve_folder(folder), True, True
        return self._resolve_folder(folder), True, mode == "force"

    def messages(
        self,
        *,
        folder: str | None = None,
        folder_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
        unread: bool = False,
        query: str | None = None,
        refresh_mode: str = "default",
        body: bool = False,
        attachments: bool = False,
    ) -> list[dict[str, Any]]:
        if limit < 1 or offset < 0 or (query is not None and not query.strip()):
            raise ValueError("Use a positive limit, nonnegative offset, and nonempty search query.")
        if refresh_mode == "none" and query is not None:
            raise ValueError(
                "Offline search is not supported. Use messages --no-refresh --format json with jq or grep."
            )
        selected, named, refreshed = self.select_folder(folder, folder_id, refresh_mode)
        if refresh_mode == "none":
            return self._offline_messages(
                selected,
                limit=limit,
                offset=offset,
                unread=unread,
                body=body,
                attachments=attachments,
            )
        try:
            items = self.client.messages(
                folder=selected,
                folder_is_id=named or folder_id is not None,
                limit=limit,
                offset=offset,
                unread=unread,
                query=query,
            )
        except OutlookError as exc:
            if not named or refreshed:
                raise
            code = exc.response_code
            if code == "HTTP400":
                # Search wraps invalid folder IDs in a generic BadRequest. Confirm
                # that the folder is missing before refreshing/retrying the query.
                try:
                    self.client.folder_metadata(selected)
                except OutlookError as folder_exc:
                    if folder_exc.response_code not in FOLDER_ERRORS:
                        raise
                    code = folder_exc.response_code
            if code not in FOLDER_ERRORS:
                raise
            self.folders()
            selected = self._resolve_folder(folder or "inbox")
            items = self.client.messages(
                folder=selected,
                folder_is_id=True,
                limit=limit,
                offset=offset,
                unread=unread,
                query=query,
            )
        result = []
        for item in items:
            record = self._cache_metadata(item, folder=selected)
            if body or attachments:
                result.append(
                    self.message(
                        message_id(item),
                        refresh_mode=refresh_mode,
                        attachments=attachments,
                        metadata=item,
                    )
                )
            else:
                result.append(self._result(record, item))
        immutable_ids = {
            message_id(item) for item in items if item.get("ImmutableId")
        }
        if immutable_ids:
            self.store.forget_search_aliases(immutable_ids)
        return result

    def _canonical_folder(self, selected: str) -> str:
        return next(
            (
                f["DistinguishedFolderId"]
                for f in self.store.folders() or []
                if f.get("FolderId", {}).get("Id") == selected
                and f.get("DistinguishedFolderId") in DISTINGUISHED_FOLDERS
            ),
            selected,
        )

    @staticmethod
    def _metadata(message: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in message.items() if key not in {"Body", "_outlook"}}

    def _cache_metadata(self, item: dict[str, Any], *, folder: str) -> dict[str, Any]:
        item = with_links(item, self.client.base_url)
        record = self.store.message(message_id(item)) or {
            "message": item,
            "complete": False,
            "attachment_files": {},
        }
        downloaded = bool(record.get("complete") and complete_message(record["message"]))
        current = bool(record.get("body_current", downloaded))
        if complete_message(item):
            record = {**record, "message": item, "complete": True}
            current = True
        elif downloaded:
            previous, latest = revision(record["message"]), revision(item)
            if previous.get("ChangeKey") and latest.get("ChangeKey"):
                current = previous["ChangeKey"] == latest["ChangeKey"]
            elif previous.get("LastModifiedTime") and latest.get("LastModifiedTime"):
                # Full-message dates may omit search's fractional seconds. Only
                # mark stale when a different whole second establishes a change.
                try:
                    old = datetime.fromisoformat(
                        previous["LastModifiedTime"].replace("Z", "+00:00")
                    )
                    new = datetime.fromisoformat(latest["LastModifiedTime"].replace("Z", "+00:00"))
                    if old.replace(microsecond=0) != new.replace(microsecond=0):
                        current = False
                except ValueError:
                    current = False
        else:
            record = {**record, "message": {**record["message"], **item}, "complete": False}
        record = {
            **record,
            "metadata": self._metadata(item),
            "folder": self._canonical_folder(folder),
            "body_current": current,
        }
        record = record_with_links(record, self.client.base_url)
        self.store.save_message(record)
        return record

    def _offline_messages(
        self, folder: str, *, limit: int, offset: int, unread: bool, body: bool, attachments: bool
    ) -> list[dict[str, Any]]:
        selected = self._canonical_folder(folder)
        records = []
        for record in self.store.records():
            metadata = record.get("metadata") or self._metadata(record["message"])
            parent = record.get("folder") or metadata.get("ParentFolderId", {}).get("Id")
            if not parent or self._canonical_folder(parent) != selected:
                continue
            if unread and metadata.get("IsRead") is not False:
                continue
            records.append((record, metadata))
        records.sort(
            key=lambda pair: (str(pair[1].get("DateTimeReceived") or ""), message_id(pair[1])),
            reverse=True,
        )
        return [
            self.message(message_id(metadata), refresh_mode="none", attachments=attachments)
            if body or attachments
            else self._result(record, metadata)
            for record, metadata in records[offset : offset + limit]
        ]

    def _result(self, record: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
        value = with_links(value, self.client.base_url)
        downloaded = bool(record.get("complete") and complete_message(record["message"]))
        current = bool(record.get("body_current", downloaded))
        paths = (
            self._attachments(message_id(value), record, mode="none", download=False)
            if current
            else []
        )
        return {
            **value,
            "_outlook": {
                "path": str(self.store.message_path(message_id(value))),
                "body_downloaded": downloaded,
                "body_current": current,
                "attachments": paths,
            },
        }

    def refresh_after_write(
        self,
        identifier: str,
        *,
        previous_id: str | None = None,
        folder_override: str | None = None,
    ) -> dict[str, Any]:
        """Read the authoritative post-write item and replace its cached state."""
        old_id = previous_id or identifier
        # Every successful named write invalidates the screenshot, even when
        # Outlook leaves ChangeKey unchanged (e.g. read/flag state changes).
        self.store.invalidate_preview(old_id)
        previous = self.store.message(old_id)
        try:
            full = self.client.message(identifier)
            if message_id(full) != identifier:
                raise OutlookError("Outlook returned a different message ID after the write.")
        except Exception:
            self.store.forget_message(old_id)
            raise
        full = with_links(full, self.client.base_url)
        parent = full.get("ParentFolderId", {}).get("Id")
        previous_parent = (previous or {}).get("metadata", {}).get("ParentFolderId", {}).get("Id")
        folder = folder_override or (
            (previous or {}).get("folder")
            if parent and parent == previous_parent and (previous or {}).get("folder")
            else self._canonical_folder(parent) if parent else None
        )
        record = {
            **(previous or {}),
            "message": full,
            "metadata": self._metadata(full),
            "complete": complete_message(full),
            "body_current": complete_message(full),
            "folder": folder,
            "attachment_files": (previous or {}).get("attachment_files", {})
            if old_id == identifier
            else {},
        }
        self.store.save_message(record)
        self.store.forget_search_aliases({identifier})
        if old_id != identifier:
            self.store.forget_message(old_id)
        return self._result(record, full)

    def message(
        self,
        identifier: str,
        *,
        refresh_mode: str = "default",
        attachments: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not identifier:
            raise ValueError("Message ID must not be empty.")
        record = self.store.message(identifier)
        summary = metadata or (record or {}).get("metadata") or {}
        is_complete = bool(
            record and record.get("complete") and complete_message(record["message"])
        )
        if refresh_mode == "none":
            if not is_complete:
                raise OutlookError(
                    "Complete message is not cached. Remove --no-refresh to fetch it."
                )
            if record is not None and record.get("body_current") is False:
                raise OutlookError(
                    "Cached body is from an older message revision. Remove --no-refresh to fetch it."
                )
        else:
            cached = record["message"] if record else {}
            current = metadata
            if (
                record
                and is_complete
                and refresh_mode != "force"
                and (
                    current is None
                    or (
                        revision(cached).get("ChangeKey") and not revision(current).get("ChangeKey")
                    )
                    or not revision(current).keys() & revision(cached).keys()
                )
            ):
                current = {**(current or {}), **self.client.message_metadata(identifier)}
            if (
                record is None
                or not is_complete
                or refresh_mode == "force"
                or not same_revision(cached, current or {})
            ):
                full = self.client.message(identifier)
                if message_id(full) != identifier:
                    raise OutlookError("Outlook returned a different message ID.")
                full = with_links({**summary, **full}, self.client.base_url)
                record = {
                    **(record or {}),
                    "message": full,
                    "metadata": self._metadata(full),
                    "complete": complete_message(full),
                    "body_current": complete_message(full),
                    "attachment_files": (record or {}).get("attachment_files", {}),
                }
                if full.get("ParentFolderId", {}).get("Id"):
                    record["folder"] = self._canonical_folder(full["ParentFolderId"]["Id"])
                self.store.save_message(record)
            elif summary:
                # Lists/search may expose extra fields absent from GetItem. Keep them
                # without replacing complete bodies or attachment metadata with previews.
                merged = {
                    **cached,
                    **{
                        k: v
                        for k, v in summary.items()
                        if k not in {"Body", "Attachments", "ItemId", "_outlook"}
                    },
                    "ItemId": {**summary.get("ItemId", {}), **cached["ItemId"]},
                }
                if merged != cached:
                    record = {
                        **record,
                        "message": merged,
                        "metadata": self._metadata(merged),
                        "body_current": True,
                    }
                    self.store.save_message(record)
            if (
                record is not None
                and record.get("complete")
                and record.get("body_current") is False
            ):
                record = {**record, "body_current": True}
                self.store.save_message(record)
        assert record is not None
        linked = record_with_links(record, self.client.base_url)
        if refresh_mode != "none" and linked != record:
            self.store.save_message(linked)
        record = linked
        paths = self._attachments(identifier, record, mode=refresh_mode, download=attachments)
        result = self._result(record, record["message"])
        result["_outlook"]["attachments"] = paths
        return result

    def _attachments(
        self, identifier: str, record: dict[str, Any], *, mode: str, download: bool
    ) -> list[dict[str, Any]]:
        result = []
        message = record["message"]
        files = record.setdefault("attachment_files", {})
        for attachment in message.get("Attachments", []):
            aid = attachment.get("AttachmentId", {}).get("Id")
            if not aid:
                if download:
                    raise OutlookError("Outlook returned attachment metadata without an ID.")
                continue
            entry = files.get(aid, {})
            url = attachment_url(identifier, aid, self.client.base_url)
            version = attachment_revision(attachment, message)
            path = self.store.cached_attachment(identifier, entry)
            valid = path is not None and entry.get("revision") == version
            reference = attachment.get("__type") == "ReferenceAttachment:#Exchange"
            if download and not reference and (not valid or mode == "force"):
                if mode == "none":
                    raise OutlookError(
                        f"Attachment {attachment.get('Name', aid)!r} is not cached for this revision. "
                        "Remove --no-refresh and use --attachments to fetch it."
                    )
                payload = self.client.attachment(aid)
                content, suffix = attachment_content(payload)
                name = str(attachment.get("Name") or payload.get("Name") or "attachment") + suffix
                path = self.store.attachment_path(identifier, aid, name)
                self.store.save_attachment(path, content)
                message_path = self.store.message_path(identifier)
                assert message_path is not None
                files[aid] = {
                    "path": str(path.relative_to(message_path.parent)),
                    "revision": version,
                    "size": len(content),
                    "url": url,
                    "metadata": {
                        **{k: v for k, v in payload.items() if k not in {"Content", "Item"}},
                        "url": url,
                    },
                }
                self.store.save_message(record)
                valid = True
            result.append(
                {
                    "id": aid,
                    "name": attachment.get("Name"),
                    "url": url,
                    "status": "reference" if reference else ("cached" if valid else "metadata"),
                    "path": str(path) if valid else None,
                }
            )
        return result


def attachment_content(payload: dict[str, Any]) -> tuple[bytes, str]:
    """File bytes, or an attached message's MIME/JSON representation."""
    content = payload.get("Content")
    if isinstance(content, str):
        try:
            return base64.b64decode(content, validate=True), ""
        except (ValueError, binascii.Error) as exc:
            raise OutlookError("Outlook returned invalid base64 attachment content.") from exc
    item = payload.get("Item")
    if isinstance(item, dict):
        mime = item.get("MimeContent", {}).get("Value")
        if isinstance(mime, str):
            return attachment_content({"Content": mime})[0], ".eml"
        return (json.dumps(item, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), ".json"
    raise OutlookError("Outlook returned an attachment without downloadable content.")
