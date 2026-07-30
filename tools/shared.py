from __future__ import annotations

import json
import re
from pathlib import Path

SESSION_DIR = Path(__file__).resolve().parent.parent / "sessions"
"""Default directory for persisted session data (cookies, localStorage, etc.)."""


def truncate_text(text: str, max_len: int = 4000) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n... (truncated, {len(text)} chars total)"


def clean_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def session_path(site_name: str) -> Path:
    """Return the path for a site's session file."""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return SESSION_DIR / f"{site_name}.json"


def save_session(site_name: str, data: dict) -> str:
    """Persist session data (cookies, localStorage, etc.) to disk."""
    path = session_path(site_name)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
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
