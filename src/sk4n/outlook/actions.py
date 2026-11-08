from __future__ import annotations

import base64
import mimetypes
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .client import OutlookClient, OutlookError, OutlookTransportError, folder_id, response_messages
from .fetcher import OutlookFetcher
from .links import message_url
from .storage import OutlookStore


def item_id(identifier: str, change_key: str | None = None) -> dict[str, str]:
    if not identifier:
        raise ValueError("Message ID must not be empty.")
    result = {"__type": "ItemId:#Exchange", "Id": identifier}
    if change_key:
        result["ChangeKey"] = change_key
    return result


def returned_id(response: dict[str, Any], *, fallback: str | None = None) -> str:
    items = response.get("Items") or [{}]
    item = items[0]
    moved = response.get("MovedItemId", {})
    identifier = item.get("ItemId", {}).get("Id") or moved.get("Id") or fallback
    if not isinstance(identifier, str) or not identifier:
        raise OutlookError("Outlook did not return a message ID for the write.")
    return identifier


def recipients(addresses: str | Sequence[str]) -> list[dict[str, str]]:
    values = [addresses] if isinstance(addresses, str) else addresses
    result = []
    for address in values:
        address = address.strip()
        if "@" not in address or any(c.isspace() for c in address):
            raise ValueError("Recipients must be email addresses (one per option).")
        result.append({"EmailAddress": address, "MailboxType": "OneOff", "RoutingType": "SMTP"})
    return result


def file_attachments(paths: Sequence[Path]) -> list[dict[str, Any]]:
    # Read every file before changing the mailbox, including for new drafts.
    return [
        {
            "__type": "FileAttachment:#Exchange",
            "Name": path.name,
            "ContentType": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "IsInline": False,
            "Content": base64.b64encode(path.expanduser().read_bytes()).decode("ascii"),
        }
        for path in paths
    ]


class OutlookActions:
    """Explicit, item-scoped mailbox writes with immediate cache reconciliation."""

    def __init__(self, client: OutlookClient, store: OutlookStore):
        self.client = client
        self.store = store
        self.fetcher = OutlookFetcher(client, store)

    def _write_existing(self, identifier: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return response_messages(self.client.write_action(name, body))[0]
        except Exception:
            # A transport/response failure can follow a successful remote write.
            # Remove the message and preview rather than expose stale local data.
            self.store.forget_message(identifier)
            raise

    def _set_field(self, identifier: str, field: str, value: Any) -> dict[str, Any]:
        current = self.client.message(identifier)
        return self._update(identifier, current, {field: value})

    def _update(
        self,
        identifier: str,
        current: dict[str, Any],
        fields: dict[str, Any],
        *,
        send: bool = False,
    ) -> dict[str, Any]:
        change_key = current.get("ItemId", {}).get("ChangeKey")
        response = self._write_existing(
            identifier,
            "UpdateItem",
            {
                "ItemChanges": [
                    {
                        "__type": "ItemChange:#Exchange",
                        "ItemId": item_id(identifier, change_key),
                        "Updates": [
                            {
                                "__type": "SetItemField:#Exchange",
                                "Path": {"__type": "PropertyUri:#Exchange", "FieldURI": field},
                                "Item": {"__type": "Message:#Exchange", field: value},
                            }
                            for field, value in fields.items()
                        ],
                    }
                ],
                "ConflictResolution": "NeverOverwrite" if current.get("IsDraft") else "AutoResolve",
                "MessageDisposition": "SendAndSaveCopy" if send else "SaveOnly",
                "SuppressReadReceipts": True,
                "SendOnNotFoundError": False,
            },
        )
        if send:
            # Submission has succeeded. A failed follow-up read must not look like a
            # failed send and invite a duplicate retry.
            try:
                result = self.fetcher.refresh_after_write(
                    returned_id(response, fallback=identifier),
                    previous_id=identifier,
                )
            except Exception as exc:
                return {
                    "status": "submitted",
                    "draft_id": identifier,
                    "url": message_url(identifier, self.client.base_url),
                    "cache_refreshed": False,
                    "warning": f"Outlook accepted the send; follow-up read failed ({type(exc).__name__}). Do not resend; check Sent Items.",
                }
            return {
                **result,
                "status": "submitted",
                "cache_refreshed": True,
                "pending": result.get("IsDraft") is True,
            }
        return self.fetcher.refresh_after_write(
            returned_id(response, fallback=identifier), previous_id=identifier
        )

    def flag(self, identifier: str, *, flagged: bool) -> dict[str, Any]:
        return self._set_field(
            identifier,
            "Flag",
            {"__type": "FlagType:#Exchange", "FlagStatus": "Flagged" if flagged else "NotFlagged"},
        )

    def mark_read(self, identifier: str, *, read: bool) -> dict[str, Any]:
        return self._set_field(identifier, "IsRead", read)

    def move(
        self, identifier: str, *, folder: str | None = None, folder_id_value: str | None = None
    ) -> dict[str, Any]:
        if (folder is None) == (folder_id_value is None):
            raise ValueError("Specify exactly one destination: --folder or --folder-id.")
        selected, named, _ = self.fetcher.select_folder(folder, folder_id_value, "default")
        current = self.client.message(identifier)
        response = self._write_existing(
            identifier,
            "MoveItem",
            {
                "ToFolderId": {
                    "__type": "TargetFolderId:#Exchange",
                    "BaseFolderId": folder_id(
                        selected, distinguished=folder_id_value is None and not named
                    ),
                },
                "ItemIds": [item_id(identifier, current.get("ItemId", {}).get("ChangeKey"))],
                "ReturnMovedItemIds": True,
            },
        )
        return self.fetcher.refresh_after_write(
            returned_id(response, fallback=identifier),
            previous_id=identifier,
            folder_override=selected,
        )

    def draft(
        self,
        *,
        to: str | Sequence[str],
        subject: str,
        body: str,
        cc: Sequence[str] = (),
        bcc: Sequence[str] = (),
        attachments: Sequence[Path] = (),
    ) -> dict[str, Any]:
        to_recipients = recipients(to)
        cc_recipients, bcc_recipients = recipients(cc), recipients(bcc)
        files = file_attachments(attachments)
        if not to_recipients or not subject.strip() or not body.strip():
            raise ValueError("Draft needs a recipient email, subject, and body.")
        response = response_messages(
            self.client.write_action(
                "CreateItem",
                {
                    "MessageDisposition": "SaveOnly",
                    "ComposeOperation": "newMail",
                    "Items": [
                        {
                            "__type": "Message:#Exchange",
                            "Subject": subject,
                            "Body": {"BodyType": "Text", "Value": body},
                            "ToRecipients": to_recipients,
                            "CcRecipients": cc_recipients,
                            "BccRecipients": bcc_recipients,
                        }
                    ],
                },
            )
        )[0]
        return self._finish_draft(returned_id(response), files)

    def reply_draft(
        self, identifier: str, *, body: str, attachments: Sequence[Path] = ()
    ) -> dict[str, Any]:
        if not body.strip():
            raise ValueError("Reply draft body must not be empty.")
        files = file_attachments(attachments)
        original = self.client.message(identifier)
        mailbox = (original.get("ReplyTo") or [original.get("From") or original.get("Sender")])[0]
        if isinstance(mailbox, dict) and isinstance(mailbox.get("Mailbox"), dict):
            mailbox = mailbox["Mailbox"]
        if not isinstance(mailbox, dict) or not mailbox.get("EmailAddress"):
            raise ValueError("Original message has no reply recipient.")
        subject = str(original.get("Subject") or "")
        if not subject.casefold().startswith("re:"):
            subject = "Re: " + subject
        response = response_messages(
            self.client.write_action(
                "CreateItem",
                {
                    "MessageDisposition": "SaveOnly",
                    "ComposeOperation": "newMail",
                    "Items": [
                        {
                            "__type": "ReplyToItem:#Exchange",
                            "ReferenceItemId": item_id(
                                identifier, original.get("ItemId", {}).get("ChangeKey")
                            ),
                            "NewBodyContent": {"BodyType": "Text", "Value": body},
                            "Subject": subject,
                            "ToRecipients": [
                                {
                                    "EmailAddress": mailbox["EmailAddress"],
                                    "Name": mailbox.get("Name") or mailbox["EmailAddress"],
                                    "MailboxType": "OneOff",
                                    "RoutingType": "SMTP",
                                }
                            ],
                        }
                    ],
                },
            )
        )[0]
        return self._finish_draft(returned_id(response), files)

    def _draft(self, identifier: str) -> dict[str, Any]:
        current = self.client.message(identifier)
        if current.get("IsDraft") is not True:
            raise ValueError("Only drafts can be changed or sent with this command.")
        if current.get("IsSubmitted"):
            raise ValueError(
                "Draft was already submitted; check Sent Items instead of sending again."
            )
        return current

    def _finish_draft(self, identifier: str, files: list[dict[str, Any]]) -> dict[str, Any]:
        if files:
            current = self._draft(identifier)
            self._write_existing(
                identifier,
                "CreateAttachment",
                {
                    "ParentItemId": item_id(identifier, current.get("ItemId", {}).get("ChangeKey")),
                    "Attachments": files,
                },
            )
        return self.fetcher.refresh_after_write(identifier, folder_override="drafts")

    def edit_draft(
        self,
        identifier: str,
        *,
        subject: str | None = None,
        body: str | None = None,
        to: Sequence[str] | None = None,
        cc: Sequence[str] | None = None,
        bcc: Sequence[str] | None = None,
        attachments: Sequence[Path] = (),
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for name, addresses in (("ToRecipients", to), ("CcRecipients", cc), ("BccRecipients", bcc)):
            if addresses is not None:
                fields[name] = recipients(addresses)
        if subject is not None:
            fields["Subject"] = subject
        if body is not None:
            fields["Body"] = {"BodyType": "Text", "Value": body}
        files = file_attachments(attachments)
        if not fields and not files:
            raise ValueError("Specify a draft field to edit or --attach a file.")
        current = self._draft(identifier)
        if fields:
            result = self._update(identifier, current, fields)
            if not files:
                return result
        return self._finish_draft(identifier, files)

    def send_draft(self, identifier: str) -> dict[str, Any]:
        current = self._draft(identifier)
        if not any(
            current.get(field) for field in ("ToRecipients", "CcRecipients", "BccRecipients")
        ):
            raise ValueError("Draft has no recipients.")
        # The web client submits an existing draft with UpdateItem, not CreateItem.
        # A no-op subject update preserves its body, attachments and unknown fields.
        try:
            return self._update(
                identifier, current, {"Subject": current.get("Subject") or ""}, send=True
            )
        except OutlookTransportError as exc:
            self.store.forget_message(identifier)
            raise OutlookTransportError(
                "Send outcome is unknown. Do not resend; check Sent Items first."
            ) from exc

    def remove_draft(self, identifier: str) -> dict[str, Any]:
        current = self._draft(identifier)
        response = self._write_existing(
            identifier,
            "DeleteItem",
            {
                "ItemIds": [item_id(identifier, current.get("ItemId", {}).get("ChangeKey"))],
                "DeleteType": "MoveToDeletedItems",
                "SuppressReadReceipts": True,
                "ReturnMovedItemIds": True,
                "SendMeetingCancellations": "SendToNone",
                "AffectedTaskOccurrences": "AllOccurrences",
            },
        )
        return self.fetcher.refresh_after_write(
            returned_id(response, fallback=identifier),
            previous_id=identifier,
            folder_override="deleteditems",
        )
