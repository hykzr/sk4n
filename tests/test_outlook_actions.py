from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from sk4n.outlook.actions import OutlookActions
from sk4n.outlook.client import OutlookClient, OutlookError, search_results
from sk4n.outlook.storage import OutlookStore, canonical_immutable_id


def item(identifier: str, folder: str = "inbox", **fields: Any) -> dict[str, Any]:
    return {
        "ItemId": {"Id": identifier, "ChangeKey": "v1"},
        "ParentFolderId": {"Id": folder},
        "Subject": "Test",
        "Body": {"BodyType": "Text", "Value": "Original body"},
        "IsRead": True,
        "IsDraft": False,
        "Flag": {"FlagStatus": "NotFlagged"},
        "From": {"Mailbox": {"EmailAddress": "sender@example.com"}},
        **fields,
    }


class Remote(OutlookClient):
    def __init__(self) -> None:
        super().__init__()
        self.items = {"one": item("one")}
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.next_id = "new"

    def message(self, message_id: str) -> dict[str, Any]:
        return deepcopy(self.items[message_id])

    def write_action(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        self.writes.append((name, deepcopy(body)))
        if name == "UpdateItem":
            change = body["ItemChanges"][0]
            identifier = change["ItemId"]["Id"]
            update = change["Updates"][0]
            field = update["Path"]["FieldURI"]
            self.items[identifier][field] = update["Item"][field]
            returned: dict[str, Any] = {"Items": [{"ItemId": {"Id": identifier}}]}
        elif name == "MoveItem":
            identifier = body["ItemIds"][0]["Id"]
            moved = deepcopy(self.items.pop(identifier))
            moved["ItemId"] = {"Id": self.next_id, "ChangeKey": "v2"}
            moved["ParentFolderId"] = {
                "Id": body["ToFolderId"]["BaseFolderId"]["Id"]
            }
            self.items[self.next_id] = moved
            returned = {"Items": [{"ItemId": moved["ItemId"]}]}
        elif name == "CreateItem":
            source = body["Items"][0]
            reply = source["__type"] == "ReplyToItem:#Exchange"
            created = item(
                self.next_id,
                "drafts",
                IsDraft=True,
                Subject=source.get("Subject", ""),
                Body={
                    "BodyType": "Text",
                    "Value": (source.get("NewBodyContent") or source.get("Body"))["Value"],
                },
                ToRecipients=source["ToRecipients"],
                **({"InReplyTo": "original-id"} if reply else {}),
            )
            self.items[self.next_id] = created
            returned = {"Items": [{"ItemId": created["ItemId"]}]}
        else:
            identifier = body["ItemIds"][0]["Id"]
            self.items[identifier]["ParentFolderId"] = {"Id": "deleteditems"}
            returned = {"MovedItemId": {"Id": identifier}}
        return {"Body": {"ResponseMessages": {"Items": [{"ResponseCode": "NoError", **returned}]}}}


@pytest.fixture
def setup(tmp_path: Path) -> tuple[OutlookActions, Remote, OutlookStore]:
    remote = Remote()
    store = OutlookStore(tmp_path / "outlook")
    store.save_message(
        {
            "message": remote.message("one"),
            "metadata": remote.message("one"),
            "folder": "inbox",
            "complete": True,
            "body_current": True,
        }
    )
    return OutlookActions(remote, store), remote, store


def test_item_scoped_flag_and_read_refresh_cache_even_with_unchanged_change_key(
    setup: tuple[OutlookActions, Remote, OutlookStore],
) -> None:
    actions, remote, store = setup
    assert actions.flag("one", flagged=True)["Flag"]["FlagStatus"] == "Flagged"
    assert (record := store.message("one")) is not None
    assert record["metadata"]["Flag"]["FlagStatus"] == "Flagged"
    result = actions.mark_read("one", read=False)
    assert result["IsRead"] is False
    assert result["_outlook"]["body_current"] is True
    assert (record := store.message("one")) is not None
    assert record["metadata"]["IsRead"] is False
    assert [name for name, _ in remote.writes] == ["UpdateItem", "UpdateItem"]
    assert remote.writes[1][1]["ItemChanges"][0]["Updates"][0]["Path"]["FieldURI"] == "IsRead"


def test_move_rekeys_cache_when_server_changes_item_id(
    setup: tuple[OutlookActions, Remote, OutlookStore],
) -> None:
    actions, remote, store = setup
    result = actions.move("one", folder="archive")
    assert result["ItemId"]["Id"] == "new"
    assert result["ParentFolderId"]["Id"] == "archive"
    assert store.message("one") is None
    assert (record := store.message("new")) is not None
    assert record["folder"] == "archive"
    assert remote.writes[0][1]["ToFolderId"]["BaseFolderId"]["Id"] == "archive"


def test_create_reply_and_remove_draft_use_save_only_and_reconcile_folder(
    setup: tuple[OutlookActions, Remote, OutlookStore],
) -> None:
    actions, remote, store = setup
    created = actions.draft(to="personal@example.com", subject="Test draft", body="Hello")
    assert created["IsDraft"] is True
    assert (record := store.message("new")) is not None
    assert record["folder"] == "drafts"
    assert remote.writes[0][1]["MessageDisposition"] == "SaveOnly"
    assert remote.writes[0][1]["Items"][0]["ToRecipients"][0]["EmailAddress"] == "personal@example.com"
    remote.next_id = "reply"
    reply = actions.reply_draft("one", body="Thanks")
    assert reply["InReplyTo"] == "original-id"
    assert remote.writes[1][1]["Items"][0]["__type"] == "ReplyToItem:#Exchange"
    assert remote.writes[1][1]["Items"][0]["ReferenceItemId"]["Id"] == "one"
    removed = actions.remove_draft("reply")
    assert removed["ParentFolderId"]["Id"] == "deleteditems"
    assert (record := store.message("reply")) is not None
    assert record["folder"] == "deleteditems"
    assert remote.writes[2][1]["DeleteType"] == "MoveToDeletedItems"
    with pytest.raises(ValueError, match="Only drafts"):
        actions.remove_draft("one")
    assert len(remote.writes) == 3


def test_search_immutable_id_reuses_list_cache_key_and_forgets_legacy_alias(tmp_path: Path) -> None:
    original = "AAk+part/ending=="
    immutable = "AAk_part-ending=="
    assert canonical_immutable_id(immutable) == original
    payload = {
        "EntitySets": [
            {
                "ResultSets": [
                    {
                        "Results": [
                            {
                                "Source": {
                                    "ItemId": {"Id": "folder-dependent-id"},
                                    "ImmutableId": immutable,
                                    "ItemRestId": "folder-dependent-id",
                                    "Subject": "Test",
                                }
                            }
                        ]
                    }
                ]
            }
        ]
    }
    normalized = search_results(payload)[0]
    assert normalized["ItemId"]["Id"] == original
    assert normalized["ItemRestId"] == "folder-dependent-id"
    assert normalized["Flag"]["FlagStatus"] == "NotFlagged"
    store = OutlookStore(tmp_path)
    store.save_message({"message": {"ItemId": {"Id": original}, "Subject": "Test"}})
    store.save_message(
        {
            "message": {
                "ItemId": {"Id": "folder-dependent-id"},
                "ImmutableId": immutable,
                "Subject": "Test",
            }
        }
    )
    store.forget_search_aliases({original})
    assert store.message(original) is not None
    assert store.message("folder-dependent-id") is None


def test_successful_write_invalidates_cache_if_followup_read_fails(tmp_path: Path) -> None:
    class FailingRemote(Remote):
        written = False

        def message(self, message_id: str) -> dict[str, Any]:
            if self.written:
                raise OutlookError("Follow-up read failed")
            return super().message(message_id)

        def write_action(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
            response = super().write_action(name, body)
            self.written = True
            return response

    remote = FailingRemote()
    store = OutlookStore(tmp_path)
    store.save_message({"message": remote.message("one")})
    with pytest.raises(OutlookError, match="Follow-up read failed"):
        OutlookActions(remote, store).flag("one", flagged=True)
    assert store.message("one") is None


def test_write_actions_do_not_expose_sending() -> None:
    with pytest.raises(ValueError, match="Unsupported Outlook write action"):
        OutlookClient().write_action("SendItem", {})
