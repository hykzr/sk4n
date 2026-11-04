from __future__ import annotations

import base64
import copy
import json
import os
import stat
from pathlib import Path, PureWindowsPath
from typing import Any, cast
from urllib.parse import quote, urlsplit

import pytest

from sk4n.outlook import cli, client, storage
from sk4n.outlook.fetcher import OutlookFetcher
from sk4n.outlook.links import attachment_url, message_url
from sk4n.outlook.storage import OutlookStore, complete_message, safe_name, same_revision


def message(identifier: str = "one", change: str = "v1", **extra: Any) -> dict[str, Any]:
    return {
        "ItemId": {"Id": identifier, "ChangeKey": change},
        "Subject": "Example subject",
        "Body": {"BodyType": "Text", "Value": "Full message body"},
        "DateTimeReceived": "2026-11-01T00:00:00Z",
        "LastModifiedTime": "2026-11-01T00:00:00Z",
        "IsRead": False,
        "Attachments": [],
        "UnknownField": {"keep": True},
        **extra,
    }


def folder(
    name: str = "Projects", identifier: str = "folder-one", parent: str = "root"
) -> dict[str, Any]:
    return {
        "DisplayName": name,
        "FolderId": {"Id": identifier},
        "ParentFolderId": {"Id": parent},
        "UnknownField": "keep",
    }


def attachment(identifier: str = "attachment-one", **extra: Any) -> dict[str, Any]:
    return {
        "__type": "FileAttachment:#Exchange",
        "AttachmentId": {"Id": identifier},
        "Name": "report.pdf",
        "LastModifiedTime": "2026-11-01T00:00:00",
        "Size": 5,
        **extra,
    }


class Remote:
    base_url = client.DEFAULT_BASE_URL

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.folder_list = [folder()]
        self.items = {"one": message()}
        self.summaries: list[dict[str, Any]] | None = None
        self.folder_errors: dict[str, str] = {}
        self.payloads: dict[str, dict[str, Any]] = {
            "attachment-one": {"Content": base64.b64encode(b"hello").decode(), "Extra": "keep"}
        }

    def folders(self) -> list[dict[str, Any]]:
        self.calls.append(("folders", None))
        return copy.deepcopy(self.folder_list)

    def messages(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("messages", kwargs))
        if kwargs["folder"] in self.folder_errors:
            raise client.OutlookError(
                "missing folder", response_code=self.folder_errors[kwargs["folder"]]
            )
        if self.summaries is not None:
            return copy.deepcopy(self.summaries)
        return [
            {
                k: v
                for k, v in item.items()
                if k not in {"Body", "Attachments", "UnknownField", "LastModifiedTime"}
            }
            for item in copy.deepcopy(list(self.items.values()))
        ]

    def message(self, identifier: str) -> dict[str, Any]:
        self.calls.append(("message", identifier))
        return copy.deepcopy(self.items[identifier])

    def message_metadata(self, identifier: str) -> dict[str, Any]:
        self.calls.append(("metadata", identifier))
        return {"ItemId": copy.deepcopy(self.items[identifier]["ItemId"])}

    def folder_metadata(self, identifier: str) -> dict[str, Any]:
        self.calls.append(("folder_metadata", identifier))
        if identifier in self.folder_errors:
            raise client.OutlookError("missing folder", response_code="ErrorFolderNotFound")
        return folder(identifier=identifier)

    def attachment(self, identifier: str) -> dict[str, Any]:
        self.calls.append(("attachment", identifier))
        return copy.deepcopy(self.payloads[identifier])


@pytest.fixture
def cached(tmp_path: Path) -> tuple[OutlookFetcher, OutlookStore, Remote]:
    remote = Remote()
    store = OutlookStore(tmp_path / "outlook")
    return OutlookFetcher(cast(client.OutlookClient, remote), store), store, remote


def test_folders_refresh_by_default_and_preserve_mapping(cached: Any) -> None:
    fetcher, store, remote = cached
    assert fetcher.folders() == remote.folder_list
    remote.folder_list = [folder("Renamed", "new")]
    assert fetcher.folders(refresh_mode="none")[0]["DisplayName"] == "Projects"
    assert fetcher.folders() == remote.folder_list
    assert store.folders() == remote.folder_list
    assert [c[0] for c in remote.calls] == ["folders", "folders"]


def test_cache_misses_are_strictly_offline_and_do_not_create_directories(cached: Any) -> None:
    fetcher, store, remote = cached
    for operation in [
        lambda: fetcher.folders(refresh_mode="none"),
        lambda: fetcher.message("one", refresh_mode="none"),
        lambda: fetcher.messages(folder="Projects", refresh_mode="none"),
    ]:
        with pytest.raises(client.OutlookError):
            operation()
    assert remote.calls == []
    assert fetcher.messages(refresh_mode="none") == []
    assert not store.root.exists()


def test_folder_name_resolution_cached_and_missing_refresh_once(cached: Any) -> None:
    fetcher, _store, remote = cached
    fetcher.messages(folder="projects")
    assert [c[0] for c in remote.calls] == ["folders", "messages"]
    assert remote.calls[1][1]["folder"] == "folder-one"
    remote.calls.clear()
    fetcher.messages(folder="Projects")
    assert [c[0] for c in remote.calls] == ["messages"]
    assert fetcher.messages(folder="Projects", refresh_mode="none")
    remote.folder_list.append(folder("New", "folder-new"))
    fetcher.messages(folder="New")
    assert remote.calls[-2][0] == "folders"
    assert remote.calls[-1][1]["folder"] == "folder-new"
    remote.calls.clear()
    with pytest.raises(client.OutlookError, match="mapping"):
        fetcher.messages(folder="Missing")
    assert remote.calls == [("folders", None)]


@pytest.mark.parametrize("mode", ["default", "force"])
def test_stale_folder_refresh_is_bounded(cached: Any, mode: str) -> None:
    fetcher, store, remote = cached
    store.save_folders([folder(identifier="old")])
    remote.folder_errors["old"] = "ErrorFolderNotFound"
    fetcher.messages(folder="Projects", refresh_mode=mode)
    assert [c[1]["folder"] for c in remote.calls if c[0] == "messages"] == (
        ["old", "folder-one"] if mode == "default" else ["folder-one"]
    )
    assert [c[0] for c in remote.calls].count("folders") == 1
    remote.calls.clear()
    store.save_folders([folder(identifier="old")])
    remote.folder_list = [folder(identifier="old")]
    with pytest.raises(client.OutlookError, match="missing folder"):
        fetcher.messages(folder="Projects", refresh_mode=mode)
    assert [c[0] for c in remote.calls].count("folders") == 1
    assert [c[0] for c in remote.calls].count("messages") <= 2


def test_renamed_folder_fails_after_one_mapping_refresh(cached: Any) -> None:
    fetcher, store, remote = cached
    store.save_folders([folder(identifier="old")])
    remote.folder_errors["old"] = "ErrorFolderNotFound"
    remote.folder_list = [folder("Renamed")]
    with pytest.raises(client.OutlookError, match="mapping"):
        fetcher.messages(folder="Projects")
    assert [c[0] for c in remote.calls] == ["messages", "folders"]


def test_unrelated_folder_errors_do_not_retry(cached: Any) -> None:
    fetcher, store, remote = cached
    store.save_folders([folder()])
    remote.folder_errors["folder-one"] = "ErrorAccessDenied"
    with pytest.raises(client.OutlookError):
        fetcher.messages(folder="Projects")
    assert [c[0] for c in remote.calls] == ["messages"]


def test_search_generic_bad_request_checks_folder_before_retry(cached: Any) -> None:
    fetcher, store, remote = cached
    store.save_folders([folder(identifier="old")])
    remote.folder_errors["old"] = "HTTP400"
    fetcher.messages(folder="Projects", query="subject:test")
    assert [c[0] for c in remote.calls] == [
        "messages",
        "folder_metadata",
        "folders",
        "messages",
    ]


def test_duplicate_folder_names_require_path_or_id(cached: Any) -> None:
    fetcher, store, remote = cached
    store.save_folders(
        [folder("Parent", "parent"), folder("Projects", "a", "parent"), folder("Projects", "b")]
    )
    with pytest.raises(ValueError, match="ambiguous"):
        fetcher.messages(folder="Projects")
    assert remote.calls == []
    fetcher.messages(folder="Parent/Projects")
    assert remote.calls[0][1]["folder"] == "a"
    fetcher.messages(folder_id="b")
    assert remote.calls[-1][1]["folder"] == "b"


def test_long_folder_name_is_resolved_as_a_name(cached: Any) -> None:
    fetcher, _store, remote = cached
    name = "A-long-folder-name" * 8
    remote.folder_list = [folder(name)]
    fetcher.messages(folder=name)
    assert [c[0] for c in remote.calls] == ["folders", "messages"]
    assert remote.calls[1][1]["folder"] == "folder-one"


def test_distinguished_folder_alias_and_display_name_share_offline_page(cached: Any) -> None:
    fetcher, store, remote = cached
    store.save_folders([{**folder("Sent Items"), "DistinguishedFolderId": "sentitems"}])
    result = fetcher.messages(folder="sentitems")
    remote.calls.clear()
    assert fetcher.messages(folder="Sent Items", refresh_mode="none") == result
    assert remote.calls == []


def test_default_lists_only_cache_metadata_without_per_message_requests(cached: Any) -> None:
    fetcher, store, remote = cached
    first = fetcher.messages()
    assert "Body" not in first[0]
    assert first[0]["_outlook"]["body_downloaded"] is False
    path = Path(first[0]["_outlook"]["path"])
    assert path.is_relative_to(store.root / "emails")
    assert store.message("one")["complete"] is False
    assert [c[0] for c in remote.calls] == ["messages"]
    remote.calls.clear()
    assert fetcher.messages() == first
    assert remote.calls[0][0] == "messages" and len(remote.calls) == 1
    remote.calls.clear()
    assert fetcher.messages(refresh_mode="none") == first
    assert not remote.calls


def test_list_detects_changed_body_without_downloading_and_keeps_old_revision(cached: Any) -> None:
    fetcher, _store, remote = cached
    fetcher.messages(body=True)
    remote.calls.clear()
    remote.items["one"] = message(change="v2", Body={"Value": "Edited"}, IsRead=True)
    result = fetcher.messages()[0]
    assert "Body" not in result
    assert result["IsRead"] is True
    assert result["_outlook"]["body_downloaded"] is True
    assert result["_outlook"]["body_current"] is False
    assert [c[0] for c in remote.calls] == ["messages"]
    with pytest.raises(client.OutlookError, match="older message revision"):
        fetcher.message("one", refresh_mode="none")
    assert fetcher.message("one")["Body"]["Value"] == "Edited"
    assert [c[0] for c in remote.calls] == ["messages", "metadata", "message"]


def test_explicit_bodies_reuse_complete_content_and_default_list_stays_concise(cached: Any) -> None:
    fetcher, store, remote = cached
    full = fetcher.messages(body=True)[0]
    assert [c[0] for c in remote.calls] == ["messages", "message"]
    assert full["UnknownField"] == {"keep": True}
    assert full["_outlook"]["body_downloaded"] is True
    remote.calls.clear()
    assert fetcher.messages(body=True)[0]["Body"] == full["Body"]
    assert [c[0] for c in remote.calls] == ["messages"]
    remote.calls.clear()
    assert "Body" not in fetcher.messages()[0]
    assert store.message("one")["complete"] is True
    assert store.message("one")["message"]["Body"] == full["Body"]
    remote.calls.clear()
    assert "Body" not in fetcher.messages(refresh_mode="none")[0]
    assert fetcher.messages(refresh_mode="none", body=True)[0]["Body"] == full["Body"]
    assert remote.calls == []


def test_body_returned_by_list_api_is_cached_without_extra_request(cached: Any) -> None:
    fetcher, store, remote = cached
    remote.summaries = [message()]
    result = fetcher.messages()
    assert result[0]["Body"]["Value"] == "Full message body"
    assert result[0]["_outlook"]["body_downloaded"] is True
    assert store.message("one")["complete"] is True
    assert remote.calls[0][0] == "messages" and len(remote.calls) == 1


def test_force_list_refresh_does_not_implicitly_fetch_bodies(cached: Any) -> None:
    fetcher, store, remote = cached
    fetcher.messages(refresh_mode="force")
    assert [c[0] for c in remote.calls] == ["folders", "messages"]
    assert store.message("one")["complete"] is False


def test_direct_message_checks_only_revision_then_force_refreshes(cached: Any) -> None:
    fetcher, _store, remote = cached
    fetcher.message("one")
    remote.calls.clear()
    fetcher.message("one")
    assert remote.calls == [("metadata", "one")]
    remote.items["one"] = message(change="v2")
    fetcher.message("one")
    assert remote.calls[-2:] == [("metadata", "one"), ("message", "one")]
    remote.calls.clear()
    fetcher.message("one", refresh_mode="force")
    assert remote.calls == [("message", "one")]


def test_search_without_revision_never_fetches_individual_messages_unless_requested(
    cached: Any,
) -> None:
    fetcher, _store, remote = cached
    remote.summaries = [{"ItemId": {"Id": "one"}, "SearchOnlyField": "keep", "Preview": "partial"}]
    result = fetcher.messages(query="subject:test")
    assert result[0]["SearchOnlyField"] == "keep"
    assert "Body" not in result[0]
    assert result[0]["Preview"] == "partial"
    assert [c[0] for c in remote.calls] == ["messages"]
    fetcher.messages(query="another query", body=True)
    remote.calls.clear()
    fetcher.messages(query="subject:test")
    assert [c[0] for c in remote.calls] == ["messages"]
    remote.calls.clear()
    fetcher.messages(query="third query", body=True)
    assert [c[0] for c in remote.calls] == ["messages", "metadata"]


def test_search_fractional_timestamps_use_change_key_and_refresh_metadata(cached: Any) -> None:
    fetcher, _store, remote = cached
    remote.summaries = [
        {
            "ItemId": {"Id": "one"},
            "DateTimeLastModified": "2026-11-01T00:00:00.1234567Z",
            "IsRead": False,
        }
    ]
    fetcher.messages(query="term", body=True)
    remote.calls.clear()
    remote.summaries[0]["IsRead"] = True
    result = fetcher.messages(query="term")
    assert [c[0] for c in remote.calls] == ["messages"]
    assert result[0]["_outlook"]["body_downloaded"] is True
    assert result[0]["_outlook"]["body_current"] is True
    assert result[0]["IsRead"] is True
    assert "Body" not in result[0]
    assert fetcher.message("one")["Body"]["Value"] == "Full message body"
    assert [c[0] for c in remote.calls] == ["messages", "metadata"]


def test_incomplete_body_is_refetched_and_never_passed_off_as_complete(cached: Any) -> None:
    fetcher, _store, remote = cached
    remote.items["one"]["Body"]["IsTruncated"] = True
    fetcher.message("one")
    with pytest.raises(client.OutlookError, match="Complete message"):
        fetcher.message("one", refresh_mode="none")
    remote.calls.clear()
    fetcher.message("one")
    assert remote.calls == [("message", "one")]


def test_missing_revision_never_uses_received_time(cached: Any) -> None:
    fetcher, _store, remote = cached
    remote.items["one"]["ItemId"].pop("ChangeKey")
    remote.items["one"].pop("LastModifiedTime")
    fetcher.messages(body=True)
    remote.calls.clear()
    fetcher.messages(body=True)
    assert [c[0] for c in remote.calls] == ["messages", "metadata", "message"]
    assert not same_revision({"DateTimeReceived": "same"}, {"DateTimeReceived": "same"})


def test_offline_listing_filters_local_records_with_different_limit_and_offset(
    cached: Any,
) -> None:
    fetcher, _store, remote = cached
    remote.items["two"] = message("two", DateTimeReceived="2026-11-02T00:00:00Z", IsRead=True)
    remote.items["three"] = message("three", DateTimeReceived="2026-11-03T00:00:00Z")
    fetcher.messages(query="term")
    remote.summaries = [message("four")]
    fetcher.messages(folder="sentitems")
    remote.calls.clear()
    offline = fetcher.messages(offset=1, limit=1, refresh_mode="none")
    assert [m["ItemId"]["Id"] for m in offline] == ["two"]
    offline = fetcher.messages(unread=True, limit=2, refresh_mode="none")
    assert [m["ItemId"]["Id"] for m in offline] == ["three", "one"]
    offline = fetcher.messages(folder="sentitems", refresh_mode="none")
    assert [m["ItemId"]["Id"] for m in offline] == ["four"]
    assert remote.calls == []


def test_search_queries_are_not_cached_and_offline_search_is_rejected(cached: Any) -> None:
    fetcher, store, remote = cached
    fetcher.messages(query="natural language query one")
    fetcher.messages(query="different query two")
    assert [c[0] for c in remote.calls] == ["messages", "messages"]
    assert not (store.root / "queries").exists()
    for path in store.root.rglob("*.json"):
        assert "natural language query one" not in path.read_text()
        assert "different query two" not in path.read_text()
    remote.calls.clear()
    with pytest.raises(ValueError, match=r"Offline search.*jq or grep"):
        fetcher.messages(query="natural language query one", refresh_mode="none")
    assert remote.calls == []


def test_partial_body_download_keeps_metadata_and_successfully_fetched_bodies(
    cached: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher, store, remote = cached
    remote.items["two"] = message("two")
    original = remote.message

    def fail_second(identifier: str) -> Any:
        if identifier == "two":
            raise client.OutlookError("failed second message")
        return original(identifier)

    monkeypatch.setattr(remote, "message", fail_second)
    with pytest.raises(client.OutlookError, match="second"):
        fetcher.messages(body=True)
    assert store.message("one")["complete"] is True
    assert store.message("two")["complete"] is False
    assert not (store.root / "queries").exists()


def test_attachments_are_opt_in_then_reused_and_repaired_when_missing(cached: Any) -> None:
    fetcher, _store, remote = cached
    remote.items["one"]["Attachments"] = [attachment()]
    metadata = fetcher.message("one")
    assert [c[0] for c in remote.calls] == ["message"]
    assert metadata["Attachments"][0]["LastModifiedTime"]
    assert metadata["_outlook"]["attachments"][0]["status"] == "metadata"
    with pytest.raises(client.OutlookError, match=r"Attachment.*not cached"):
        fetcher.message("one", refresh_mode="none", attachments=True)
    fetched = fetcher.message("one", attachments=True)
    path = Path(fetched["_outlook"]["attachments"][0]["path"])
    assert path.read_bytes() == b"hello"
    remote.calls.clear()
    fetcher.message("one", attachments=True)
    assert remote.calls == [("metadata", "one")]
    remote.calls.clear()
    fetcher.message("one", refresh_mode="none", attachments=True)
    assert remote.calls == []
    path.unlink()
    fetcher.message("one", attachments=True)
    assert remote.calls == [("metadata", "one"), ("attachment", "attachment-one")]


def test_attachment_timestamp_avoids_download_on_unrelated_message_change(cached: Any) -> None:
    fetcher, _store, remote = cached
    remote.items["one"]["Attachments"] = [attachment()]
    fetcher.message("one", attachments=True)
    remote.calls.clear()
    remote.items["one"]["ItemId"]["ChangeKey"] = "v2"
    fetcher.message("one", attachments=True)
    assert [c[0] for c in remote.calls] == ["metadata", "message"]
    remote.items["one"]["ItemId"]["ChangeKey"] = "v3"
    remote.items["one"]["Attachments"][0]["LastModifiedTime"] = "2026-11-02T00:00:00"
    result = fetcher.message("one")
    assert result["_outlook"]["attachments"][0]["status"] == "metadata"
    with pytest.raises(client.OutlookError, match="not cached"):
        fetcher.message("one", refresh_mode="none", attachments=True)
    remote.calls.clear()
    fetcher.message("one", attachments=True)
    assert [c[0] for c in remote.calls] == ["metadata", "attachment"]
    remote.calls.clear()
    fetcher.message("one", refresh_mode="force", attachments=True)
    assert [c[0] for c in remote.calls] == ["message", "attachment"]


def test_truncated_attachment_file_is_refetched(cached: Any) -> None:
    fetcher, store, remote = cached
    remote.items["one"]["Attachments"] = [attachment()]
    result = fetcher.message("one", attachments=True)
    path = Path(result["_outlook"]["attachments"][0]["path"])
    path.write_bytes(b"h")
    remote.calls.clear()
    fetcher.message("one", attachments=True)
    assert remote.calls == [("metadata", "one"), ("attachment", "attachment-one")]
    assert path.read_bytes() == b"hello"
    assert store.message("one")["attachment_files"]["attachment-one"]["metadata"]["Extra"] == "keep"


def test_attachment_without_timestamp_follows_parent_revision(cached: Any) -> None:
    fetcher, _store, remote = cached
    a = attachment()
    a.pop("LastModifiedTime")
    remote.items["one"]["Attachments"] = [a]
    fetcher.message("one", attachments=True)
    remote.items["one"]["ItemId"]["ChangeKey"] = "v2"
    remote.calls.clear()
    fetcher.message("one", attachments=True)
    assert [c[0] for c in remote.calls] == ["metadata", "message", "attachment"]


def test_reference_and_item_attachments(cached: Any) -> None:
    fetcher, _store, remote = cached
    remote.items["one"]["Attachments"] = [
        attachment("ref", __type="ReferenceAttachment:#Exchange"),
        attachment("item", __type="ItemAttachment:#Exchange", Name="Forwarded mail"),
    ]
    remote.payloads["item"] = {"Item": {"Subject": "Attached message", "UnknownField": "keep"}}
    result = fetcher.message("one", attachments=True)
    entries = result["_outlook"]["attachments"]
    assert entries[0]["status"] == "reference"
    assert entries[1]["path"].endswith(".json")
    assert json.loads(Path(entries[1]["path"]).read_text())["UnknownField"] == "keep"
    assert [c[1] for c in remote.calls if c[0] == "attachment"] == ["item"]


def test_attached_message_mime_is_cached_as_eml(cached: Any) -> None:
    fetcher, _store, remote = cached
    mime = b"Subject: Attached message\r\n\r\nBody\r\n"
    remote.items["one"]["Attachments"] = [attachment("item", __type="ItemAttachment:#Exchange")]
    remote.payloads["item"] = {"Item": {"MimeContent": {"Value": base64.b64encode(mime).decode()}}}
    result = fetcher.message("one", attachments=True)
    path = Path(result["_outlook"]["attachments"][0]["path"])
    assert path.suffix == ".eml"
    assert path.read_bytes() == mime


def test_bad_attachment_content_does_not_destroy_cached_file(cached: Any) -> None:
    fetcher, store, remote = cached
    remote.items["one"]["Attachments"] = [attachment()]
    result = fetcher.message("one", attachments=True)
    path = Path(result["_outlook"]["attachments"][0]["path"])
    remote.payloads["attachment-one"] = {"Content": "!invalid base64"}
    with pytest.raises(client.OutlookError, match="base64"):
        fetcher.message("one", refresh_mode="force", attachments=True)
    assert path.read_bytes() == b"hello"
    assert store.message("one")["attachment_files"]


def test_safe_bounded_names_duplicate_subjects_and_attachment_paths(cached: Any) -> None:
    fetcher, store, remote = cached
    subject = "../CON/é/测试\\" + "long subject " * 100
    remote.items = {
        i: message(i, Subject=subject, Attachments=[attachment(Name="../../outside.pdf")])
        for i in ["one", "two"]
    }
    result = fetcher.messages(attachments=True)
    paths = [Path(item["_outlook"]["path"]) for item in result]
    assert paths[0] != paths[1]
    assert all(len(p.parent.name.encode()) <= 58 for p in paths)
    assert all(len(p.parent.name.rsplit("--", 1)[1]) == 16 for p in paths)
    for item in result:
        path = Path(item["_outlook"]["attachments"][0]["path"])
        assert path.is_relative_to(store.root / "emails")
        assert len(path.name.encode()) <= 71
        windows_path = PureWindowsPath(
            r"C:\Users\student\AppData\Local\sk4n\outlook"
        ) / path.relative_to(store.root)
        assert len(str(windows_path)) < 260
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert safe_name("A:B", "one") != safe_name("A/B", "two")
    old_path = paths[0]
    remote.items["one"] = message(change="v2", Subject="Renamed")
    assert fetcher.message("one")["_outlook"]["path"] == str(old_path)


def test_short_message_hash_extends_only_on_collision(
    cached: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher, store, remote = cached
    actual_digest = storage.digest

    def colliding(identifier: str) -> str:
        return "ab" * 8 + ("01" if identifier == "one" else "02") + actual_digest(identifier)[18:]

    monkeypatch.setattr(storage, "digest", colliding)
    remote.items["one"]["Subject"] = "x" * 100
    remote.items["two"] = message("two", Subject="x" * 100)
    first, second = fetcher.messages(body=True)
    one, two = Path(first["_outlook"]["path"]), Path(second["_outlook"]["path"])
    assert len(one.parent.name.rsplit("--", 1)[1]) == 16
    assert len(two.parent.name.rsplit("--", 1)[1]) == 18
    assert one != two
    assert store.message("one")["message"]["ItemId"]["Id"] == "one"
    assert store.message("two")["message"]["ItemId"]["Id"] == "two"
    assert Path(fetcher.message("two")["_outlook"]["path"]) == two


def test_short_attachment_hash_collision_does_not_overwrite_files(
    cached: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher, _store, remote = cached
    actual_digest = storage.digest

    def colliding(identifier: str) -> str:
        if identifier.startswith("attachment-"):
            return (
                "ab" * 8
                + ("01" if identifier.endswith("one") else "02")
                + actual_digest(identifier)[18:]
            )
        return actual_digest(identifier)

    monkeypatch.setattr(storage, "digest", colliding)
    remote.items["one"]["Attachments"] = [
        attachment(identifier, Name="x" * 100 + ".pdf")
        for identifier in ["attachment-one", "attachment-two"]
    ]
    remote.payloads["attachment-two"] = {"Content": base64.b64encode(b"other").decode()}
    result = fetcher.message("one", attachments=True)
    one, two = [Path(a["path"]) for a in result["_outlook"]["attachments"]]
    assert len(one.stem.rsplit("--", 1)[1]) == 16
    assert len(two.stem.rsplit("--", 1)[1]) == 18
    assert one.read_bytes() == b"hello"
    assert two.read_bytes() == b"other"
    remote.calls.clear()
    assert [
        a["path"] for a in fetcher.message("one", attachments=True)["_outlook"]["attachments"]
    ] == [str(one), str(two)]
    assert remote.calls == [("metadata", "one")]


def test_unindexed_path_collision_preserves_existing_content(cached: Any) -> None:
    fetcher, store, _remote = cached
    directory = store.root / "emails" / safe_name("Example subject", "one")
    directory.mkdir(parents=True)
    (directory / "unrelated.txt").write_text("keep")
    result = fetcher.message("one")
    assert len(Path(result["_outlook"]["path"]).parent.name.rsplit("--", 1)[1]) == 18
    assert (directory / "unrelated.txt").read_text() == "keep"


def test_folder_id_bypasses_name_mapping_and_folder_name_never_guesses_id(cached: Any) -> None:
    fetcher, _store, remote = cached
    fetcher.messages(folder_id="opaque-id", refresh_mode="force")
    assert [c[0] for c in remote.calls] == ["messages"]
    assert remote.calls[0][1]["folder"] == "opaque-id"
    assert remote.calls[0][1]["folder_is_id"] is True
    remote.calls.clear()
    with pytest.raises(client.OutlookError, match="mapping"):
        fetcher.messages(folder="opaque-id")
    assert [c[0] for c in remote.calls] == ["folders"]
    remote.calls.clear()
    with pytest.raises(ValueError, match="mutually exclusive"):
        fetcher.messages(folder="inbox", folder_id="id")
    with pytest.raises(ValueError, match="empty"):
        fetcher.messages(folder_id="")
    assert remote.calls == []


@pytest.mark.parametrize("command", [["messages"], ["search", "term"]])
def test_cli_folder_selectors_are_distinct_and_mutually_exclusive(command: list[str]) -> None:
    parser = cli.build_parser()
    assert parser.parse_args(command).folder is None
    assert parser.parse_args([*command, "--folder-id", "opaque-id"]).folder_id == "opaque-id"
    assert parser.parse_args([*command, "--folder", "id:literal-name"]).folder == "id:literal-name"
    with pytest.raises(SystemExit):
        parser.parse_args([*command, "--folder", "inbox", "--folder-id", "opaque-id"])


def test_message_and_attachment_urls_are_cached_and_available_without_download(cached: Any) -> None:
    fetcher, store, remote = cached
    remote.items["one"]["Attachments"] = [attachment()]
    listed = fetcher.messages()[0]
    assert listed["url"] == message_url("one")
    assert store.message("one")["metadata"]["url"] == listed["url"]
    result = fetcher.message("one")
    expected = attachment_url("one", "attachment-one")
    assert result["Attachments"][0]["url"] == expected
    assert result["_outlook"]["attachments"][0]["url"] == expected
    assert result["_outlook"]["attachments"][0]["path"] is None
    assert store.message("one")["message"]["Attachments"][0]["url"] == expected
    downloaded = fetcher.message("one", attachments=True)
    assert Path(downloaded["_outlook"]["attachments"][0]["path"]).is_file()
    entry = store.message("one")["attachment_files"]["attachment-one"]
    assert entry["url"] == entry["metadata"]["url"] == expected
    offline = fetcher.message("one", refresh_mode="none", attachments=True)
    assert offline["url"] == result["url"]
    assert offline["_outlook"]["attachments"][0]["url"] == expected


def test_reference_links_encode_ids_as_path_segments() -> None:
    mid = "message/with+chars==?#"
    aid = "attachment/id+==?#"
    assert message_url(mid) == client.DEFAULT_BASE_URL + "deeplink/read/" + quote(mid, safe="")
    url = attachment_url(mid, aid)
    assert (
        url
        == client.DEFAULT_BASE_URL
        + "deeplink/attachment/"
        + quote(mid, safe="")
        + "/"
        + quote(aid, safe="")
        + "/false/"
    )
    assert not urlsplit(url).query and not urlsplit(url).fragment


def test_existing_cached_records_gain_links_without_redownloading(cached: Any) -> None:
    fetcher, store, remote = cached
    raw = message(Attachments=[attachment()])
    remote.items["one"] = raw
    record = {
        "message": raw,
        "metadata": {k: v for k, v in raw.items() if k != "Body"},
        "complete": True,
        "attachment_files": {},
    }
    store.save_message(record)
    file_path = store.attachment_path("one", "attachment-one", "report.pdf")
    store.save_attachment(file_path, b"hello")
    message_path = store.message_path("one")
    record["attachment_files"]["attachment-one"] = {
        "path": str(file_path.relative_to(message_path.parent)),
        "size": 5,
        "revision": {k: raw["Attachments"][0][k] for k in ["LastModifiedTime", "Size", "__type"]},
        "metadata": {"AttachmentOriginalUrl": ""},
    }
    store.save_message(record)
    before = message_path.read_bytes()
    offline = fetcher.message("one", refresh_mode="none", attachments=True)
    assert offline["url"] and offline["_outlook"]["attachments"][0]["url"]
    assert message_path.read_bytes() == before
    result = fetcher.message("one", attachments=True)
    assert remote.calls == [("metadata", "one")]
    saved = store.message("one")
    assert saved["message"]["url"] == result["url"]
    assert saved["metadata"]["url"] == result["url"]
    assert (
        saved["attachment_files"]["attachment-one"]["metadata"]["url"]
        == result["_outlook"]["attachments"][0]["url"]
    )
    assert file_path.read_bytes() == b"hello"


def test_corrupt_message_cache_refetches_and_offline_fails(cached: Any) -> None:
    fetcher, store, remote = cached
    fetcher.message("one")
    path = store.message_path("one")
    path.write_text("{broken")
    with pytest.raises(client.OutlookError, match="not cached"):
        fetcher.message("one", refresh_mode="none")
    remote.calls.clear()
    fetcher.message("one")
    assert remote.calls == [("message", "one")]


def test_missing_body_does_not_trust_stored_complete_flag(cached: Any) -> None:
    fetcher, store, remote = cached
    fetcher.message("one")
    record = store.message("one")
    record["message"].pop("Body")
    store.save_message(record)
    with pytest.raises(client.OutlookError, match="Complete message"):
        fetcher.message("one", refresh_mode="none")
    remote.calls.clear()
    assert fetcher.message("one")["Body"]
    assert remote.calls == [("message", "one")]


def test_cli_cache_only_needs_no_login_and_never_contacts_outlook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    store = OutlookStore(tmp_path)
    store.save_message({"message": message(), "complete": True, "attachment_files": {}})
    monkeypatch.setattr(
        client.OutlookClient, "request", lambda *_a: pytest.fail("unexpected request")
    )
    monkeypatch.setattr(
        client.OutlookClient, "_session", lambda *_a: pytest.fail("unexpected auth")
    )
    assert (
        cli.main(
            ["--data-path", str(tmp_path), "message", "one", "--no-refresh", "--format", "json"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["UnknownField"] == {"keep": True}
    assert not (tmp_path / ".cache.lock").exists()


@pytest.mark.parametrize(
    "command", [["folders"], ["messages"], ["search", "term"], ["message", "id"]]
)
def test_cli_refresh_modes_are_mutually_exclusive(command: list[str]) -> None:
    parser = cli.build_parser()
    assert parser.parse_args(command).refresh_mode == "default"
    with pytest.raises(SystemExit):
        parser.parse_args([*command, "--refresh", "--no-refresh"])


def test_revision_and_completeness_contract() -> None:
    assert same_revision(message(), {"ItemId": {"ChangeKey": "v1"}})
    assert not same_revision(message(), {"ItemId": {"ChangeKey": "v2"}})
    assert same_revision(message(), {"DateTimeLastModified": "2026-11-01T00:00:00Z"})
    assert complete_message(message(Body={"Value": ""}))
    assert not complete_message(message(Body={"Value": "partial", "IsTruncated": True}))
