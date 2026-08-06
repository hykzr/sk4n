from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from playwright.async_api import async_playwright

from tools.shared import delete_session, load_session, save_session

from .client import DEFAULT_APP_BASE_URL

DEFAULT_SITE_NAME = "nus_talent_connect"
DEFAULT_LOGIN_WAIT_SECONDS = 300


@dataclass
class AuthStatus:
    authenticated: bool
    display_name: str = ""
    email: str = ""
    user_id: str = ""
    error: str = ""


async def _profile_from_page(page: Any) -> dict[str, Any] | None:
    try:
        await page.wait_for_function(
            "() => Boolean(window.$nuxt && window.$nuxt.$axios)",
            timeout=20_000,
        )
        result = await page.evaluate(
            """
            async () => {
                try {
                    const response = await window.$nuxt.$axios.$get('/api/auth/');
                    return {ok: true, data: response.data || null};
                } catch (error) {
                    return {
                        ok: false,
                        status: error.response && error.response.status,
                        message: String(error.message || error)
                    };
                }
            }
            """
        )
    except Exception:
        return None
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    data = result.get("data")
    return data if isinstance(data, dict) and data.get("_id") else None


def _status_from_profile(profile: dict[str, Any] | None) -> AuthStatus:
    if not profile:
        return AuthStatus(authenticated=False)
    display_name = str(
        profile.get("display_name") or profile.get("full_name") or profile.get("fullname") or ""
    )
    return AuthStatus(
        authenticated=True,
        display_name=display_name,
        email=str(profile.get("email") or ""),
        user_id=str(profile.get("_id") or ""),
    )


async def check_auth_status_async(
    *,
    site_name: str = DEFAULT_SITE_NAME,
    app_base_url: str = DEFAULT_APP_BASE_URL,
) -> AuthStatus:
    session = load_session(site_name)
    if not session or "storage_state" not in session:
        return AuthStatus(authenticated=False)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(
                storage_state=session["storage_state"],
                viewport={"width": 1440, "height": 950},
            )
            page = await context.new_page()
            await page.goto(
                f"{app_base_url.rstrip('/')}/jobs",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            profile = await _profile_from_page(page)
            await browser.close()
            return _status_from_profile(profile)
    except Exception as exc:
        return AuthStatus(authenticated=False, error=str(exc))


def check_auth_status(
    *,
    site_name: str = DEFAULT_SITE_NAME,
    app_base_url: str = DEFAULT_APP_BASE_URL,
) -> AuthStatus:
    return asyncio.run(check_auth_status_async(site_name=site_name, app_base_url=app_base_url))


async def login_async(
    *,
    refresh: bool = False,
    site_name: str = DEFAULT_SITE_NAME,
    app_base_url: str = DEFAULT_APP_BASE_URL,
    login_wait_seconds: int = DEFAULT_LOGIN_WAIT_SECONDS,
) -> AuthStatus:
    if not refresh:
        current = await check_auth_status_async(
            site_name=site_name,
            app_base_url=app_base_url,
        )
        if current.authenticated:
            return current

    session = None if refresh else load_session(site_name)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context_options: dict[str, Any] = {"viewport": {"width": 1440, "height": 950}}
        if session and "storage_state" in session:
            context_options["storage_state"] = session["storage_state"]
        context = await browser.new_context(**context_options)
        page = await context.new_page()
        await page.goto(app_base_url, wait_until="domcontentloaded", timeout=30_000)

        print("A TalentConnect browser window is open.")
        print("Complete the NUS login there; this command will detect it automatically.")

        deadline = asyncio.get_running_loop().time() + login_wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            profile = await _profile_from_page(page)
            if profile:
                storage_state = await context.storage_state()
                save_session(site_name, {"storage_state": storage_state})
                await browser.close()
                return _status_from_profile(profile)
            await page.wait_for_timeout(2_000)

        await browser.close()
        raise TimeoutError(
            f"TalentConnect login was not detected within {login_wait_seconds} seconds."
        )


def login(
    *,
    refresh: bool = False,
    site_name: str = DEFAULT_SITE_NAME,
    app_base_url: str = DEFAULT_APP_BASE_URL,
    login_wait_seconds: int = DEFAULT_LOGIN_WAIT_SECONDS,
) -> AuthStatus:
    return asyncio.run(
        login_async(
            refresh=refresh,
            site_name=site_name,
            app_base_url=app_base_url,
            login_wait_seconds=login_wait_seconds,
        )
    )


def logout(*, site_name: str = DEFAULT_SITE_NAME) -> str:
    """Forget the CLI's local Kinobi browser session.

    This deliberately does not sign the user out of NUS SSO in other browsers.
    """
    return delete_session(site_name)
