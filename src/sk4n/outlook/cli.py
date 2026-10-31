from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

import requests
from playwright.async_api import Error as PlaywrightError

from sk4n.errors import exit_code_for_error
from sk4n.tools.playwright_cli import (
    PlaywrightCLIError,
    ensure_session_available,
    open_authenticated_session,
    playwright_cli_executable,
)

from .auth import DEFAULT_LOGIN_WAIT_SECONDS, check_auth_status, login, logout
from .browser import validate_base_url
from .client import DEFAULT_BASE_URL, DEFAULT_SITE_NAME, OutlookClient, OutlookError


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nus-outlook",
        description="Read NUS Outlook mail and look up people using a saved NUS SSO browser login.",
        epilog=(
            "Exit codes: 0 success, 1 unauthenticated status, 2 validation, "
            "3 authentication, 4 transport, 5 remote HTTP/response failure."
        ),
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
    messages = commands.add_parser("messages", help="List messages without marking them read.")
    messages.add_argument(
        "--folder", default="inbox", help="Distinguished folder name or folder ID. Default: inbox."
    )
    messages.add_argument(
        "--limit", type=positive_int, default=20, help="Maximum results. Default: 20."
    )
    messages.add_argument("--offset", type=int, default=0, help="Result offset. Default: 0.")
    messages.add_argument("--unread", action="store_true", help="Only unread messages.")
    add_format_argument(messages)
    search = commands.add_parser("search", help="Search mailbox messages.")
    search.add_argument("query", help="Outlook search query, for example 'subject:internship'.")
    search.add_argument(
        "--folder", default="inbox", help="Distinguished folder name or folder ID. Default: inbox."
    )
    search.add_argument(
        "--limit", type=positive_int, default=20, help="Maximum results. Default: 20."
    )
    search.add_argument("--offset", type=int, default=0, help="Result offset. Default: 0.")
    add_format_argument(search)
    message = commands.add_parser(
        "message", help="Read one message without changing its read state."
    )
    message.add_argument("message_id", help="Message ID returned by messages or search.")
    add_format_argument(message)

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
        "ID": record["ItemId"].get("Id"),
        "From": f"{name} <{email}>" if name and email else (name or email or None),
        "Received": record.get("DateTimeReceived"),
        "Read": record.get("IsRead"),
        "Attachments": record.get("HasAttachments"),
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
    if args.command == "folders":
        value = client.folders()
    elif args.command == "people":
        value = client.people(args.query, limit=args.limit)
    elif args.command in {"messages", "search"}:
        value = client.messages(
            folder=args.folder,
            limit=args.limit,
            offset=args.offset,
            unread=getattr(args, "unread", False),
            query=getattr(args, "query", None),
        )
    elif args.command == "message":
        value = client.message(args.message_id)
    elif args.command == "api":
        value = client.request(args.method, args.path, args.data)
    else:
        raise AssertionError(args.command)
    print_result(
        value, getattr(args, "format", None) or ("json" if args.command == "api" else None)
    )
    return 0


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
