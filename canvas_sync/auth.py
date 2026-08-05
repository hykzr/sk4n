from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from playwright.async_api import async_playwright

from tools import RequestTools
from tools.shared import delete_session, load_session, save_session

DEFAULT_LOGIN_WAIT_SECONDS = 300


@dataclass(frozen=True)
class CanvasAuthStatus:
    authenticated: bool
    name: str = ""
    email: str = ""
    user_id: str = ""
    error: str = ""


def canvas_api_url(base_url: str, path: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def validate_canvas_session(base_url: str, site_name: str, timeout: int = 15) -> bool:
    if not load_session(site_name):
        return False
    rt = RequestTools(site_name=site_name, timeout=timeout)
    try:
        response = rt.get(canvas_api_url(base_url, "/api/v1/users/self/profile"))
    except (requests.RequestException, ValueError):
        return False
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        return False
    try:
        data = response.json()
    except ValueError:
        return False
    return isinstance(data, dict) and bool(data.get("id"))


def check_auth_status(
    *,
    base_url: str,
    site_name: str,
    timeout: int = 15,
) -> CanvasAuthStatus:
    if not load_session(site_name):
        return CanvasAuthStatus(authenticated=False)
    rt = RequestTools(site_name=site_name, timeout=timeout)
    try:
        response = rt.get(canvas_api_url(base_url, "/api/v1/users/self/profile"))
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        return CanvasAuthStatus(authenticated=False, error=str(exc))
    if not isinstance(data, dict) or not data.get("id"):
        return CanvasAuthStatus(authenticated=False)
    return CanvasAuthStatus(
        authenticated=True,
        name=str(data.get("name") or data.get("short_name") or ""),
        email=str(data.get("primary_email") or ""),
        user_id=str(data["id"]),
    )


async def fetch_profile_from_browser(page: Any, base_url: str) -> dict[str, Any]:
    return await page.evaluate(
        """
        async (url) => {
            try {
                const response = await fetch(url, {
                    credentials: 'include',
                    headers: { 'Accept': 'application/json' }
                });
                const text = await response.text();
                let data;
                try {
                    data = JSON.parse(text);
                } catch {
                    data = null;
                }
                return { ok: response.ok, status: response.status, data };
            } catch (error) {
                return { ok: false, status: 0, error: String(error) };
            }
        }
        """,
        canvas_api_url(base_url, "/api/v1/users/self/profile"),
    )


async def login_with_browser(
    *,
    base_url: str,
    site_name: str,
    login_wait_seconds: int = DEFAULT_LOGIN_WAIT_SECONDS,
    refresh: bool = False,
) -> None:

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context_kwargs: dict[str, Any] = {"viewport": {"width": 1440, "height": 950}}
        session = None if refresh else load_session(site_name)
        if session and "storage_state" in session:
            context_kwargs["storage_state"] = session["storage_state"]
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()
        page.set_default_timeout(30_000)
        await page.goto(base_url, wait_until="domcontentloaded")

        print("Canvas session is missing or expired.")
        print("A browser window is open. Log in to Canvas there; sync will continue automatically.")

        canvas_host = urlparse(base_url).netloc
        deadline = asyncio.get_running_loop().time() + login_wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            current_host = urlparse(page.url).netloc
            if current_host == canvas_host:
                profile = await fetch_profile_from_browser(page, base_url)
                data = profile.get("data")
                if profile.get("ok") and isinstance(data, dict) and data.get("id"):
                    storage_state = await context.storage_state()
                    save_session(site_name, {"storage_state": storage_state})
                    await browser.close()
                    print(f"Canvas session saved as '{site_name}'.")
                    return
            await page.wait_for_timeout(5000)

        await browser.close()
        raise TimeoutError(f"Canvas login was not detected within {login_wait_seconds} seconds.")


def ensure_canvas_session(
    *,
    base_url: str,
    site_name: str,
    login_wait_seconds: int = DEFAULT_LOGIN_WAIT_SECONDS,
) -> bool:
    if validate_canvas_session(base_url, site_name):
        return False
    asyncio.run(
        login_with_browser(
            base_url=base_url,
            site_name=site_name,
            login_wait_seconds=login_wait_seconds,
        )
    )
    if not validate_canvas_session(base_url, site_name):
        raise RuntimeError("Canvas login completed, but the saved session still is not valid.")
    return True


def login(
    *,
    base_url: str,
    site_name: str,
    login_wait_seconds: int = DEFAULT_LOGIN_WAIT_SECONDS,
    refresh: bool = False,
) -> CanvasAuthStatus:
    status = check_auth_status(base_url=base_url, site_name=site_name)
    if status.authenticated and not refresh:
        return status
    asyncio.run(
        login_with_browser(
            base_url=base_url,
            site_name=site_name,
            login_wait_seconds=login_wait_seconds,
            refresh=refresh,
        )
    )
    status = check_auth_status(base_url=base_url, site_name=site_name)
    if not status.authenticated:
        raise RuntimeError("Canvas login completed, but the saved session still is not valid.")
    return status


def logout(*, site_name: str) -> str:
    """Forget only this CLI's saved Canvas session."""
    return delete_session(site_name)
