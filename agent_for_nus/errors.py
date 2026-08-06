from __future__ import annotations

from enum import IntEnum

from playwright.async_api import Error as PlaywrightError


class ExitCode(IntEnum):
    """Stable process exit codes shared by the service CLIs."""

    SUCCESS = 0
    AUTH_REQUIRED = 1
    VALIDATION = 2
    AUTH = 3
    TRANSPORT = 4
    REMOTE = 5


def exit_code_for_error(error: BaseException) -> int:
    """Map a handled CLI exception to the public exit-code contract."""
    explicit = getattr(error, "exit_code", None)
    if isinstance(explicit, int):
        return explicit
    if isinstance(error, ValueError):
        return ExitCode.VALIDATION
    if isinstance(error, TimeoutError):
        return ExitCode.AUTH
    if isinstance(error, (OSError, PlaywrightError)):
        return ExitCode.TRANSPORT
    return ExitCode.REMOTE
