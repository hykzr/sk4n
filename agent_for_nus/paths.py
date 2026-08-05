from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

from platformdirs import user_data_path

APP_NAME = "agent-for-nus"
HOME_ENV_VAR = "AGENT_FOR_NUS_HOME"
SESSION_DIR_ENV_VAR = "AGENT_FOR_NUS_SESSION_DIR"


def configured_path(env_var: str) -> Path | None:
    value = os.getenv(env_var)
    if not value:
        return None
    return Path(value).expanduser().resolve()


def home_dir() -> Path:
    """Return the stable root for mutable application data."""
    configured = configured_path(HOME_ENV_VAR)
    if configured is not None:
        return configured
    return user_data_path(APP_NAME, appauthor=False)


def sessions_dir() -> Path:
    """Return the directory containing saved browser authentication state."""
    return configured_path(SESSION_DIR_ENV_VAR) or home_dir() / "sessions"


def canvas_data_dir() -> Path:
    return home_dir() / "canvas"


def nusmods_data_dir() -> Path:
    return home_dir() / "nusmods"


def talent_connect_data_dir() -> Path:
    return home_dir() / "talent-connect"


def talent_connect_database_path() -> Path:
    return talent_connect_data_dir() / "talent_connect.sqlite3"


def ensure_private_directory(path: Path) -> Path:
    """Create a directory and restrict it to the current user where supported."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        with suppress(OSError):
            path.chmod(0o700)
    return path
