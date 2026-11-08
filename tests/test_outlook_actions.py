from __future__ import annotations

import base64
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from sk4n.outlook.actions import OutlookActions, recipients, returned_id
from sk4n.outlook.client import OutlookClient, OutlookError, OutlookTransportError, search_results
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
            for update in change["Updates"]:
                field = update["Path"]["FieldURI"]
                self.items[identifier][field] = update["Item"][field]
            if body["MessageDisposition"] == "SendAndSaveCopy":
                self.items[identifier]["IsDraft"] = False
                self.items[identifier]["ParentFolderId"] = {"Id": "sentitems"}
            returned: dict[str, Any] = {"Items": [{"ItemId": {"Id": identifier}}]}
        elif name == "CreateAttachment":
            identifier = body["ParentItemId"]["Id"]
            existing = self.items[identifier].setdefault("Attachments", [])
            for file in body["Attachments"]:
                existing.append({**file, "AttachmentId": {"Id": f"file-{len(existing)}"}})
            self.items[identifier]["HasAttachments"] = True
            returned = {"Attachments": existing}
        elif name == "MoveItem":
            identifier = body["ItemIds"][0]["Id"]
            moved = deepcopy(self.items.pop(identifier))
            moved["ItemId"] = {"Id": self.next_id, "ChangeKey": "v2"}
            moved["ParentFolderId"] = {"Id": body["ToFolderId"]["BaseFolderId"]["Id"]}
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


@pytest.mark.parametrize("folder_error", [False, True])
def test_write_refresh_resolves_opaque_parent_and_invalidates_cache_on_resolution_failure(
    setup: tuple[OutlookActions, Remote, OutlookStore],
    monkeypatch: pytest.MonkeyPatch,
    folder_error: bool,
) -> None:
    actions, remote, store = setup
    remote.items["one"]["ParentFolderId"] = {"Id": "opaque-inbox-id"}

    def folders() -> list[dict[str, Any]]:
        if folder_error:
            raise OutlookError("Folder lookup failed")
        return [
            {
                "FolderId": {"Id": "opaque-inbox-id"},
                "DisplayName": "Inbox",
                "DistinguishedFolderId": "inbox",
            }
        ]

    monkeypatch.setattr(remote, "folders", folders)
    if folder_error:
        with pytest.raises(OutlookError, match="Folder lookup failed"):
            actions.mark_read("one", read=False)
        assert store.message("one") is None
    else:
        actions.mark_read("one", read=False)
        assert (record := store.message("one")) is not None
        assert record["folder"] == "inbox"
        assert len(actions.fetcher.messages(refresh_mode="none")) == 1
    assert remote.items["one"]["IsRead"] is False


def test_create_reply_and_remove_draft_use_save_only_and_reconcile_folder(
    setup: tuple[OutlookActions, Remote, OutlookStore],
) -> None:
    actions, remote, store = setup
    created = actions.draft(to="personal@example.com", subject="Test draft", body="Hello")
    assert created["IsDraft"] is True
    assert (record := store.message("new")) is not None
    assert record["folder"] == "drafts"
    assert remote.writes[0][1]["MessageDisposition"] == "SaveOnly"
    assert (
        remote.writes[0][1]["Items"][0]["ToRecipients"][0]["EmailAddress"] == "personal@example.com"
    )
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


def test_sending_does_not_add_senditem_to_low_level_actions() -> None:
    with pytest.raises(ValueError, match="Unsupported Outlook write action"):
        OutlookClient().write_action("SendItem", {})


@pytest.mark.parametrize("action", ["UpdateItem", "CreateAttachment", "SendItem"])
def test_low_level_api_stays_read_only(action: str) -> None:
    with pytest.raises(ValueError, match="read-only"):
        OutlookClient().request("POST", "/owa/service.svc?action=" + action, {})


def test_edit_and_attach_preserve_other_fields_and_send_only_existing_draft(
    setup: tuple[OutlookActions, Remote, OutlookStore],
    tmp_path: Path,
) -> None:
    actions, remote, store = setup
    data = b"\x00binary\xff preview"
    file = tmp_path / "preview.png"
    file.write_bytes(data)
    actions.reply_draft("one", body="Review me")
    edited = actions.edit_draft(
        "new", subject="Edited", body="Reviewed", to=["self@example.com"], attachments=[file]
    )
    assert edited["Body"]["Value"] == "Reviewed"
    assert edited["InReplyTo"] == "original-id"
    assert edited["ToRecipients"][0]["EmailAddress"] == "self@example.com"
    upload = remote.writes[-1][1]
    assert upload["ParentItemId"]["Id"] == "new"
    assert base64.b64decode(upload["Attachments"][0]["Content"]) == data
    sent = actions.send_draft("new")
    assert sent["status"] == "submitted"
    assert sent["IsDraft"] is False
    assert sent["Attachments"][0]["Name"] == "preview.png"
    assert sent["Body"]["Value"] == "Reviewed"
    name, send = remote.writes[-1]
    assert name == "UpdateItem"
    assert send["MessageDisposition"] == "SendAndSaveCopy"
    assert send["SendOnNotFoundError"] is False
    assert send["ConflictResolution"] == "NeverOverwrite"
    assert len(send["ItemChanges"]) == 1
    assert [u["Path"]["FieldURI"] for u in send["ItemChanges"][0]["Updates"]] == ["Subject"]
    assert (record := store.message("new")) is not None
    assert record["folder"] == "sentitems"
    writes = len(remote.writes)
    with pytest.raises(ValueError, match="Only drafts"):
        actions.send_draft("new")
    with pytest.raises(ValueError, match="Only drafts"):
        actions.edit_draft("one", body="No")
    assert len(remote.writes) == writes


def test_draft_validation_prevents_partial_writes(
    setup: tuple[OutlookActions, Remote, OutlookStore],
    tmp_path: Path,
) -> None:
    actions, remote, _ = setup
    with pytest.raises(FileNotFoundError):
        actions.draft(
            to="self@example.com", subject="Test", body="Hello", attachments=[tmp_path / "missing"]
        )
    for invalid in ("not-an-email", "@", "user@", "@example.com", "a@b@example.com"):
        with pytest.raises(ValueError, match="email addresses"):
            recipients(invalid)
    with pytest.raises(ValueError, match="email addresses"):
        actions.draft(to="not-an-email", subject="Test", body="Hello")
    assert remote.writes == []
    actions.draft(
        to=["one@example.com", "two@example.com"],
        cc=["cc@example.com"],
        bcc=["bcc@example.com"],
        subject="Test",
        body="Hello",
    )
    assert len(remote.writes[0][1]["Items"][0]["ToRecipients"]) == 2
    with pytest.raises(ValueError, match="Specify a draft field"):
        actions.edit_draft("new")
    remote.items["new"]["ToRecipients"] = []
    with pytest.raises(ValueError, match="no recipients"):
        actions.send_draft("new")
    assert len(remote.writes) == 1


def test_accepted_send_with_failed_cache_read_is_reported_as_submitted(tmp_path: Path) -> None:
    class SentButUnreadable(Remote):
        def message(self, message_id: str) -> dict[str, Any]:
            if self.writes and self.writes[-1][1].get("MessageDisposition") == "SendAndSaveCopy":
                raise OutlookError("temporarily unavailable")
            return super().message(message_id)

    remote = SentButUnreadable()
    store = OutlookStore(tmp_path)
    actions = OutlookActions(remote, store)
    actions.draft(to="self@example.com", subject="Test", body="Hello")
    result = actions.send_draft("new")
    assert result["status"] == "submitted"
    assert result["cache_refreshed"] is False
    assert "Do not resend" in result["warning"]
    assert store.message("new") is None


def test_pending_submission_cannot_be_sent_edited_or_discarded_again(
    setup: tuple[OutlookActions, Remote, OutlookStore],
) -> None:
    actions, remote, _ = setup
    actions.draft(to="self@example.com", subject="Test", body="Hello")
    remote.items["new"]["IsSubmitted"] = True
    for action in (
        lambda: actions.send_draft("new"),
        lambda: actions.edit_draft("new", body="Too late"),
        lambda: actions.remove_draft("new"),
    ):
        with pytest.raises(ValueError, match="already submitted"):
            action()
    assert len(remote.writes) == 1


def test_uncertain_send_invalidates_cache_and_does_not_retry(tmp_path: Path) -> None:
    class Disconnected(Remote):
        def write_action(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
            result = super().write_action(name, body)
            if body.get("MessageDisposition") == "SendAndSaveCopy":
                raise OutlookTransportError("connection lost after submission")
            return result

    remote = Disconnected()
    store = OutlookStore(tmp_path)
    actions = OutlookActions(remote, store)
    actions.draft(to="self@example.com", subject="Test", body="Hello")
    with pytest.raises(OutlookTransportError, match="Do not resend"):
        actions.send_draft("new")
    assert len(remote.writes) == 2
    assert store.message("new") is None


def test_pending_send_retains_actual_folder_until_transport_moves_it(tmp_path: Path) -> None:
    class Pending(Remote):
        def write_action(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
            result = super().write_action(name, body)
            if body.get("MessageDisposition") == "SendAndSaveCopy":
                self.items["new"].update(
                    IsDraft=True, IsSubmitted=True, ParentFolderId={"Id": "drafts"}
                )
            return result

    store = OutlookStore(tmp_path)
    actions = OutlookActions(Pending(), store)
    actions.draft(to="self@example.com", subject="Test", body="Hello")
    result = actions.send_draft("new")
    assert result["status"] == "submitted" and result["pending"] is True
    assert (record := store.message("new")) is not None
    assert record["folder"] == "drafts"
    assert record["metadata"]["IsSubmitted"] is True


def test_partial_attachment_failure_invalidates_cached_draft(tmp_path: Path) -> None:
    class PartialUpload(Remote):
        def write_action(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
            result = super().write_action(name, body)
            if name == "CreateAttachment":
                raise OutlookError("second upload failed")
            return result

    remote = PartialUpload()
    store = OutlookStore(tmp_path / "cache")
    actions = OutlookActions(remote, store)
    actions.draft(to="self@example.com", subject="Test", body="Hello")
    file = tmp_path / "preview.png"
    file.write_bytes(b"preview")
    with pytest.raises(OutlookError, match="upload failed"):
        actions.edit_draft("new", attachments=[file])
    assert store.message("new") is None
    assert remote.items["new"]["Attachments"][0]["Name"] == "preview.png"


@pytest.mark.parametrize("command", ["edit", "attach", "flag", "read", "move", "remove", "send"])
def test_every_named_write_invalidates_preview_even_with_unchanged_change_key(
    command: str,
    setup: tuple[OutlookActions, Remote, OutlookStore],
) -> None:
    actions, remote, store = setup
    actions.draft(to="self@example.com", subject="Test", body="Hello")
    record = store.message("new")
    path = store.message_path("new")
    assert record is not None and path is not None
    image = path.parent / "preview.png"
    image.write_bytes(b"old screenshot")
    record["preview"] = {"path": "preview.png"}
    store.save_message(record)
    if command == "edit":
        result = actions.edit_draft("new", body="Updated")
        assert result["Body"]["Value"] == "Updated"
        assert result["ItemId"]["ChangeKey"] == "v1"
    elif command == "attach":
        actions.edit_draft("new", attachments=[image])
        assert (
            base64.b64decode(remote.writes[-1][1]["Attachments"][0]["Content"]) == b"old screenshot"
        )
    elif command == "flag":
        actions.flag("new", flagged=True)
    elif command == "read":
        actions.mark_read("new", read=False)
    elif command == "move":
        actions.move("new", folder="archive")
    elif command == "remove":
        actions.remove_draft("new")
    else:
        actions.send_draft("new")
    assert not image.exists()
    assert (updated := store.message("new")) is not None
    assert "preview" not in updated


@pytest.mark.parametrize("failure", ["response", "reread"])
def test_edit_failure_invalidates_message_and_preview(tmp_path: Path, failure: str) -> None:
    class Failing(Remote):
        written = False

        def write_action(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
            result = super().write_action(name, body)
            if name == "UpdateItem":
                self.written = True
                if failure == "response":
                    raise OutlookTransportError("lost update response")
            return result

        def message(self, message_id: str) -> dict[str, Any]:
            if self.written and failure == "reread":
                raise OutlookError("failed post-write read")
            return super().message(message_id)

    remote = Failing()
    store = OutlookStore(tmp_path)
    actions = OutlookActions(remote, store)
    actions.draft(to="self@example.com", subject="Test", body="Hello")
    path = store.message_path("new")
    assert path is not None
    image = path.parent / "preview.png"
    image.write_bytes(b"old screenshot")
    with pytest.raises(OutlookError):
        actions.edit_draft("new", body="Changed remotely")
    assert store.message("new") is None
    assert not image.exists()
    assert remote.items["new"]["Body"]["Value"] == "Changed remotely"


def test_returned_id_tolerates_explicit_null_fields() -> None:
    assert (
        returned_id({"Items": None, "MovedItemId": None}, fallback="fallback-id") == "fallback-id"
    )
    assert (
        returned_id({"Items": [{"ItemId": None}], "MovedItemId": {"Id": "moved-id"}}) == "moved-id"
    )


def test_combined_edit_and_attach_failure_invalidates_cached_draft(tmp_path: Path) -> None:
    class PartialCombined(Remote):
        def write_action(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
            result = super().write_action(name, body)
            if name == "CreateAttachment":
                raise OutlookError("attachment upload failed after field update")
            return result

    remote = PartialCombined()
    store = OutlookStore(tmp_path / "cache")
    actions = OutlookActions(remote, store)
    actions.draft(to="self@example.com", subject="Test", body="Hello")
    file = tmp_path / "preview.png"
    file.write_bytes(b"preview")
    with pytest.raises(OutlookError, match="attachment upload failed"):
        actions.edit_draft("new", body="Updated before upload failure", attachments=[file])
    assert store.message("new") is None
