from __future__ import annotations

from typing import Any

from .client import OutlookClient, OutlookError, folder_id, response_messages
from .fetcher import OutlookFetcher
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


class OutlookActions:
    """Explicit, item-scoped mailbox writes with immediate cache reconciliation."""

    def __init__(self, client: OutlookClient, store: OutlookStore):
        self.client = client
        self.store = store
        self.fetcher = OutlookFetcher(client, store)

    def _set_field(self, identifier: str, field: str, value: Any) -> dict[str, Any]:
        current = self.client.message(identifier)
        change_key = current.get("ItemId", {}).get("ChangeKey")
        response = response_messages(
            self.client.write_action(
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
                            ],
                        }
                    ],
                    "ConflictResolution": "AutoResolve",
                    "MessageDisposition": "SaveOnly",
                    "SuppressReadReceipts": True,
                },
            )
        )[0]
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
        response = response_messages(
            self.client.write_action(
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
        )[0]
        return self.fetcher.refresh_after_write(
            returned_id(response, fallback=identifier),
            previous_id=identifier,
            folder_override=selected,
        )

    def draft(
        self, *, to: str, subject: str, body: str
    ) -> dict[str, Any]:
        to = to.strip()
        if not to or "@" not in to or not subject.strip() or not body.strip():
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
                            "ToRecipients": [
                                {"EmailAddress": to, "MailboxType": "OneOff", "RoutingType": "SMTP"}
                            ],
                        }
                    ],
                },
            )
        )[0]
        return self.fetcher.refresh_after_write(returned_id(response), folder_override="drafts")

    def reply_draft(self, identifier: str, *, body: str) -> dict[str, Any]:
        if not body.strip():
            raise ValueError("Reply draft body must not be empty.")
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
        return self.fetcher.refresh_after_write(returned_id(response), folder_override="drafts")

    def remove_draft(self, identifier: str) -> dict[str, Any]:
        current = self.client.message(identifier)
        if current.get("IsDraft") is not True:
            raise ValueError("Only drafts can be removed with this command.")
        response = response_messages(
            self.client.write_action(
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
        )[0]
        return self.fetcher.refresh_after_write(
            returned_id(response, fallback=identifier),
            previous_id=identifier,
            folder_override="deleteditems",
        )
