from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Route, async_playwright

from sk4n.tools.shared import atomic_output_path, load_session

from .browser import OUTLOOK_HOSTS, validate_base_url
from .client import OutlookAuthError, OutlookClient, OutlookError
from .fetcher import OutlookFetcher
from .links import message_url
from .storage import OutlookStore, digest, same_revision


def preview_fingerprint(message: dict[str, Any]) -> str:
    # Include display fields as well as the server revision: read/flag changes
    # and some writes can leave ChangeKey unchanged. Local result paths vary.
    fields = {key: value for key, value in message.items() if key != "_outlook"}
    return digest(json.dumps(fields, sort_keys=True, ensure_ascii=False))


class OutlookPreview:
    def __init__(self, client: OutlookClient, store: OutlookStore):
        self.client, self.store = client, store
        self.fetcher = OutlookFetcher(client, store)

    def message(
        self, identifier: str, *, refresh_mode: str = "default", output: Path | None = None
    ) -> dict[str, Any]:
        if output is not None:
            output = output.expanduser().resolve()
            if output.suffix.lower() != ".png":
                raise ValueError("Preview output must have a .png extension.")
        message = self.fetcher.message(identifier, refresh_mode=refresh_mode)
        record = self.store.message(identifier)
        path = self.store.message_path(identifier)
        if record is None or path is None:
            raise OutlookError("Cache the message before generating its preview.")
        path = path.parent / "preview.png"
        fingerprint = preview_fingerprint(message)
        entry = record.get("preview") or {}
        content = path.read_bytes() if path.is_file() else b""
        reusable = bool(
            content
            and entry.get("fingerprint") == fingerprint
            and entry.get("size") == len(content)
            and entry.get("sha256") == hashlib.sha256(content).hexdigest()
        )
        reused = reusable and refresh_mode != "force"
        if not reused:
            if refresh_mode == "none":
                raise OutlookError(
                    "Preview is not cached for this message revision. Remove --no-refresh to generate it."
                )
            capture_preview(self.client, message, path)
            content = path.read_bytes()
            record["preview"] = {
                "path": path.name,
                "fingerprint": fingerprint,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            self.store.save_message(record)
        if output is not None and output != path.resolve():
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with atomic_output_path(output) as temporary:
                temporary.write_bytes(content)
        return {
            **message,
            "_outlook": {
                **message["_outlook"],
                "preview_path": str(path),
                "preview_reused": reused,
                "preview_viewport_only": True,
                **({"preview_output_path": str(output)} if output is not None else {}),
            },
        }


def is_mailbox_write(url: str, method: str, headers: dict[str, str]) -> bool:
    """Keep Outlook's preview browser from autosaving or marking mail read."""
    parts = urlsplit(url)
    if parts.hostname not in OUTLOOK_HOSTS or not parts.path.startswith("/owa/service.svc"):
        return False
    action = (
        parse_qs(parts.query).get("action", [""])[0]
        or headers.get("action", "")
        or headers.get("x-owa-action", "")
    )
    # Unknown service operations fail closed; UI read calls use Get/Find/Resolve.
    return not action.startswith(("Get", "Find", "Resolve"))


async def capture_preview_async(
    client: OutlookClient, message: dict[str, Any], output: Path
) -> None:
    saved = load_session(client.site_name)
    if not isinstance(saved, dict) or not isinstance(saved.get("storage_state"), dict):
        raise OutlookAuthError("No saved browser login. Run `nus-outlook auth login` first.")
    base_url = validate_base_url(str(saved.get("base_url") or client.base_url))
    identifier = message["ItemId"]["Id"]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                storage_state=saved["storage_state"],
                viewport={"width": 1440, "height": 1100},
                service_workers="block",
            )

            async def guard(route: Route) -> None:
                request = route.request
                if is_mailbox_write(request.url, request.method, request.headers):
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**/*", guard)
            page = await context.new_page()
            page.set_default_timeout(client.timeout * 1000)
            await page.goto(message_url(identifier, base_url), wait_until="domcontentloaded")
            await page.get_by_role("document", name="Message body", exact=True).wait_for()
            if message.get("IsDraft"):
                await page.get_by_role("button", name="Continue editing", exact=True).click()
                await page.get_by_role("textbox", name="Add a message", exact=True).wait_for()
                await page.get_by_role("button", name="Send", exact=True).wait_for()
                # The recipient wells and ribbon hydrate after the editor mounts.
                await page.get_by_role("button", name="Attach file", exact=True).wait_for()
                for field in ("ToRecipients", "CcRecipients", "BccRecipients"):
                    for recipient in message.get(field) or []:
                        mailbox = recipient.get("Mailbox", recipient)
                        labels = [mailbox.get("EmailAddress"), mailbox.get("Name")]
                        names = [re.escape(label) for label in labels if label]
                        if names:
                            await page.get_by_text(
                                re.compile("^(?:" + "|".join(names) + ")$")
                            ).first.wait_for()
            for attachment in message.get("Attachments") or []:
                if attachment.get("Name") and not attachment.get("IsInline"):
                    # Images expose their filename as an accessible option name,
                    # rather than visible text; ordinary files use the same role.
                    await page.get_by_role(
                        "option", name=re.compile(re.escape(attachment["Name"]))
                    ).first.wait_for()
            await page.evaluate("document.fonts.ready")
            await page.screenshot(
                path=str(output), type="png", full_page=True, animations="disabled"
            )
        finally:
            await browser.close()


def capture_preview(client: OutlookClient, message: dict[str, Any], output: Path) -> None:
    """Publish only screenshots of a loaded, unchanged message; never send from UI."""
    output = output.expanduser().resolve()
    if output.suffix.lower() != ".png":
        raise ValueError("Preview output must have a .png extension.")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with atomic_output_path(output) as temporary:
        asyncio.run(capture_preview_async(client, message, temporary))
        latest = client.message(message["ItemId"]["Id"])
        if not same_revision(message, latest) or message.get("IsDraft") != latest.get("IsDraft"):
            raise OutlookError(
                "Message changed during preview; generate a fresh preview before sending."
            )
