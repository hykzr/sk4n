from __future__ import annotations

import asyncio
import sys
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from sk4n.tools.shared import load_session, save_session

OUTLOOK_HOSTS = frozenset(
    {"outlook.office.com", "outlook.office365.com", "outlook.cloud.microsoft"}
)
DEFAULT_BASE_URL = "https://outlook.cloud.microsoft/mail/"
DEFAULT_SITE_NAME = "nus_outlook"
DEFAULT_LOGIN_WAIT_SECONDS = 300


def validate_base_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in OUTLOOK_HOSTS
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Outlook URL must use HTTPS on a supported Microsoft Outlook host.")
    return url


def login_url(base_url: str, login_hint: str | None = None) -> str:
    validate_base_url(base_url)
    parts = urlsplit(base_url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in {"realm", "login_hint"}]
    if login_hint:
        query.append(("login_hint", login_hint))
    return urlunsplit(parts._replace(query=urlencode(query)))


async def capture_session_async(
    *,
    site_name: str,
    base_url: str,
    timeout: int = 30,
    login_wait_seconds: int = DEFAULT_LOGIN_WAIT_SECONDS,
    headed: bool = False,
    refresh: bool = False,
    login_hint: str | None = None,
) -> dict[str, Any]:
    """Let Outlook renew its own MSAL tokens; never handle passwords or MFA."""
    saved = None if refresh else load_session(site_name)
    if not login_hint and isinstance(saved, dict):
        login_hint = saved.get("login_hint")
    target = login_url(base_url, login_hint)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not headed)
        try:
            options: dict[str, Any] = {"viewport": {"width": 1440, "height": 950}}
            if isinstance(saved, dict) and isinstance(saved.get("storage_state"), dict):
                options["storage_state"] = saved["storage_state"]
            context = await browser.new_context(**options)
            page = await context.new_page()
            tokens: list[str] = []

            async def observe(request: Any) -> None:
                parts = urlsplit(request.url)
                if parts.hostname not in OUTLOOK_HOSTS or not parts.path.startswith("/owa/"):
                    return
                try:
                    value = (await request.all_headers()).get("authorization", "")
                    if value.lower().startswith("bearer "):
                        tokens.append(value[7:])
                except PlaywrightError:
                    pass

            page.on("request", observe)
            if headed:
                print(
                    "A NUS Outlook browser window is open. Complete sign-in and MFA there; login will be detected automatically.",
                    file=sys.stderr,
                    flush=True,
                )
            await page.goto(target, wait_until="domcontentloaded", timeout=timeout * 1000)
            deadline = asyncio.get_running_loop().time() + login_wait_seconds
            while asyncio.get_running_loop().time() < deadline:
                parts = urlsplit(page.url)
                if tokens and parts.hostname in OUTLOOK_HOSTS:
                    origin = f"https://{parts.hostname}"
                    try:
                        response = await context.request.post(
                            origin + "/owa/service.svc?action=GetOwaUserConfiguration",
                            headers={
                                "Authorization": "Bearer " + tokens[-1],
                                "Action": "GetOwaUserConfiguration",
                            },
                            data={
                                "__type": "GetOwaUserConfigurationJsonRequest:#Exchange",
                                "Header": {"RequestServerVersion": "V2018_01_08"},
                                "Body": {},
                            },
                            timeout=timeout * 1000,
                        )
                        profile = (
                            (await response.json()).get("SessionSettings", {})
                            if response.ok
                            else {}
                        )
                    except (PlaywrightError, ValueError):
                        profile = {}
                    if profile.get("UserEmailAddress"):
                        state = await context.storage_state(indexed_db=True)
                        result = {
                            "storage_state": state,
                            "access_token": tokens[-1],
                            "base_url": origin + "/mail/",
                            "login_hint": profile.get("UserPrincipalName") or login_hint or "",
                        }
                        save_session(site_name, result)
                        return result
                await page.wait_for_timeout(1000)
            raise TimeoutError(
                "Outlook login could not be renewed. Run `nus-outlook auth login` to complete NUS sign-in and MFA."
            )
        finally:
            await browser.close()


def capture_session(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(capture_session_async(**kwargs))
