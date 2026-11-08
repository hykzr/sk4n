from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import requests
from playwright.async_api import Error as PlaywrightError

from sk4n.errors import exit_code_for_error
from sk4n.paths import ensure_private_directory, outlook_data_dir
from sk4n.tools.playwright_cli import (
    PlaywrightCLIError,
    ensure_session_available,
    open_authenticated_session,
    playwright_cli_executable,
)
from sk4n.tools.shared import exclusive_file_lock

from .actions import OutlookActions
from .auth import DEFAULT_LOGIN_WAIT_SECONDS, check_auth_status, login, logout
from .browser import validate_base_url
from .client import DEFAULT_BASE_URL, DEFAULT_SITE_NAME, OutlookClient, OutlookError
from .fetcher import OutlookFetcher
from .preview import OutlookPreview
from .storage import OutlookStore


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def json_argument(value: str) -> Any:
    try:
        return json.loads(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be valid JSON") from exc


def add_format_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        choices=("json", "jsonl", "plain"),
        default=None,
        help="Output format. Default: human-friendly details.",
    )


def add_refresh_arguments(parser: argparse.ArgumentParser, *, attachments: bool = False) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--refresh",
        action="store_const",
        const="force",
        dest="refresh_mode",
        help="Force refresh the requested data and local artifacts.",
    )
    group.add_argument(
        "--no-refresh",
        action="store_const",
        const="none",
        dest="refresh_mode",
        help="Read only from the local cache; fail if the requested data is missing.",
    )
    parser.set_defaults(refresh_mode="default")
    if attachments:
        parser.add_argument(
            "--attachments",
            action="store_true",
            help="Download and cache file/item attachments, fetching message details as needed.",
        )


def add_folder_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--folder",
        default=None,
        help="Folder display name/path or distinguished name. Default: inbox when neither folder option is given.",
    )
    group.add_argument(
        "--folder-id",
        default=None,
        help="Exact Outlook folder ID; bypass folder-name lookup.",
    )


def add_body_argument(parser: argparse.ArgumentParser, *, required: bool) -> None:
    body = parser.add_mutually_exclusive_group(required=required)
    body.add_argument(
        "--body",
        help="Plain-text body; when editing, replaces the entire body including quoted text.",
    )
    body.add_argument("--body-file", type=Path, help="Read the plain-text body from a UTF-8 file.")


def add_attachment_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--attach",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="Attach a local file; repeat for multiple files. Existing attachments are retained.",
    )


def add_recipient_arguments(parser: argparse.ArgumentParser, *, editing: bool = False) -> None:
    for name in ("to", "cc", "bcc"):
        group = parser.add_mutually_exclusive_group(required=name == "to" and not editing)
        group.add_argument(
            f"--{name}",
            action="append",
            help=f"{name.title()} email address; repeat for multiple recipients. Editing replaces this list.",
        )
        if editing:
            group.add_argument(
                f"--clear-{name}",
                dest=name,
                action="store_const",
                const=[],
                help=f"Remove all {name.title()} recipients.",
            )


def build_parser() -> argparse.ArgumentParser:
    default_data_path = outlook_data_dir()
    parser = argparse.ArgumentParser(
        prog="nus-outlook",
        description="Read, cache, and manage NUS Outlook mail using a saved NUS SSO browser login.",
        epilog=(
            "Exit codes: 0 success, 1 unauthenticated status, 2 validation, "
            "3 authentication, 4 transport, 5 remote HTTP/response failure."
        ),
    )
    parser.add_argument(
        "--data-path",
        type=Path,
        default=default_data_path,
        help=f"Local Outlook cache directory. Default: {default_data_path}.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("OUTLOOK_BASE_URL", DEFAULT_BASE_URL),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--site-name",
        default=os.getenv("OUTLOOK_SITE_NAME", DEFAULT_SITE_NAME),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--timeout",
        type=positive_int,
        default=int(os.getenv("OUTLOOK_TIMEOUT", "30")),
        help="Request timeout in seconds. Default: 30.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", help="Manage the saved NUS login.")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    status = auth_commands.add_parser("status", help="Check the saved login.")
    status.add_argument(
        "--format",
        choices=("json",),
        default=None,
        help="Output machine-readable authentication status.",
    )
    auth_login = auth_commands.add_parser("login", help="Open a browser for NUS login and MFA.")
    auth_login.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore saved authentication and perform a fresh login.",
    )
    auth_login.add_argument(
        "--login-hint",
        default=None,
        metavar="EMAIL",
        help="Microsoft sign-in address: ...@u.nus.edu for students or ...@nus.edu.sg for staff. Subsequent logins remember it.",
    )
    auth_login.add_argument(
        "--wait-seconds",
        type=positive_int,
        default=DEFAULT_LOGIN_WAIT_SECONDS,
        help=f"Login detection timeout. Default: {DEFAULT_LOGIN_WAIT_SECONDS}.",
    )
    auth_commands.add_parser(
        "logout", help="Delete this CLI's saved login without signing other browsers out."
    )

    folders = commands.add_parser("folders", help="List mailbox folders.")
    add_refresh_arguments(folders)
    add_format_argument(folders)
    people = commands.add_parser(
        "people", help="Find people's email addresses in the NUS directory."
    )
    people.add_argument(
        "query", help="Name or email to look up, for example an instructor, tutor, or TA's name."
    )
    people.add_argument(
        "--limit", type=positive_int, default=20, help="Maximum matches. Default: 20."
    )
    add_format_argument(people)
    messages = commands.add_parser(
        "messages",
        help="List messages without marking them read.",
        epilog="Default: one list request, metadata only. --body and --attachments opt into message detail requests. Offline listing filters cached records.",
    )
    add_folder_arguments(messages)
    messages.add_argument(
        "--limit", type=positive_int, default=20, help="Maximum results. Default: 20."
    )
    messages.add_argument("--offset", type=int, default=0, help="Result offset. Default: 0.")
    messages.add_argument("--unread", action="store_true", help="Only unread messages.")
    messages.add_argument(
        "--body",
        action="store_true",
        help="Also fetch full bodies. Default: use only the list response without per-message requests.",
    )
    add_refresh_arguments(messages, attachments=True)
    add_format_argument(messages)
    search = commands.add_parser(
        "search",
        help="Search mailbox messages.",
        epilog="Default: one search request, without per-message fetching. Queries are not cached. Offline search is unsupported; use messages --no-refresh --format json with jq or grep.",
    )
    search.add_argument("query", help="Outlook search query, for example 'subject:internship'.")
    add_folder_arguments(search)
    search.add_argument(
        "--limit", type=positive_int, default=20, help="Maximum results. Default: 20."
    )
    search.add_argument("--offset", type=int, default=0, help="Result offset. Default: 0.")
    search.add_argument(
        "--body",
        action="store_true",
        help="Also fetch full bodies. Default: use only the search response without per-message requests.",
    )
    add_refresh_arguments(search, attachments=True)
    add_format_argument(search)
    message = commands.add_parser(
        "message", help="Read one message without changing its read state."
    )
    message.add_argument("message_id", help="Message ID returned by messages or search.")
    add_refresh_arguments(message, attachments=True)
    add_format_argument(message)

    preview = commands.add_parser(
        "preview",
        help="Capture an Outlook web screenshot of a draft or message without changing mail.",
        epilog="Reuses a cached screenshot when the message is unchanged. --refresh forces recapture; --no-refresh requires a valid local preview. Screenshots show the viewport; read the returned body for long messages.",
    )
    preview.add_argument("message_id", help="Exact draft or message ID.")
    preview.add_argument(
        "--output",
        type=Path,
        help="Export a copy to this PNG path. The preview is always cached as preview.png beside the email's message.json.",
    )
    add_refresh_arguments(preview)
    add_format_argument(preview)

    for name, help_text in (
        ("flag", "Flag one message for follow-up."),
        ("unflag", "Remove a message's follow-up flag."),
        ("mark-read", "Mark one message as read."),
        ("mark-unread", "Mark one message as unread."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("message_id", help="Exact Outlook message ID.")
        add_format_argument(command)

    move = commands.add_parser("move", help="Move one message to another folder.")
    move.add_argument("message_id", help="Exact Outlook message ID.")
    add_folder_arguments(move)
    add_format_argument(move)

    draft = commands.add_parser(
        "draft", help="Create, edit, discard, or send an existing draft with user authorization."
    )
    draft_commands = draft.add_subparsers(dest="draft_command", required=True)
    create = draft_commands.add_parser("create", help="Save a new unsent message.")
    add_recipient_arguments(create)
    create.add_argument("--subject", required=True, help="Draft subject.")
    add_body_argument(create, required=True)
    add_attachment_argument(create)
    add_format_argument(create)
    reply = draft_commands.add_parser("reply", help="Save an unsent reply to one message.")
    reply.add_argument("message_id", help="Original message ID.")
    add_body_argument(reply, required=True)
    add_attachment_argument(reply)
    add_format_argument(reply)
    edit = draft_commands.add_parser(
        "edit", help="Edit specified draft fields and/or add attachments; save without sending."
    )
    edit.add_argument("message_id", help="Existing draft ID.")
    add_recipient_arguments(edit, editing=True)
    edit.add_argument("--subject", help="Replace the subject.")
    add_body_argument(edit, required=False)
    add_attachment_argument(edit)
    add_format_argument(edit)
    send = draft_commands.add_parser(
        "send",
        help="Send an existing draft only, saving a copy in Sent Items.",
        epilog="Requires user authorization. Review the draft and its preview first; present its link and preview for approval unless the user explicitly waived their review. Never retry an uncertain send without checking Sent Items.",
    )
    send.add_argument("message_id", help="Existing draft ID; non-draft messages are rejected.")
    add_format_argument(send)
    remove = draft_commands.add_parser("remove", help="Discard a draft into Deleted Items.")
    remove.add_argument("message_id", help="Draft message ID.")
    add_format_argument(remove)

    api = commands.add_parser(
        "api",
        help="Make a read-only Outlook web API request.",
    )
    api.add_argument(
        "path",
        help="Supported /owa/service.svc read action, /searchservice/api/v2/query (mail), or /search/api/v2/query (people) path.",
    )
    api.add_argument(
        "-X",
        "--method",
        type=str.upper,
        choices=("GET", "POST"),
        default="GET",
        help="HTTP method. Default: GET. POST is limited to supported read actions.",
    )
    api.add_argument(
        "-d", "--data", type=json_argument, default=None, metavar="JSON", help="JSON request body."
    )
    browser = commands.add_parser(
        "playwright-cli", help="Open an authenticated @playwright/cli inspection session."
    )
    browser.add_argument("--url", default=None, help="Initial Outlook URL. Default: mailbox.")
    mode = browser.add_mutually_exclusive_group()
    mode.add_argument(
        "--headed", dest="headed", action="store_true", help="Show the browser window."
    )
    mode.add_argument(
        "--headless", dest="headed", action="store_false", help="Run headless (default)."
    )
    browser.set_defaults(headed=False)
    browser.add_argument(
        "-s",
        "--session",
        default="nus-outlook",
        help="@playwright/cli session ID. Default: nus-outlook.",
    )
    return parser


def print_result(value: Any, output_format: str | None) -> None:
    if output_format == "json":
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return
    records = value if isinstance(value, list) else [value]
    if not records and output_format is None:
        print("No results.")
    for record in records:
        if output_format == "jsonl":
            print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        elif isinstance(record, dict):
            if output_format is None:
                record = human_record(record)
            for key, item in record.items():
                if item is not None:
                    rendered = (
                        json.dumps(item, ensure_ascii=False)
                        if isinstance(item, (dict, list))
                        else str(item)
                    )
                    print(f"{key}: {rendered}")
            if len(records) > 1:
                print()
        else:
            print(record)


def human_record(record: dict[str, Any]) -> dict[str, Any]:
    if "DisplayName" in record and "EmailAddresses" in record:
        return {
            "Name": record.get("DisplayName"),
            "Email": record.get("EmailAddresses"),
            "Title": record.get("JobTitle"),
            "Department": record.get("Department"),
            "Organization": record.get("CompanyName"),
            "Office": record.get("OfficeLocation"),
        }
    if "FolderId" in record:
        return {
            "Name": record.get("DisplayName"),
            "ID": record["FolderId"].get("Id"),
            "Folder": record.get("DistinguishedFolderId"),
            "Messages": record.get("TotalCount"),
            "Unread": record.get("UnreadCount"),
        }
    if "ItemId" not in record:
        return record
    sender = record.get("From") or record.get("Sender") or {}
    address = sender.get("Mailbox", sender) if isinstance(sender, dict) else {}
    name, email = address.get("Name", ""), address.get("EmailAddress", "")
    body = record.get("Body")
    return {
        "Subject": record.get("Subject"),
        "URL": record.get("url"),
        "ID": record["ItemId"].get("Id"),
        "From": f"{name} <{email}>" if name and email else (name or email or None),
        "Received": record.get("DateTimeReceived"),
        "Read": record.get("IsRead"),
        "Flag": record.get("Flag", {}).get("FlagStatus"),
        "Draft": record.get("IsDraft"),
        "Send status": record.get("status"),
        "Send pending": record.get("pending"),
        "Preview screenshot": record.get("_outlook", {}).get("preview_path"),
        "Preview reused": record.get("_outlook", {}).get("preview_reused"),
        "Preview export": record.get("_outlook", {}).get("preview_output_path"),
        "Attachments": record.get("HasAttachments"),
        "Attachment links": [
            {"name": a.get("name"), "url": a.get("url"), "local": a.get("path")}
            for a in record.get("_outlook", {}).get("attachments", [])
        ]
        or None,
        "Cache": record.get("_outlook", {}).get("path"),
        "Body cached": record.get("_outlook", {}).get("body_downloaded"),
        "Body": body.get("Value") if isinstance(body, dict) else (body or record.get("Preview")),
    }


def run(args: argparse.Namespace) -> int:
    options = {"site_name": args.site_name, "base_url": args.base_url, "timeout": args.timeout}
    if args.command == "auth":
        if args.auth_command == "logout":
            print(logout(site_name=args.site_name))
            return 0
        if args.auth_command == "login":
            status = login(
                **options,
                refresh=args.refresh,
                login_wait_seconds=args.wait_seconds,
                login_hint=args.login_hint,
            )
        else:
            status = check_auth_status(**options)
        if getattr(args, "format", None) == "json":
            print_result(status.as_dict(), "json")
        elif status.authenticated:
            print(f"Authenticated as {status.name or status.email}.")
        else:
            print("Not authenticated.")
            if status.error:
                print(status.error, file=sys.stderr)
        return 0 if status.authenticated else 1

    if args.command == "playwright-cli":
        executable = playwright_cli_executable()
        ensure_session_available(executable, args.session)
        url = args.url or args.base_url
        validate_base_url(url)
        client = OutlookClient(**options)
        client.profile()
        open_authenticated_session(
            executable=executable,
            session_id=args.session,
            site_name=args.site_name,
            url=url,
            headed=args.headed,
        )
        print(
            f"@playwright/cli session {args.session!r} is open. Use: playwright-cli -s={args.session} <command>"
        )
        return 0

    # Validate selectors before loading authentication or making a request.
    if getattr(args, "offset", 0) < 0:
        raise ValueError("--offset must be zero or greater")
    client = OutlookClient(**options)
    if args.command in {"folders", "messages", "search", "message", "preview"}:
        value = cached_read(args, client)
    elif args.command in {"flag", "unflag", "mark-read", "mark-unread", "move", "draft"}:
        value = cached_write(args, client)
    elif args.command == "people":
        value = client.people(args.query, limit=args.limit)
    elif args.command == "api":
        value = client.request(args.method, args.path, args.data)
    else:
        raise AssertionError(args.command)
    print_result(
        value, getattr(args, "format", None) or ("json" if args.command == "api" else None)
    )
    return 0


def cached_read(args: argparse.Namespace, client: OutlookClient) -> Any:
    store = OutlookStore(args.data_path)
    fetcher = OutlookFetcher(client, store)

    def read() -> Any:
        if args.command == "folders":
            return fetcher.folders(refresh_mode=args.refresh_mode)
        if args.command == "message":
            return fetcher.message(
                args.message_id, refresh_mode=args.refresh_mode, attachments=args.attachments
            )
        if args.command == "preview":
            return OutlookPreview(client, store).message(
                args.message_id, refresh_mode=args.refresh_mode, output=args.output
            )
        return fetcher.messages(
            folder=args.folder,
            folder_id=args.folder_id,
            limit=args.limit,
            offset=args.offset,
            unread=getattr(args, "unread", False),
            query=getattr(args, "query", None),
            refresh_mode=args.refresh_mode,
            body=args.body,
            attachments=args.attachments,
        )

    if getattr(args, "refresh_mode", None) == "none":
        return read()
    ensure_private_directory(store.root)
    with exclusive_file_lock(store.root / ".cache.lock"):
        return read()


def cached_write(args: argparse.Namespace, client: OutlookClient) -> Any:
    store = OutlookStore(args.data_path)
    ensure_private_directory(store.root)
    with exclusive_file_lock(store.root / ".cache.lock"):
        actions = OutlookActions(client, store)
        if args.command in {"flag", "unflag"}:
            return actions.flag(args.message_id, flagged=args.command == "flag")
        if args.command in {"mark-read", "mark-unread"}:
            return actions.mark_read(args.message_id, read=args.command == "mark-read")
        if args.command == "move":
            return actions.move(args.message_id, folder=args.folder, folder_id_value=args.folder_id)
        if args.draft_command == "send":
            return actions.send_draft(args.message_id)
        if args.draft_command == "remove":
            return actions.remove_draft(args.message_id)
        body = args.body_file.read_text(encoding="utf-8") if args.body_file else args.body
        if args.draft_command == "create":
            return actions.draft(
                to=args.to,
                cc=args.cc or [],
                bcc=args.bcc or [],
                subject=args.subject,
                body=body,
                attachments=args.attach,
            )
        if args.draft_command == "reply":
            return actions.reply_draft(args.message_id, body=body, attachments=args.attach)
        return actions.edit_draft(
            args.message_id,
            to=args.to,
            cc=args.cc,
            bcc=args.bcc,
            subject=args.subject,
            body=body,
            attachments=args.attach,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (
        OutlookError,
        PlaywrightCLIError,
        PlaywrightError,
        requests.RequestException,
        OSError,
        ValueError,
        TimeoutError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exit_code_for_error(exc)
