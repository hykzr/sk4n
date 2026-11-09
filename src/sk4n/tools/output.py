"""Small presentation helpers; never alter the records stored in service caches."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from bs4 import BeautifulSoup

OUTPUT_FORMATS = ("json", "json-full", "jsonl", "plain")
FORMAT_HELP = "Output format: json/jsonl contain useful fields; json-full includes the full record."


def useful_fields(record: Mapping[str, Any], fields: set[str]) -> dict[str, Any]:
    """Select populated fields, retaining meaningful false and zero values."""
    return {
        key: value
        for key, value in record.items()
        if key in fields and value not in (None, "", [], {})
    }


def html_text(value: str) -> str:
    """Remove markup and styling without truncating prose or losing link targets."""
    if not re.search(
        r"</?(?:html|body|p|div|span|br|a|b|strong|i|em|u|ul|ol|li|table|thead|tbody|tr|td|th|h[1-6]|blockquote|pre|code|img|style|script|head|hr|font)(?=[\s/>])",
        value,
        re.IGNORECASE,
    ):
        return value
    soup = BeautifulSoup(value, "html.parser")
    for element in soup(["script", "style", "head"]):
        element.decompose()
    code_blocks = {}
    for index, element in enumerate(soup.find_all("pre")):
        marker = f"SK4N_CODE_BLOCK_{index}_"
        while marker in value:
            marker += "_"
        code_blocks[marker] = element.get_text().strip("\n")
        element.replace_with(f"\n{marker}\n")
    for image in soup.find_all("img"):
        label, target = image.get("alt"), str(image.get("src") or "")
        if target and not target.lower().startswith(("javascript:", "data:")):
            image.replace_with(f"{label or 'Image'} ({target})")
        elif label:
            image.replace_with(str(label))
    for link in soup.find_all("a", href=True):
        target = str(link["href"])
        label = link.get_text(" ", strip=True)
        if target and not target.lower().startswith(("javascript:", "data:")):
            link.replace_with(f"{label} ({target})" if label and label != target else target)
    for element in soup.find_all(["br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"]):
        element.insert_before("\n")
        element.insert_after("\n")
    for element in soup.find_all(["td", "th"]):
        element.insert_after(" ")
    result = "\n".join(
        line for part in soup.get_text().splitlines() if (line := " ".join(part.split()))
    )
    for marker, code in code_blocks.items():
        result = result.replace(marker, code)
    return result
