from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path

from agent_for_nus.paths import ensure_private_directory, sessions_dir

SESSION_DIR = sessions_dir()
"""Default directory for persisted session data (cookies, localStorage, etc.)."""


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
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        if os.name == "posix":
            with suppress(OSError):
                path.chmod(0o600)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise
    finally:
        with suppress(OSError):
            temporary_path.unlink()
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
