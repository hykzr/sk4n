from __future__ import annotations

import importlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from sk4n.paths import ensure_private_directory, sessions_dir

SESSION_DIR = sessions_dir()
"""Default directory for persisted session data (cookies, localStorage, etc.)."""

_FCNTL: Any | None = importlib.import_module("fcntl") if os.name == "posix" else None
_MSVCRT: Any | None = importlib.import_module("msvcrt") if os.name == "nt" else None


@contextmanager
def atomic_output_path(path: Path) -> Iterator[Path]:
    """Yield a process-unique sibling path and atomically publish it on success."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        yield temporary_path
        os.replace(temporary_path, path)
    finally:
        with suppress(OSError):
            temporary_path.unlink()


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write UTF-8 text through a process-unique file and atomically replace *path*."""
    with atomic_output_path(path) as temporary_path:
        with temporary_path.open("w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None and os.name == "posix":
            temporary_path.chmod(mode)


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Serialize cache mutations across CLI processes using a stable lock file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if _FCNTL is not None:
            _FCNTL.flock(stream.fileno(), _FCNTL.LOCK_EX)
        elif _MSVCRT is not None:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            _MSVCRT.locking(stream.fileno(), _MSVCRT.LK_LOCK, 1)
        try:
            yield
        finally:
            if _FCNTL is not None:
                _FCNTL.flock(stream.fileno(), _FCNTL.LOCK_UN)
            elif _MSVCRT is not None:
                stream.seek(0)
                _MSVCRT.locking(stream.fileno(), _MSVCRT.LK_UNLCK, 1)


def truncate_text(text: str, max_len: int = 4000) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n... (truncated, {len(text)} chars total)"


def clean_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def session_path(site_name: str) -> Path:
    """Return the path for a site's session file."""
    if (
        not site_name
        or site_name in {".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", site_name)
    ):
        raise ValueError("site_name must be a simple file-safe name")
    directory = ensure_private_directory(sessions_dir())
    return directory / f"{site_name}.json"


def save_session(site_name: str, data: dict) -> str:
    """Persist session data (cookies, localStorage, etc.) to disk."""
    path = session_path(site_name)
    with (
        atomic_output_path(path) as temporary_path,
        temporary_path.open("w", encoding="utf-8") as stream,
    ):
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
        if os.name == "posix":
            temporary_path.chmod(0o600)
    if os.name == "posix":
        with suppress(OSError):
            path.chmod(0o600)
    return f"Session saved to {path}"


def load_session(site_name: str) -> dict | None:
    """Load previously persisted session data."""
    path = session_path(site_name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def has_session(site_name: str) -> bool:
    """Check whether a persisted session exists for *site_name*."""
    return session_path(site_name).exists()


def delete_session(site_name: str) -> str:
    """Delete a persisted session file."""
    path = session_path(site_name)
    if path.exists():
        path.unlink()
        return f"Session deleted: {path}"
    return f"No session file found for {site_name!r}"
