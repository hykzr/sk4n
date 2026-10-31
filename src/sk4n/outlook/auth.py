from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from playwright.async_api import Error as PlaywrightError

from sk4n.tools.shared import delete_session, load_session

from .browser import (
    DEFAULT_BASE_URL,
    DEFAULT_LOGIN_WAIT_SECONDS,
    DEFAULT_SITE_NAME,
    capture_session,
)
from .client import OutlookAuthError, OutlookClient, OutlookError


@dataclass(frozen=True)
class AuthStatus:
    authenticated: bool
    name: str = ""
    email: str = ""
    user_id: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "authenticated": self.authenticated,
            "name": self.name or None,
            "email": self.email or None,
            "user_id": self.user_id or None,
            "error": self.error or None,
        }


def check_auth_status(
    *, site_name: str = DEFAULT_SITE_NAME, base_url: str = DEFAULT_BASE_URL, timeout: int = 30
) -> AuthStatus:
    if not load_session(site_name):
        return AuthStatus(False)
    try:
        profile = OutlookClient(site_name=site_name, base_url=base_url, timeout=timeout).profile()
    except (OutlookError, PlaywrightError, TimeoutError, OSError) as exc:
        return AuthStatus(False, error=str(exc))
    return AuthStatus(
        True,
        name=str(profile.get("UserDisplayName") or ""),
        email=str(profile.get("UserEmailAddress") or ""),
        user_id=str(profile.get("UserPrincipalName") or ""),
    )


def login(
    *,
    site_name: str = DEFAULT_SITE_NAME,
    base_url: str = DEFAULT_BASE_URL,
    timeout: int = 30,
    refresh: bool = False,
    login_wait_seconds: int = DEFAULT_LOGIN_WAIT_SECONDS,
    login_hint: str | None = None,
) -> AuthStatus:
    if not refresh:
        current = check_auth_status(site_name=site_name, base_url=base_url, timeout=timeout)
        if current.authenticated:
            return current
    capture_session(
        site_name=site_name,
        base_url=base_url,
        timeout=timeout,
        headed=True,
        refresh=refresh,
        login_wait_seconds=login_wait_seconds,
        login_hint=login_hint,
    )
    status = check_auth_status(site_name=site_name, base_url=base_url, timeout=timeout)
    if not status.authenticated:
        raise OutlookAuthError(
            status.error or "Outlook login completed, but the saved session is invalid."
        )
    return status


def logout(*, site_name: str = DEFAULT_SITE_NAME) -> str:
    return delete_session(site_name)
