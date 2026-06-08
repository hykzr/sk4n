from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal, Sequence

from ._shared import load_session, save_session, truncate_text, clean_whitespace
from .request import async_request_user_interaction

if TYPE_CHECKING:
    from playwright.async_api import Cookie


class BrowserTools:
    """Thin wrapper around Playwright exposing agent-friendly helpers."""

    def __init__(
        self,
        headless: bool = False,
        viewport: dict | None = None,
        user_agent: str | None = None,
        timeout: int = 30_000,
    ):
        self._headless = headless
        self._viewport = viewport or {"width": 1280, "height": 720}
        self._user_agent = user_agent
        self._timeout = timeout
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    async def start(self, site_name: str | None = None):
        """Launch the browser and optionally restore a saved session."""
        from playwright.async_api import async_playwright

        self._site_name = site_name
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self._headless)
        kwargs: dict[str, Any] = {"viewport": self._viewport}
        if self._user_agent:
            kwargs["user_agent"] = self._user_agent

        if site_name:
            session = load_session(site_name)
            if session and "storage_state" in session:
                kwargs["storage_state"] = session["storage_state"]

        self._context = await self._browser.new_context(**kwargs)
        self._page = await self._context.new_page()
        self._page.set_default_timeout(self._timeout)

        restored = ""
        if site_name and load_session(site_name):
            restored = f" (session '{site_name}' restored)"
        return f"Browser started.{restored}"

    async def stop(self):
        """Close the browser and free resources."""
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._page = self._context = self._browser = self._pw = None
        self._site_name = None
        return "Browser stopped."

    async def save_session_data(self, site_name: str | None = None) -> str:
        """Save the current browser session to disk."""
        name = site_name or getattr(self, "_site_name", None)
        if not name:
            return "Error: provide site_name to save_session_data(), or pass it to start()."
        if not self._context:
            return "Error: browser context not initialized."
        storage_state = await self._context.storage_state()
        return save_session(name, {"storage_state": storage_state})

    async def is_logged_in(
        self, check_selector: str | None = None, check_url: str | None = None
    ) -> bool:
        """Heuristic check whether the user is logged in."""
        if check_url:
            await self.navigate(check_url)
            await self.wait_for_timeout(2000)
        if check_selector:
            try:
                await self.page.wait_for_selector(
                    check_selector, state="attached", timeout=5000
                )
                return True
            except Exception:
                return False
        return False

    async def ensure_logged_in(
        self,
        site_name: str,
        check_selector: str,
        login_url: str | None = None,
        check_url: str | None = None,
        login_msg: str = "Please log in to the website in the browser window.",
    ) -> str:
        """Ensure the user is logged in and persist the session."""
        if await self.is_logged_in(check_selector=check_selector, check_url=check_url):
            return f"Already logged in ('{site_name}')."

        if login_url:
            await self.navigate(login_url)
        await async_request_user_interaction(login_msg)
        await self.wait_for_timeout(2000)

        if not await self.is_logged_in(
            check_selector=check_selector, check_url=check_url
        ):
            return "Warning: login check still fails after user interaction."

        await self.save_session_data(site_name)
        return f"Logged in and session saved for '{site_name}'."

    @property
    def page(self):
        assert self._page, "Browser not started. Call start() first."
        return self._page

    async def navigate(
        self,
        url: str,
        wait_until: (
            Literal["commit", "domcontentloaded", "load", "networkidle"] | None
        ) = "domcontentloaded",
    ) -> str:
        resp = await self.page.goto(url, wait_until=wait_until)
        status = resp.status if resp else "?"
        return f"Navigated to {self.page.url}  (status {status}, title: {await self.page.title()})"

    async def current_url(self) -> str:
        return self.page.url

    async def go_back(self) -> str:
        await self.page.go_back()
        return f"Back -> {self.page.url}"

    async def go_forward(self) -> str:
        await self.page.go_forward()
        return f"Forward -> {self.page.url}"

    async def reload(self) -> str:
        await self.page.reload()
        return f"Reloaded {self.page.url}"

    async def title(self) -> str:
        return await self.page.title()

    async def get_html(self, selector: str = "html", outer: bool = True) -> str:
        prop = "outerHTML" if outer else "innerHTML"
        html = await self.page.eval_on_selector(selector, f"el => el.{prop}")
        return truncate_text(html)

    async def get_text(self, selector: str = "body") -> str:
        text = await self.page.eval_on_selector(selector, "el => el.innerText")
        return truncate_text(clean_whitespace(text))

    async def get_texts(self, selector: str) -> list[str]:
        return await self.page.eval_on_selector_all(
            selector, "els => els.map(el => el.innerText.trim())"
        )

    async def get_attribute(self, selector: str, attr: str) -> str | None:
        return await self.page.get_attribute(selector, attr)

    async def get_attributes(self, selector: str) -> list[dict]:
        return await self.page.eval_on_selector_all(
            selector,
            """els => els.map(el => {
                const o = {};
                for (const a of el.attributes) o[a.name] = a.value;
                return o;
            })""",
        )

    async def query_selector_all(self, selector: str) -> int:
        els = await self.page.query_selector_all(selector)
        return len(els)

    async def dom_tree(
        self,
        selector: str = "body",
        max_depth: int = 6,
        include_text: bool = True,
    ) -> str:
        js = """
        ({selector, maxDepth, includeText}) => {
            function walk(node, depth) {
                if (depth > maxDepth && maxDepth > 0) return '  '.repeat(depth) + '...';
                const lines = [];
                if (node.nodeType === 3) {
                    const t = node.textContent.trim();
                    if (t && includeText) {
                        const short = t.length > 80 ? t.slice(0, 80) + '…' : t;
                        lines.push('  '.repeat(depth) + JSON.stringify(short));
                    }
                    return lines.join('\\n');
                }
                if (node.nodeType !== 1) return '';
                const tag = node.tagName.toLowerCase();
                if (['script','style','noscript','svg','path'].includes(tag)) return '';
                let attrs = '';
                for (const a of ['id','class','href','src','type','role',
                                  'aria-label','name','placeholder','value',
                                  'action','method','data-id','data-url',
                                  'data-src','data-href','title','alt']) {
                    const v = node.getAttribute(a);
                    if (v !== null && v !== '') {
                        let short = v.length > 120 ? v.slice(0, 120) + '…' : v;
                        attrs += ' ' + a + '=' + JSON.stringify(short);
                    }
                }
                lines.push('  '.repeat(depth) + '<' + tag + attrs + '>');
                for (const c of node.childNodes) {
                    const r = walk(c, depth + 1);
                    if (r) lines.push(r);
                }
                return lines.join('\\n');
            }
            const root = document.querySelector(selector);
            if (!root) return 'No element found for: ' + selector;
            return walk(root, 0);
        }
        """
        tree = await self.page.evaluate(
            js,
            {"selector": selector, "maxDepth": max_depth, "includeText": include_text},
        )
        return truncate_text(tree, 12000)

    async def get_links(self, selector: str = "a[href]") -> list[dict]:
        return await self.page.eval_on_selector_all(
            selector,
            "els => els.map(el => ({text: el.innerText.trim(), href: el.href}))",
        )

    async def get_forms(self) -> list[dict]:
        return await self.page.evaluate("""
            () => Array.from(document.querySelectorAll('form')).map(f => ({
                action: f.action,
                method: f.method,
                id: f.id,
                fields: Array.from(f.elements).map(e => ({
                    tag: e.tagName.toLowerCase(),
                    type: e.type || '',
                    name: e.name || '',
                    id: e.id || '',
                    placeholder: e.placeholder || '',
                    value: e.value?.slice(0, 100) || '',
                })),
            }))
        """)

    async def get_tables(self, selector: str = "table") -> list[list[list[str]]]:
        return await self.page.eval_on_selector_all(
            selector,
            """els => els.map(tbl =>
                Array.from(tbl.rows).map(row =>
                    Array.from(row.cells).map(c => c.innerText.trim())
                )
            )""",
        )

    async def screenshot(
        self, path: str = "screenshot.png", full_page: bool = False
    ) -> str:
        await self.page.screenshot(path=path, full_page=full_page)
        return f"Screenshot saved to {path}"

    async def scroll(self, direction: str = "down", amount: int = 500) -> str:
        delta = amount if direction == "down" else -amount
        await self.page.mouse.wheel(0, delta)
        await self.page.wait_for_timeout(300)
        return f"Scrolled {direction} by {amount}px"

    async def click(self, selector: str, timeout: int | None = None) -> str:
        await self.page.click(selector, timeout=timeout or self._timeout)
        await self.page.wait_for_timeout(500)
        return f"Clicked {selector!r}. Now at: {self.page.url}"

    async def fill(self, selector: str, value: str) -> str:
        await self.page.fill(selector, value)
        return f"Filled {selector!r} with {value!r}"

    async def select_option(self, selector: str, value: str) -> str:
        await self.page.select_option(selector, value)
        return f"Selected {value!r} in {selector!r}"

    async def hover(self, selector: str) -> str:
        await self.page.hover(selector)
        return f"Hovered over {selector!r}"

    async def press(self, key: str) -> str:
        await self.page.keyboard.press(key)
        return f"Pressed {key!r}"

    async def type_text(self, text: str, delay: int = 50) -> str:
        await self.page.keyboard.type(text, delay=delay)
        return f"Typed {text!r}"

    async def wait_for(
        self,
        selector: str,
        state: Literal["attached", "detached", "hidden", "visible"] | None = "visible",
        timeout: int | None = None,
    ) -> str:
        await self.page.wait_for_selector(
            selector, state=state, timeout=timeout or self._timeout
        )
        return f"Element {selector!r} is {state}"

    async def wait_for_navigation(self, timeout: int | None = None) -> str:
        await self.page.wait_for_load_state(
            "domcontentloaded", timeout=timeout or self._timeout
        )
        return f"Navigation complete: {self.page.url}"

    async def wait_for_timeout(self, ms: int) -> str:
        await self.page.wait_for_timeout(ms)
        return f"Waited {ms}ms"

    async def evaluate(self, expression: str) -> Any:
        result = await self.page.evaluate(expression)
        if isinstance(result, str):
            return truncate_text(result)
        return result

    async def get_cookies(self) -> list["Cookie"]:
        if not self._context:
            return []
        return await self._context.cookies()

    async def set_cookies(self, cookies: Sequence[dict]) -> str:
        if not self._context:
            return "Browser context not initialized."
        await self._context.add_cookies(cookies)  # pyright: ignore[reportArgumentType]
        return f"Set {len(cookies)} cookie(s)"

    async def get_local_storage(self) -> dict:
        return await self.page.evaluate(
            "() => Object.fromEntries(Object.entries(localStorage))"
        )

    async def find_interactive_elements(self) -> str:
        js = """
        () => {
            const selectors = [
                'a[href]', 'button', 'input', 'textarea', 'select',
                '[role="button"]', '[onclick]', '[tabindex]'
            ];
            const seen = new Set();
            const results = [];
            for (const sel of selectors) {
                for (const el of document.querySelectorAll(sel)) {
                    if (seen.has(el)) continue;
                    seen.add(el);
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 && rect.height === 0) continue;
                    const tag = el.tagName.toLowerCase();
                    const info = {tag};
                    if (el.id) info.id = el.id;
                    if (el.name) info.name = el.name;
                    if (el.type) info.type = el.type;
                    if (el.href) info.href = el.href;
                    if (el.className) info.class = typeof el.className === 'string'
                        ? el.className.slice(0, 100) : '';
                    const text = el.innerText?.trim();
                    if (text) info.text = text.slice(0, 80);
                    if (el.placeholder) info.placeholder = el.placeholder;
                    if (el.ariaLabel) info.ariaLabel = el.ariaLabel;
                    const selectorParts = [];
                    selectorParts.push(tag);
                    if (el.id) selectorParts[0] += '#' + el.id;
                    else if (el.className && typeof el.className === 'string')
                        selectorParts[0] += '.' + el.className.trim().split(/\\s+/).slice(0,2).join('.');
                    info.suggested_selector = selectorParts[0];
                    results.push(info);
                }
            }
            return JSON.stringify(results, null, 2);
        }
        """
        result = await self.page.evaluate(js)
        return truncate_text(result, 12000)

    async def network_log_start(self) -> str:
        if not hasattr(self, "_network_log"):
            self._network_log: list[dict] = []

        async def _on_request(request):
            self._network_log.append(
                {
                    "method": request.method,
                    "url": request.url,
                    "resource_type": request.resource_type,
                }
            )

        self.page.on("request", _on_request)
        self._network_handler = _on_request
        return "Network logging started."

    async def network_log_get(self, filter_type: str | None = None) -> list[dict]:
        log = getattr(self, "_network_log", [])
        if filter_type:
            log = [r for r in log if r["resource_type"] == filter_type]
        return log

    async def intercept_response(self, url_pattern: str, timeout: int = 10000) -> dict:
        future = asyncio.get_event_loop().create_future()

        async def handler(response):
            if url_pattern in response.url:
                try:
                    body = await response.text()
                except Exception:
                    body = "<binary>"
                if not future.done():
                    future.set_result(
                        {
                            "url": response.url,
                            "status": response.status,
                            "headers": dict(response.headers),
                            "body": truncate_text(body, 8000),
                        }
                    )

        self.page.on("response", handler)
        try:
            result = await asyncio.wait_for(future, timeout=timeout / 1000)
        except asyncio.TimeoutError:
            result = {
                "error": f"No response matching {url_pattern!r} within {timeout}ms"
            }
        self.page.remove_listener("response", handler)
        return result


def run_sync(coro):
    """Run an async coroutine from sync code."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import nest_asyncio

        nest_asyncio.apply()
        return loop.run_until_complete(coro)
    return asyncio.run(coro)
