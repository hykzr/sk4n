from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

from .browser import DEFAULT_BASE_URL, validate_base_url


def _mail_root(base_url: str) -> str:
    parsed = urlsplit(validate_base_url(base_url))
    return f"https://{parsed.hostname}/mail/"


def message_url(identifier: str, base_url: str = DEFAULT_BASE_URL) -> str:
    return _mail_root(base_url) + "deeplink/read/" + quote(identifier, safe="")


def attachment_url(identifier: str, attachment_id: str, base_url: str = DEFAULT_BASE_URL) -> str:
    # Outlook's previewAttachmentInPopOut route; no temporary download tokens.
    return (
        _mail_root(base_url)
        + "deeplink/attachment/"
        + quote(identifier, safe="")
        + "/"
        + quote(attachment_id, safe="")
        + "/false/"
    )


def with_links(message: dict[str, Any], base_url: str = DEFAULT_BASE_URL) -> dict[str, Any]:
    identifier = message["ItemId"]["Id"]
    result = {**message, "url": message_url(identifier, base_url)}
    if isinstance(message.get("Attachments"), list):
        result["Attachments"] = [
            {
                **attachment,
                "url": attachment_url(identifier, attachment["AttachmentId"]["Id"], base_url),
            }
            if attachment.get("AttachmentId", {}).get("Id")
            else attachment
            for attachment in message["Attachments"]
        ]
    return result


def record_with_links(record: dict[str, Any], base_url: str = DEFAULT_BASE_URL) -> dict[str, Any]:
    result = {**record, "message": with_links(record["message"], base_url)}
    if record.get("metadata"):
        result["metadata"] = with_links(record["metadata"], base_url)
    identifier = record["message"]["ItemId"]["Id"]
    result["attachment_files"] = {
        aid: {
            **entry,
            "url": attachment_url(identifier, aid, base_url),
            "metadata": {
                **entry.get("metadata", {}),
                "url": attachment_url(identifier, aid, base_url),
            },
        }
        for aid, entry in record.get("attachment_files", {}).items()
    }
    return result
