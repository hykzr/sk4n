from __future__ import annotations

import asyncio
import json
from typing import Any

import bs4
import requests
from bs4 import BeautifulSoup, Tag

from ._shared import clean_whitespace, load_session, save_session, truncate_text


class RequestTools:
    """Lightweight tools using requests + BeautifulSoup for static pages."""

    def __init__(
        self,
        headers: dict | None = None,
        cookies: dict | None = None,
        timeout: int = 30,
        site_name: str | None = None,
    ):

        self._session = requests.Session()
        self._timeout = timeout
        self._site_name = site_name
        if headers:
            self._session.headers.update(headers)
        else:
            self._session.headers.update(
                {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    )
                }
            )
        if cookies:
            self._session.cookies.update(cookies)

        if site_name:
            session_data = load_session(site_name)
            if session_data:
                saved_cookies = session_data.get("cookies") or []
                if not saved_cookies and "storage_state" in session_data:
                    saved_cookies = session_data["storage_state"].get("cookies", [])
                for c in saved_cookies:
                    self._session.cookies.set(
                        c.get("name", ""),
                        c.get("value", ""),
                        domain=c.get("domain", ""),
                        path=c.get("path", "/"),
                    )

    @property
    def session(self):
        """Direct access to the underlying requests.Session."""
        return self._session

    def get(self, url: str, **kwargs) -> requests.Response:
        """Perform a GET request. Returns the raw Response object."""
        resp = self._session.get(url, timeout=self._timeout, **kwargs)
        resp.raise_for_status()
        return resp

    def post(self, url: str, **kwargs) -> requests.Response:
        """Perform a POST request. Returns the raw Response object."""
        resp = self._session.post(url, timeout=self._timeout, **kwargs)
        resp.raise_for_status()
        return resp

    def get_soup(self, url: str, **kwargs) -> bs4.BeautifulSoup:
        """GET *url* and return a BeautifulSoup object for the HTML."""
        resp = self.get(url, **kwargs)
        return BeautifulSoup(resp.text, "html.parser")

    def soup_from_html(self, html: str) -> bs4.BeautifulSoup:
        """Parse an HTML string into a BeautifulSoup object."""
        return BeautifulSoup(html, "html.parser")

    def get_text(self, soup: BeautifulSoup | Tag, selector: str = "body") -> str:
        """Return visible text of the first element matching *selector*."""
        el = soup.select_one(selector)
        if not el:
            return f"No element found for: {selector}"
        return clean_whitespace(el.get_text())

    def get_texts(self, soup: BeautifulSoup | Tag, selector: str) -> list[str]:
        """Return text for all elements matching *selector*."""
        return [clean_whitespace(el.get_text()) for el in soup.select(selector)]

    def get_links(self, soup: BeautifulSoup | Tag, selector: str = "a[href]") -> list[dict]:
        """Return [{text, href}] for all links matching *selector*."""
        results = []
        for a in soup.select(selector):
            results.append(
                {
                    "text": clean_whitespace(a.get_text()),
                    "href": a.get("href", ""),
                }
            )
        return results

    def get_attribute(self, soup: BeautifulSoup | Tag, selector: str, attr: str) -> str | None:
        """Return a single attribute value of the first matching element."""
        el = soup.select_one(selector)
        if not el:
            return None
        value = el.get(attr)
        if isinstance(value, list):
            return " ".join(value)
        return value

    def get_attributes(self, soup: BeautifulSoup | Tag, selector: str) -> list[dict]:
        """Return all attributes as dicts for every matching element."""
        return [dict(el.attrs) for el in soup.select(selector)]

    def select(self, soup: BeautifulSoup | Tag, selector: str) -> list:
        """Return all elements matching *selector* (bs4 Tag objects)."""
        return soup.select(selector)

    def select_one(self, soup: BeautifulSoup | Tag, selector: str):
        """Return the first element matching *selector* (bs4 Tag object)."""
        return soup.select_one(selector)

    def count(self, soup: BeautifulSoup | Tag, selector: str) -> int:
        """Count elements matching *selector*."""
        return len(soup.select(selector))

    def dom_tree(
        self,
        soup: BeautifulSoup | Tag,
        selector: str = "body",
        max_depth: int = 6,
        include_text: bool = True,
    ) -> str:
        """Return a simplified, indented DOM tree using BeautifulSoup."""
        from bs4.element import NavigableString

        root = soup.select_one(selector)
        if not root:
            return f"No element found for: {selector}"

        skip_tags = {"script", "style", "noscript", "svg", "path"}
        show_attrs = [
            "id",
            "class",
            "href",
            "src",
            "type",
            "role",
            "aria-label",
            "name",
            "placeholder",
            "value",
            "action",
            "method",
            "data-id",
            "data-url",
            "data-src",
            "data-href",
            "title",
            "alt",
        ]

        def walk(node, depth: int) -> list[str]:
            if max_depth > 0 and depth > max_depth:
                return ["  " * depth + "..."]
            lines: list[str] = []
            if isinstance(node, NavigableString):
                text = node.strip()
                if text and include_text:
                    short = text[:80] + "..." if len(text) > 80 else text
                    lines.append("  " * depth + json.dumps(short))
                return lines
            if not isinstance(node, Tag):
                return []
            tag = node.name
            if tag in skip_tags:
                return []
            attrs = ""
            for attr in show_attrs:
                value = node.get(attr)
                if value is None:
                    continue
                if isinstance(value, list):
                    value = " ".join(value)
                if value:
                    short = value[:120] + "..." if len(value) > 120 else value
                    attrs += f" {attr}={json.dumps(short)}"
            lines.append("  " * depth + f"<{tag}{attrs}>")
            for child in node.children:
                lines.extend(walk(child, depth + 1))
            return lines

        result = "\n".join(walk(root, 0))
        return truncate_text(result, 12000)

    def get_tables(
        self, soup: BeautifulSoup | Tag, selector: str = "table"
    ) -> list[list[list[str]]]:
        """Return table data as list of tables, each a list of rows."""
        tables = []
        for tbl in soup.select(selector):
            rows = []
            for tr in tbl.select("tr"):
                cells = [clean_whitespace(td.get_text()) for td in tr.select("td, th")]
                if cells:
                    rows.append(cells)
            tables.append(rows)
        return tables

    def get_json(self, url: str, **kwargs) -> Any:
        """GET *url* and parse the response as JSON."""
        resp = self.get(url, **kwargs)
        return resp.json()

    def post_json(self, url: str, **kwargs) -> Any:
        """POST to *url* and parse the response as JSON."""
        resp = self.post(url, **kwargs)
        return resp.json()

    def save_session_data(self, site_name: str | None = None) -> str:
        """Save the current session cookies to disk."""
        name = site_name or self._site_name
        if not name:
            return "Error: provide site_name."
        cookies = []
        for c in self._session.cookies:
            cookies.append(
                {
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain,
                    "path": c.path,
                }
            )
        return save_session(name, {"cookies": cookies})


def request_user_interaction(msg: str) -> str:
    """Pause execution and ask the human to perform a manual action."""
    print(f"\n{'=' * 60}")
    print(f"ACTION REQUIRED: {msg}")
    print(f"{'=' * 60}")
    input("Press Enter when done... ")
    return "done"


async def async_request_user_interaction(msg: str) -> str:
    """Async version of request_user_interaction."""
    loop = asyncio.get_event_loop()
    print(f"\n{'=' * 60}")
    print(f"ACTION REQUIRED: {msg}")
    print(f"{'=' * 60}")
    await loop.run_in_executor(None, input, "Press Enter when done... ")
    return "done"
