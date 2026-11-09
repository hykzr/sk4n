"""Useful Outlook fields, without Exchange transport and rendering metadata."""

from __future__ import annotations

from typing import Any

from sk4n.tools.output import html_text, useful_fields

MESSAGE_FIELDS = {
    "ItemId",
    "Subject",
    "url",
    "From",
    "Sender",
    "ToRecipients",
    "CcRecipients",
    "BccRecipients",
    "ReplyTo",
    "DateTimeReceived",
    "DateTimeSent",
    "DateTimeLastModified",
    "IsRead",
    "IsDraft",
    "Importance",
    "Flag",
    "Categories",
    "HasAttachments",
    "Attachments",
    "Body",
    "UniqueBody",
    "Preview",
    "ParentFolderId",
    "ParentFolderDisplayName",
    "ConversationId",
    "InReplyTo",
    "References",
    "_outlook",
    "status",
    "pending",
    "error",
    "warning",
    "sent",
    "draft_id",
    "message_id",
    "cache_refreshed",
}
ATTACHMENT_FIELDS = {
    "id",
    "status",
    "__type",
    "AttachmentId",
    "Name",
    "ContentType",
    "Size",
    "IsInline",
    "ContentId",
    "url",
    "path",
    "name",
    "size",
    "content_type",
    "downloaded",
    "error",
    "kind",
}
PERSON_FIELDS = {
    "Id",
    "DisplayName",
    "EmailAddresses",
    "EmailAddress",
    "JobTitle",
    "Department",
    "CompanyName",
    "OfficeLocation",
    "BusinessPhoneNumbers",
    "MobilePhone",
    "PrimaryEmailAddress",
}


def compact(value: Any) -> Any:
    if isinstance(value, list):
        return [compact(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "ItemId" in value:
        result = useful_fields(value, MESSAGE_FIELDS)
    elif "FolderId" in value:
        result = useful_fields(
            value,
            {
                "FolderId",
                "ParentFolderId",
                "DisplayName",
                "DistinguishedFolderId",
                "TotalCount",
                "UnreadCount",
                "__type",
            },
        )
    elif "DisplayName" in value and "EmailAddresses" in value:
        return useful_fields(value, PERSON_FIELDS)
    else:
        # Unknown API envelopes and small action/status results remain intact.
        return {key: compact(item) for key, item in value.items()}
    for key, item in list(result.items()):
        if key in {"ItemId", "FolderId", "ParentFolderId", "ConversationId"}:
            result[key] = useful_fields(item, {"Id"}) if isinstance(item, dict) else item
        elif key in {"From", "Sender"}:
            result[key] = _address(item)
        elif key in {"ToRecipients", "CcRecipients", "BccRecipients", "ReplyTo"}:
            result[key] = [_address(address) for address in item]
        elif key in {"Body", "UniqueBody"} and isinstance(item, dict):
            result[key] = useful_fields(item, {"BodyType", "Value", "IsTruncated"})
            if str(item.get("BodyType", "")).casefold() == "html":
                result[key] = {
                    **result[key],
                    "BodyType": "Text",
                    "Value": html_text(item.get("Value") or ""),
                }
        elif key == "Attachments":
            result[key] = [_attachment(attachment) for attachment in item]
        elif key == "_outlook":
            result[key] = {
                name: [_attachment(a) for a in field] if name == "attachments" else field
                for name, field in item.items()
                if name
                in {
                    "path",
                    "body_downloaded",
                    "body_current",
                    "attachments",
                    "preview_path",
                    "preview_reused",
                    "preview_viewport_only",
                    "preview_output_path",
                }
            }
    if "Body" in result:
        result.pop("Preview", None)
        result.pop("UniqueBody", None)
    if result.get("Sender") == result.get("From"):
        result.pop("Sender", None)
    return result


def _address(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    address = value.get("Mailbox", value)
    result = useful_fields(address, {"Name", "EmailAddress"})
    return {"Mailbox": result} if "Mailbox" in value else result


def _attachment(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    result = useful_fields(value, ATTACHMENT_FIELDS)
    if isinstance(result.get("AttachmentId"), dict):
        result["AttachmentId"] = useful_fields(result["AttachmentId"], {"Id"})
    return result
