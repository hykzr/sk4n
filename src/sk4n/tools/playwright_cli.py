from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from sk4n.errors import ExitCode

from .shared import load_session


class PlaywrightCLIError(RuntimeError):
    """Raised when an authenticated @playwright/cli session cannot be opened."""

    exit_code = ExitCode.TRANSPORT


def playwright_cli_executable() -> str:
    executable = shutil.which("playwright-cli")
    if not executable:
        raise PlaywrightCLIError(
            "@playwright/cli is not installed or `playwright-cli` is not on PATH."
        )
    return executable


def _run(executable: str, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [executable, *arguments],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as exc:
        raise PlaywrightCLIError(f"Could not run @playwright/cli: {exc}") from exc


def _failure_message(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or f"exit code {result.returncode}").strip()


def ensure_session_available(executable: str, session_id: str) -> None:
    result = _run(executable, ["list", "--json"])
    if result.returncode:
        raise PlaywrightCLIError(
            f"Could not list @playwright/cli sessions: {_failure_message(result)}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PlaywrightCLIError("@playwright/cli returned an invalid session list.") from exc
    browsers = payload.get("browsers") if isinstance(payload, Mapping) else None
    if not isinstance(browsers, list):
        raise PlaywrightCLIError("@playwright/cli returned an invalid session list.")
    if any(
        isinstance(browser, Mapping) and browser.get("name") == session_id for browser in browsers
    ):
        raise PlaywrightCLIError(
            f"@playwright/cli session {session_id!r} already exists; close it first."
        )


def _storage_state(site_name: str) -> dict[str, Any]:
    saved = load_session(site_name)
    if not isinstance(saved, Mapping):
        raise PlaywrightCLIError(f"No saved authenticated session exists for {site_name!r}.")
    state = saved.get("storage_state")
    if isinstance(state, Mapping):
        return dict(state)
    cookies = saved.get("cookies")
    if isinstance(cookies, list):
        return {"cookies": cookies, "origins": []}
    raise PlaywrightCLIError(
        f"The saved session for {site_name!r} has no Playwright storage state."
    )


def _close_new_session(executable: str, session_id: str) -> None:
    with suppress(PlaywrightCLIError):
        _run(executable, [f"-s={session_id}", "close"])


def open_authenticated_session(
    *,
    executable: str,
    session_id: str,
    site_name: str,
    url: str,
    headed: bool = False,
) -> None:
    """Open a new CLI session, inject saved auth state, and leave it running."""
    ensure_session_available(executable, session_id)
    state = _storage_state(site_name)
    open_arguments = [f"-s={session_id}", "open", url]
    if headed:
        open_arguments.append("--headed")

    open_attempted = False
    try:
        open_attempted = True
        result = _run(executable, open_arguments)
        if result.returncode:
            raise PlaywrightCLIError(
                f"Could not open @playwright/cli session {session_id!r}: {_failure_message(result)}"
            )
        descriptor, state_name = tempfile.mkstemp(
            prefix="sk4n-playwright-state-",
            suffix=".json",
        )
        state_path = Path(state_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                json.dump(state, state_file, ensure_ascii=False)
                state_file.flush()
            result = _run(
                executable,
                [f"-s={session_id}", "state-load", str(state_path.resolve())],
            )
        finally:
            with suppress(OSError):
                state_path.unlink()
        if result.returncode:
            raise PlaywrightCLIError(
                f"Could not inject authentication into @playwright/cli session "
                f"{session_id!r}: {_failure_message(result)}"
            )

        result = _run(executable, [f"-s={session_id}", "goto", url])
        if result.returncode:
            raise PlaywrightCLIError(
                f"Could not navigate authenticated @playwright/cli session {session_id!r}: "
                f"{_failure_message(result)}"
            )
    except Exception:
        if open_attempted:
            _close_new_session(executable, session_id)
        raise
