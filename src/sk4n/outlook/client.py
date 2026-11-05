from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import requests

from sk4n.errors import ExitCode
from sk4n.tools.request import RequestTools
from sk4n.tools.shared import load_session

from .browser import (
    DEFAULT_BASE_URL,
    DEFAULT_SITE_NAME,
    OUTLOOK_HOSTS,
    capture_session,
    validate_base_url,
)
from .storage import canonical_immutable_id

READ_ACTIONS = frozenset(
    {
        "GetOwaUserConfiguration",
        "FindFolder",
        "GetFolder",
        "FindItem",
        "GetItem",
        "GetAttachment",
        "FindConversation",
        "GetConversationItems",
        "GetCalendarFoldersV2",
        "GetCalendarView",
    }
)
WRITE_ACTIONS = frozenset({"UpdateItem", "MoveItem", "CreateItem", "DeleteItem"})
DISTINGUISHED_FOLDERS = frozenset(
    {
        "inbox",
        "drafts",
        "sentitems",
        "deleteditems",
        "junkemail",
        "archive",
        "msgfolderroot",
        "root",
        "calendar",
        "contacts",
        "tasks",
        "notes",
        "outbox",
    }
)


class OutlookError(RuntimeError):
    exit_code = ExitCode.REMOTE

    def __init__(self, message: str, *, response_code: str | None = None):
        super().__init__(message)
        self.response_code = response_code


class OutlookAuthError(OutlookError):
    exit_code = ExitCode.AUTH


class OutlookTransportError(OutlookError):
    exit_code = ExitCode.TRANSPORT


def envelope(action: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "__type": f"{action}JsonRequest:#Exchange",
        "Header": {"__type": "JsonRequestHeaders:#Exchange", "RequestServerVersion": "V2018_01_08"},
        "Body": {"__type": f"{action}Request:#Exchange", **body},
    }


def folder_id(value: str, *, distinguished: bool = True) -> dict[str, str]:
    if not value:
        raise ValueError("Folder must not be empty.")
    known = distinguished and value.lower() in DISTINGUISHED_FOLDERS
    return {
        "__type": "DistinguishedFolderId:#Exchange" if known else "FolderId:#Exchange",
        "Id": value.lower() if known else value,
    }


def properties(*names: str) -> list[dict[str, str]]:
    return [{"__type": "PropertyUri:#Exchange", "FieldURI": name} for name in names]


def response_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    body = payload.get("Body", {})
    if body.get("ErrorCode") or body.get("FaultMessage"):
        raise OutlookError(
            str(body.get("FaultMessage") or body["ErrorCode"]),
            response_code=str(body.get("ErrorCode") or ""),
        )
    messages = body.get("ResponseMessages", {}).get("Items")
    if not isinstance(messages, list) or not messages:
        raise OutlookError("Outlook returned no response messages.")
    for message in messages:
        if (
            message.get("ResponseCode") not in (None, "NoError")
            or message.get("ResponseClass") == "Error"
        ):
            raise OutlookError(
                str(
                    message.get("MessageText")
                    or message.get("ResponseCode")
                    or "Outlook request failed."
                ),
                response_code=message.get("ResponseCode"),
            )
    return messages


def search_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload.get("EntitySets"), list) or not payload["EntitySets"]:
        raise OutlookError("Outlook returned no search result sets.")
    items: list[dict[str, Any]] = []
    for entity in payload["EntitySets"]:
        for result_set in entity.get("ResultSets", []):
            for result in result_set.get("Results", []):
                source = result.get("Source")
                if not isinstance(source, dict):
                    raise OutlookError("Outlook returned an unexpected search result.")
                immutable = canonical_immutable_id(source.get("ImmutableId"))
                if immutable:
                    source = {**source, "ItemId": {**source.get("ItemId", {}), "Id": immutable}}
                    if "Flag" not in source:
                        source = {**source, "Flag": {"FlagStatus": "NotFlagged"}}
                items.append(source)
    return items


class OutlookClient:
    allowed_hosts = OUTLOOK_HOSTS

    def __init__(
        self,
        *,
        site_name: str = DEFAULT_SITE_NAME,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 30,
        renew: bool = True,
    ):
        validate_base_url(base_url)
        if timeout <= 0:
            raise ValueError("--timeout must be positive")
        self.site_name, self.base_url, self.timeout, self.renew = (
            site_name,
            base_url,
            timeout,
            renew,
        )

    def _session(self) -> dict[str, Any]:
        saved = load_session(self.site_name)
        if not isinstance(saved, dict) or not isinstance(saved.get("storage_state"), dict):
            raise OutlookAuthError("No saved Outlook login. Run `nus-outlook auth login` first.")
        return saved

    def request(
        self, method: str, path: str, data: Any = None, *, _write: bool = False
    ) -> dict[str, Any]:
        method = method.upper()
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        action = query.get("action", [])
        search = (
            parsed.path in {"/searchservice/api/v2/query", "/search/api/v2/query"}
            and method == "POST"
            and not action
        )
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or (_write and method != "POST")
            or not (
                search
                or (
                    parsed.path == "/owa/service.svc"
                    and len(action) == 1
                    and action[0] in (WRITE_ACTIONS if _write else READ_ACTIONS)
                )
            )
            or method not in {"GET", "POST"}
        ):
            raise ValueError(
                "Use /owa/service.svc?action=<supported read action> with GET or POST, or POST /searchservice/api/v2/query or /search/api/v2/query. API access is read-only; authorized writes must use the browser UI."
            )
        if data is not None and not isinstance(data, dict):
            raise ValueError("Outlook request data must be a JSON object.")
        label = "Search" if search else action[0]
        payload = data if data is not None else ({} if search else envelope(label, {}))
        if not search and (
            payload.get("__type", f"{action[0]}JsonRequest:#Exchange")
            != f"{action[0]}JsonRequest:#Exchange"
        ):
            raise ValueError("Request type must match the read action.")
        body = payload.get("Body", {})
        if not search and (
            not isinstance(body, dict)
            or body.get("__type", f"{action[0]}Request:#Exchange")
            != f"{action[0]}Request:#Exchange"
        ):
            raise ValueError("Body type must match the read action.")
        saved = self._session()
        for attempt in range(2):
            token = saved.get("access_token")
            if not isinstance(token, str) or not token:
                response = None
            else:
                base = validate_base_url(str(saved.get("base_url") or self.base_url))
                origin = f"https://{urlsplit(base).hostname}"
                rt = RequestTools(site_name=self.site_name, timeout=self.timeout)
                try:
                    response = rt.session.request(
                        method,
                        origin + path,
                        headers={
                            "Authorization": "Bearer " + token,
                            **({} if search else {"Action": label}),
                        },
                        json=payload,
                        timeout=self.timeout,
                        allow_redirects=False,
                    )
                except requests.RequestException as exc:
                    raise OutlookTransportError(
                        "Could not contact Outlook. Check the network connection and try again."
                    ) from exc
            if response is None or response.status_code in {401, 403, 440}:
                if attempt == 0 and self.renew:
                    try:
                        saved = capture_session(
                            site_name=self.site_name,
                            base_url=self.base_url,
                            timeout=self.timeout,
                            login_wait_seconds=self.timeout,
                        )
                    except TimeoutError as exc:
                        raise OutlookAuthError(str(exc)) from exc
                    continue
                raise OutlookAuthError(
                    "Saved Outlook login has expired. Run `nus-outlook auth login`."
                )
            if not response.ok or response.is_redirect:
                raise OutlookError(
                    f"Outlook returned HTTP {response.status_code} for {label}.",
                    response_code=f"HTTP{response.status_code}",
                )
            try:
                result = response.json()
            except ValueError as exc:
                raise OutlookError("Outlook returned a non-JSON response.") from exc
            if not isinstance(result, dict):
                raise OutlookError("Outlook returned an unexpected response.")
            return result
        raise AssertionError("unreachable")

    def action(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/owa/service.svc?action=" + name, envelope(name, body))

    def write_action(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        if name not in WRITE_ACTIONS:
            raise ValueError("Unsupported Outlook write action.")
        return self.request(
            "POST", "/owa/service.svc?action=" + name, envelope(name, body), _write=True
        )

    def profile(self) -> dict[str, Any]:
        result = self.action("GetOwaUserConfiguration", {})
        profile = result.get("SessionSettings", {})
        if not isinstance(profile, dict) or not profile.get("UserEmailAddress"):
            raise OutlookAuthError(
                "Outlook did not return a signed-in mailbox profile. Run `nus-outlook auth login`."
            )
        return profile

    def folders(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        offset = 0
        while True:
            payload = self.action(
                "FindFolder",
                {
                    "FolderShape": {
                        "__type": "FolderResponseShape:#Exchange",
                        "BaseShape": "IdOnly",
                        "AdditionalProperties": properties(
                            "FolderDisplayName",
                            "ParentFolderId",
                            "TotalCount",
                            "UnreadCount",
                            "DistinguishedFolderId",
                        ),
                    },
                    "ParentFolderIds": [folder_id("msgfolderroot")],
                    "Traversal": "Deep",
                    "Paging": {
                        "__type": "IndexedPageView:#Exchange",
                        "BasePoint": "Beginning",
                        "Offset": offset,
                        "MaxEntriesReturned": 1000,
                    },
                },
            )
            root = response_messages(payload)[0].get("RootFolder", {})
            folders = root.get("Folders", [])
            result.extend(folders)
            if root.get("IncludesLastItemInRange", True):
                return result
            next_offset = root.get("IndexedPagingOffset", offset + len(folders))
            if not folders or not isinstance(next_offset, int) or next_offset <= offset:
                raise OutlookError("Outlook folder pagination made no progress.")
            offset = next_offset

    def messages(
        self,
        *,
        folder: str = "inbox",
        folder_is_id: bool = False,
        limit: int = 20,
        offset: int = 0,
        unread: bool = False,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset must be zero or greater")
        if query is not None and not query.strip():
            raise ValueError("Search query must not be empty.")
        if unread or query is not None:
            search_query = (
                f"({query}) AND isread:no" if unread and query else (query or "isread:no")
            )
            return self.search(
                search_query, folder=folder, folder_is_id=folder_is_id, limit=limit, offset=offset
            )
        body: dict[str, Any] = {
            "ItemShape": {
                "__type": "ItemResponseShape:#Exchange",
                "BaseShape": "Default",
                "AdditionalProperties": properties(
                    "DateTimeReceived", "ConversationId", "InternetMessageId", "Flag"
                ),
            },
            "ParentFolderIds": [folder_id(folder, distinguished=not folder_is_id)],
            "Traversal": "Shallow",
            "Paging": {
                "__type": "IndexedPageView:#Exchange",
                "BasePoint": "Beginning",
                "Offset": offset,
                "MaxEntriesReturned": limit,
            },
            "SortOrder": [
                {
                    "__type": "SortResults:#Exchange",
                    "Order": "Descending",
                    "Path": {"__type": "PropertyUri:#Exchange", "FieldURI": "DateTimeReceived"},
                }
            ],
        }
        payload = self.action("FindItem", body)
        return response_messages(payload)[0].get("RootFolder", {}).get("Items", [])

    def folder_metadata(self, identifier: str) -> dict[str, Any]:
        payload = self.action(
            "GetFolder",
            {
                "FolderIds": [folder_id(identifier)],
                "FolderShape": {"__type": "FolderResponseShape:#Exchange", "BaseShape": "IdOnly"},
            },
        )
        folders = response_messages(payload)[0].get("Folders", [])
        if not folders:
            raise OutlookError("Outlook returned no folder for that ID.")
        return folders[0]

    def search(
        self,
        query: str,
        *,
        folder: str = "inbox",
        folder_is_id: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if not query.strip() or limit < 1 or offset < 0:
            raise ValueError(
                "Search needs a nonempty query, positive limit, and nonnegative offset."
            )
        selected = folder_id(folder, distinguished=not folder_is_id)
        scope = (
            "DistinguishedFolderName"
            if selected["__type"] == "DistinguishedFolderId:#Exchange"
            else "FolderId"
        )
        payload = self.request(
            "POST",
            "/searchservice/api/v2/query",
            {
                "Cvid": str(uuid4()),
                "Scenario": {"Name": "owa.react"},
                "TextDecorations": "Off",
                "EntityRequests": [
                    {
                        "EntityType": "Message",
                        "ContentSources": ["Exchange"],
                        "Filter": {"Term": {scope: selected["Id"]}},
                        "From": offset,
                        "Query": {"QueryString": query},
                        "Size": limit,
                        "Sort": [{"Field": "Time", "SortDirection": "Desc"}],
                        "EnableTopResults": False,
                    }
                ],
            },
        )
        return search_results(payload)[:limit]

    def people(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        query = query.strip()
        if not query or limit < 1:
            raise ValueError("People lookup needs a nonempty name or email and a positive limit.")
        payload = self.request(
            "POST",
            "/search/api/v2/query",
            {
                "Cvid": str(uuid4()),
                "Scenario": {"Name": "owa.react.people"},
                "TextDecorations": "Off",
                "EntityRequests": [
                    {
                        "EntityType": "People",
                        "ContentSources": ["Exchange"],
                        "Provenances": ["Directory"],
                        "Query": {"QueryString": query},
                        "Size": limit,
                        "Filter": {
                            "And": [
                                {"Term": {"PeopleType": "Person"}},
                                {
                                    "Or": [
                                        {"Term": {"PeopleSubtype": "OrganizationUser"}},
                                        {"Term": {"PeopleSubtype": "OrganizationContact"}},
                                    ]
                                },
                            ]
                        },
                        "Fields": [
                            "Id",
                            "DisplayName",
                            "EmailAddresses",
                            "PeopleType",
                            "PeopleSubtype",
                            "JobTitle",
                            "Department",
                            "CompanyName",
                            "OfficeLocation",
                            "UserPrincipalName",
                        ],
                    }
                ],
            },
        )
        return search_results(payload)[:limit]

    def message(self, message_id: str) -> dict[str, Any]:
        if not message_id:
            raise ValueError("Message ID must not be empty.")
        payload = self.action(
            "GetItem",
            {
                "ItemIds": [{"__type": "ItemId:#Exchange", "Id": message_id}],
                "ItemShape": {
                    "__type": "ItemResponseShape:#Exchange",
                    "BaseShape": "AllProperties",
                    "BodyType": "Text",
                    "BlockExternalImages": True,
                },
            },
        )
        items = response_messages(payload)[0].get("Items", [])
        if not items:
            raise OutlookError("Outlook returned no message for that ID.")
        return items[0]

    def message_metadata(self, message_id: str) -> dict[str, Any]:
        """Read the current ChangeKey without downloading the message body."""
        if not message_id:
            raise ValueError("Message ID must not be empty.")
        payload = self.action(
            "GetItem",
            {
                "ItemIds": [{"__type": "ItemId:#Exchange", "Id": message_id}],
                "ItemShape": {"__type": "ItemResponseShape:#Exchange", "BaseShape": "IdOnly"},
            },
        )
        items = response_messages(payload)[0].get("Items", [])
        if not items:
            raise OutlookError("Outlook returned no message for that ID.")
        return items[0]

    def attachment(self, attachment_id: str) -> dict[str, Any]:
        if not attachment_id:
            raise ValueError("Attachment ID must not be empty.")
        payload = self.action(
            "GetAttachment",
            {"AttachmentIds": [{"__type": "AttachmentId:#Exchange", "Id": attachment_id}]},
        )
        attachments = response_messages(payload)[0].get("Attachments", [])
        if not attachments:
            raise OutlookError("Outlook returned no attachment for that ID.")
        return attachments[0]
