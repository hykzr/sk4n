from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sk4n.paths import ensure_private_directory
from sk4n.tools.shared import atomic_output_path, atomic_write_text


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_name(name: str, identifier: str, *, id_bytes: int = 8) -> str:
    """Bound portable filenames and distinguish equal/truncated subjects or names."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", ascii_name).strip("-_")[:40] or "untitled"
    return f"{stem}--{digest(identifier)[: id_bytes * 2]}"


def message_id(message: dict[str, Any]) -> str:
    identifier = message.get("ItemId", {}).get("Id")
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("Outlook message has no ItemId.Id.")
    return identifier


def canonical_immutable_id(value: Any) -> str | None:
    """Search's URL-safe immutable ID in the standard Exchange ID alphabet."""
    if not isinstance(value, str) or not value:
        return None
    # Outlook's search alphabet maps '-' to '/' and '_' to '+'.
    return value.replace("-", "/").replace("_", "+")


def revision(message: dict[str, Any]) -> dict[str, str]:
    result = {}
    change_key = message.get("ItemId", {}).get("ChangeKey")
    if change_key:
        result["ChangeKey"] = str(change_key)
    modified = message.get("LastModifiedTime") or message.get("DateTimeLastModified")
    if modified:
        result["LastModifiedTime"] = str(modified)
    return result


def same_revision(cached: dict[str, Any], current: dict[str, Any]) -> bool:
    old, new = revision(cached), revision(current)
    if old.get("ChangeKey") and new.get("ChangeKey"):
        # Search timestamps include fractions that GetItem rounds to seconds.
        # ChangeKey is the server's authoritative item revision.
        return old["ChangeKey"] == new["ChangeKey"]
    # Received/sent dates do not detect edits, draft updates, or read-state changes.
    shared = old.keys() & new.keys()
    return bool(shared) and all(old[key] == new[key] for key in shared)


def complete_message(message: dict[str, Any]) -> bool:
    body = message.get("Body")
    return (
        isinstance(body, dict)
        and isinstance(body.get("Value"), str)
        and not body.get("IsTruncated")
        and not message.get("IsBodyTruncated")
    )


class OutlookStore:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    @staticmethod
    def read(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    @staticmethod
    def write(path: Path, value: Any) -> None:
        ensure_private_directory(path.parent)
        atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", mode=0o600)

    def folders(self) -> list[dict[str, Any]] | None:
        value = self.read(self.root / "folders.json")
        return value if isinstance(value, list) else None

    def save_folders(self, folders: list[dict[str, Any]]) -> None:
        ensure_private_directory(self.root)
        self.write(self.root / "folders.json", folders)

    def _index(self) -> dict[str, str]:
        value = self.read(self.root / "emails" / "index.json")
        return value if isinstance(value, dict) else {}

    def message_path(self, identifier: str) -> Path | None:
        name = self._index().get(identifier)
        if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
            return None
        return self.root / "emails" / name / "message.json"

    def message(self, identifier: str) -> dict[str, Any] | None:
        path = self.message_path(identifier)
        value = self.read(path) if path else None
        if not isinstance(value, dict) or not isinstance(value.get("message"), dict):
            return None
        return value if value["message"].get("ItemId", {}).get("Id") == identifier else None

    def save_message(self, record: dict[str, Any]) -> Path:
        identifier = message_id(record["message"])
        index = self._index()
        path = self.message_path(identifier)
        if path is None:
            title = str(record["message"].get("Subject") or "message")
            owners = {
                name.casefold(): owner for owner, name in index.items() if isinstance(name, str)
            }
            for count in range(8, 33):
                name = safe_name(title, identifier, id_bytes=count)
                candidate = self.root / "emails" / name / "message.json"
                owner = owners.get(name.casefold())
                if owner is not None and owner != identifier:
                    continue
                if candidate.parent.exists():
                    existing = self.read(candidate)
                    if (
                        not isinstance(existing, dict)
                        or existing.get("message", {}).get("ItemId", {}).get("Id") != identifier
                    ):
                        continue
                path = candidate
                index[identifier] = name
                break
            else:
                raise ValueError("Cannot allocate a unique Outlook message cache name.")
        ensure_private_directory(self.root)
        ensure_private_directory(self.root / "emails")
        self.write(path, record)
        if index != self._index():
            self.write(self.root / "emails" / "index.json", index)
        return path

    def forget_message(self, identifier: str) -> None:
        """Remove a stale record from offline listings after a remote write."""
        index = self._index()
        name = index.pop(identifier, None)
        if name is None:
            return
        self.write(self.root / "emails" / "index.json", index)
        if isinstance(name, str) and Path(name).name == name and name not in {".", ".."}:
            (self.root / "emails" / name / "message.json").unlink(missing_ok=True)
            (self.root / "emails" / name / "preview.png").unlink(missing_ok=True)

    def invalidate_preview(self, identifier: str) -> None:
        record = self.message(identifier)
        path = self.message_path(identifier)
        if record is not None and "preview" in record:
            record.pop("preview")
            self.save_message(record)
        if path is not None:
            (path.parent / "preview.png").unlink(missing_ok=True)

    def forget_search_aliases(self, identifiers: set[str]) -> None:
        """Discard legacy search records keyed by folder-dependent REST IDs."""
        aliases = [
            message_id(record["message"])
            for record in self.records()
            if message_id(record["message"]) not in identifiers
            and canonical_immutable_id(record["message"].get("ImmutableId")) in identifiers
        ]
        for alias in aliases:
            self.forget_message(alias)

    def records(self) -> Iterator[dict[str, Any]]:
        """Iterate cached emails without a saved query or repeated index reads."""
        for identifier, name in self._index().items():
            if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
                continue
            value = self.read(self.root / "emails" / name / "message.json")
            if (
                isinstance(value, dict)
                and isinstance(value.get("message"), dict)
                and value["message"].get("ItemId", {}).get("Id") == identifier
            ):
                yield value

    def attachment_path(self, identifier: str, attachment_id: str, name: str) -> Path:
        path = self.message_path(identifier)
        if path is None:
            raise ValueError("Cache the message before its attachments.")
        # Keep ordinary file extensions, but never trust remote directory components.
        extension = Path(name.replace("\\", "/")).suffix
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,12}", extension):
            extension = ""
        records = (self.message(identifier) or {}).get("attachment_files", {})
        previous = records.get(attachment_id, {}).get("path")
        if isinstance(previous, str):
            candidate = path.parent / previous
            if candidate.parent == path.parent / "attachments":
                return candidate
        owners = {
            str(entry.get("path") or "").casefold(): owner
            for owner, entry in records.items()
            if isinstance(entry, dict)
        }
        # The extension is separate from the bounded title stem.
        title = name[: -len(extension)] if extension else name
        for count in range(8, 33):
            candidate = (
                path.parent
                / "attachments"
                / (safe_name(title, attachment_id, id_bytes=count) + extension)
            )
            relative = str(candidate.relative_to(path.parent))
            owner = owners.get(relative.casefold())
            if owner is not None and owner != attachment_id:
                continue
            if candidate.exists() and owner != attachment_id:
                continue
            return candidate
        raise ValueError("Cannot allocate a unique Outlook attachment cache name.")

    def save_attachment(self, path: Path, content: bytes) -> None:
        ensure_private_directory(path.parent)
        with atomic_output_path(path) as temporary:
            temporary.write_bytes(content)
            temporary.chmod(0o600)

    def cached_attachment(self, identifier: str, entry: dict[str, Any]) -> Path | None:
        path = self.message_path(identifier)
        relative = entry.get("path")
        if path is None or not isinstance(relative, str):
            return None
        candidate = (path.parent / relative).resolve()
        if not candidate.is_relative_to(path.parent.resolve()):
            return None
        if not candidate.is_file():
            return None
        if entry.get("size") is not None and candidate.stat().st_size != entry["size"]:
            return None
        return candidate
