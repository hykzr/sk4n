from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from agent_for_nus.errors import exit_code_for_error
from agent_for_nus.paths import canvas_data_dir
from tools.playwright_cli import (
    ensure_session_available,
    open_authenticated_session,
    playwright_cli_executable,
)
from tools.shared import exclusive_file_lock

from .auth import (
    DEFAULT_LOGIN_WAIT_SECONDS,
    check_auth_status,
    login,
    logout,
)
from .client import CanvasAPIError, CanvasAuthError, CanvasClient
from .fetcher import CanvasFetcher
from .sync import sync_canvas
from .utils import DEFAULT_BASE_URL, DEFAULT_SITE_NAME

console = Console()
error_console = Console(stderr=True)
OUTPUT_FORMATS = ("json", "jsonl", "plain")


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def iso_date_argument(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO date such as 2026-08-10") from exc


def http_method(value: str) -> str:
    method = value.strip().upper()
    if not method.isalpha():
        raise argparse.ArgumentTypeError("must contain only letters")
    return method


def json_argument(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"must be valid JSON: {exc.msg}") from exc


def key_value(value: str) -> tuple[str, str]:
    for separator in ("=", ":"):
        if separator in value:
            key, item = value.split(separator, 1)
            if key.strip():
                return key.strip(), item.strip()
    raise argparse.ArgumentTypeError("must be NAME=VALUE or NAME:VALUE")


def add_format_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        choices=OUTPUT_FORMATS,
        default=None,
        help="Output format. Default: human-friendly rich output.",
    )


def add_refresh_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--refresh",
        action="store_const",
        const="force",
        dest="refresh_mode",
        help="Force refresh the requested remote data and local artifacts.",
    )
    group.add_argument(
        "--no-refresh",
        action="store_const",
        const="none",
        dest="refresh_mode",
        help="Read only from the local cache.",
    )
    parser.set_defaults(refresh_mode="default")


def add_sync_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-courses",
        type=int,
        default=None,
        help="Optional debugging limit for the number of courses to sync.",
    )
    parser.add_argument(
        "--course",
        action="append",
        nargs="+",
        default=[],
        help="Sync only matching course IDs or course codes. Can be repeated.",
    )
    parser.add_argument(
        "--refresh-course",
        action="store_true",
        help="Refresh existing course metadata, tabs, cover image, and syllabus.",
    )
    for content_type in (
        "people",
        "content",
        "announcements",
        "discussions",
        "pages",
        "syllabus",
        "modules",
        "assignments",
        "files",
    ):
        parser.add_argument(
            f"--refresh-{content_type}",
            action="store_true",
            help=(
                "Force refresh people and groups."
                if content_type == "people"
                else f"Force refresh {content_type.replace('-', ' ')}."
            ),
        )
    for content_type in (
        "announcements",
        "discussions",
        "people",
        "pages",
        "syllabus",
        "modules",
        "assignments",
        "files",
    ):
        parser.add_argument(
            f"--skip-{content_type}",
            action="store_true",
            help=(
                "Skip syncing people and groups."
                if content_type == "people"
                else f"Skip syncing {content_type}."
            ),
        )
    parser.add_argument(
        "--login-only",
        action="store_true",
        help="Validate or refresh the saved Canvas login, then exit without syncing.",
    )


def build_parser() -> argparse.ArgumentParser:
    default_data_path = canvas_data_dir()
    parser = argparse.ArgumentParser(
        prog="canvas",
        description="Query and incrementally cache NUS Canvas data.",
        epilog=(
            "Exit codes: 0 success, 1 unauthenticated status, 2 validation, "
            "3 authentication, 4 transport, 5 remote HTTP/response failure."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("CANVAS_BASE_URL", DEFAULT_BASE_URL),
        help=f"Canvas base URL. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--site-name",
        default=os.getenv("CANVAS_SITE_NAME", DEFAULT_SITE_NAME),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--data-path",
        type=Path,
        default=default_data_path,
        help=f"Cache folder. Default: {default_data_path}",
    )
    parser.add_argument(
        "--timeout",
        type=positive_int,
        default=int(os.getenv("CANVAS_TIMEOUT", "30")),
        help="HTTP timeout in seconds. Default: 30.",
    )
    parser.add_argument(
        "--login-wait-seconds",
        type=positive_int,
        default=int(os.getenv("CANVAS_LOGIN_WAIT_SECONDS", str(DEFAULT_LOGIN_WAIT_SECONDS))),
        help=argparse.SUPPRESS,
    )

    commands = parser.add_subparsers(dest="command", required=True)

    auth_parser = commands.add_parser("auth", help="Manage the saved NUS Canvas login.")
    auth_commands = auth_parser.add_subparsers(dest="auth_command", required=True)
    status_parser = auth_commands.add_parser("status", help="Check the saved login.")
    add_format_argument(status_parser)
    login_parser = auth_commands.add_parser("login", help="Open a browser for NUS login.")
    login_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore the saved session and perform a fresh login.",
    )
    login_parser.add_argument(
        "--wait-seconds",
        type=positive_int,
        default=None,
        help=f"Login detection timeout. Default: {DEFAULT_LOGIN_WAIT_SECONDS}.",
    )
    auth_commands.add_parser(
        "logout",
        help="Delete the CLI's saved session without signing other browsers out of NUS SSO.",
    )

    sync_parser = commands.add_parser("sync", help="Sync courses using the original bulk options.")
    add_sync_arguments(sync_parser)

    student_parser = commands.add_parser("student", help="Show the current Canvas student.")
    add_refresh_arguments(student_parser)
    add_format_argument(student_parser)

    list_parser = commands.add_parser(
        "list",
        help="List Canvas courses, including retired courses retained in the cache.",
    )
    list_parser.add_argument(
        "-s",
        "--semester",
        metavar="SEM",
        help="Case-insensitive semester: latest, 2526S1, AY2526S1, or Y3S1.",
    )
    add_refresh_arguments(list_parser)
    add_format_argument(list_parser)

    events_parser = commands.add_parser(
        "calendar-events",
        help="List the current user's Canvas calendar events.",
    )
    events_parser.add_argument(
        "--start",
        type=iso_date_argument,
        default=None,
        metavar="YYYY-MM-DD",
        help="Inclusive start date in Canvas time. Default: Canvas endpoint default.",
    )
    events_parser.add_argument(
        "--end",
        type=iso_date_argument,
        default=None,
        metavar="YYYY-MM-DD",
        help="Inclusive end date in Canvas time. Default: Canvas endpoint default.",
    )
    events_parser.add_argument(
        "--type",
        choices=("event", "assignment"),
        default="event",
        dest="event_type",
        help="Canvas calendar item type. Default: event.",
    )
    add_format_argument(events_parser)

    todo_parser = commands.add_parser("todo", help="List the current user's Canvas To-Do items.")
    add_format_argument(todo_parser)

    upcoming_parser = commands.add_parser(
        "upcoming",
        help="List the current user's upcoming Canvas events.",
    )
    add_format_argument(upcoming_parser)

    course_parser = commands.add_parser("course", help="Show a course or one cached content area.")
    course_parser.add_argument("course_code", help="Course code or Canvas course ID.")
    course_parser.add_argument(
        "-s",
        "--semester",
        metavar="SEM",
        help="Restrict course-code matches to a semester, including Non-Academic.",
    )
    course_parser.add_argument(
        "resource",
        nargs="?",
        help="path, home, announcements, assignments, discussions, files, groups, modules, pages, people, quizzes, or syllabus.",
    )
    course_parser.add_argument(
        "item",
        nargs="?",
        default="list",
        help="For a resource: list, path, or an item ID.",
    )
    add_refresh_arguments(course_parser)
    add_format_argument(course_parser)

    api_parser = commands.add_parser(
        "api",
        help="Send a direct low-level request with the saved Canvas session.",
    )
    api_parser.add_argument(
        "url",
        help=("Canvas API path or same-origin absolute URL; query parameters may be included."),
    )
    api_parser.add_argument(
        "-X",
        "--method",
        type=http_method,
        default="GET",
        help="HTTP method. Default: GET.",
    )
    api_parser.add_argument(
        "-d",
        "--data",
        type=json_argument,
        default=None,
        metavar="JSON",
        help="JSON request body.",
    )
    api_parser.add_argument(
        "--param",
        action="append",
        type=key_value,
        default=[],
        metavar="NAME=VALUE",
        help="Query parameter. Repeat for multiple values.",
    )
    api_parser.add_argument(
        "-H",
        "--header",
        action="append",
        type=key_value,
        default=[],
        metavar="NAME:VALUE",
        help="Request header. Repeat for multiple headers.",
    )

    playwright_parser = commands.add_parser(
        "playwright-cli",
        help="Open an authenticated low-level @playwright/cli browser session.",
    )
    playwright_parser.add_argument(
        "--url",
        default=None,
        help="Initial URL. Default: the Canvas root page.",
    )
    browser_mode = playwright_parser.add_mutually_exclusive_group()
    browser_mode.add_argument(
        "--headless",
        action="store_false",
        dest="headed",
        help="Run headless (default).",
    )
    browser_mode.add_argument(
        "--headed",
        action="store_true",
        dest="headed",
        help="Show the browser window.",
    )
    playwright_parser.set_defaults(headed=False)
    playwright_parser.add_argument(
        "-s",
        "--session",
        default="canvas",
        help="@playwright/cli session ID. Default: canvas.",
    )
    return parser


def _json_text(value: Any, *, pretty: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=not pretty,
        indent=2 if pretty else None,
        default=str,
    )


def _records(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        return [value]
    return [{"value": value}]


def _without_canvas_sync_metadata(value: Any) -> Any:
    if isinstance(value, list):
        return [_without_canvas_sync_metadata(item) for item in value]
    if isinstance(value, Mapping):
        return {
            key: _without_canvas_sync_metadata(item)
            for key, item in value.items()
            if not str(key).startswith("_canvas_sync")
        }
    return value


def print_formatted(value: Any, output_format: str) -> None:
    if output_format == "json":
        print(_json_text(_without_canvas_sync_metadata(value)))
        return
    records = _records(_without_canvas_sync_metadata(value) if output_format == "jsonl" else value)
    if output_format == "jsonl":
        for record in records:
            print(_json_text(record))
        return
    if output_format == "plain":
        for index, record in enumerate(records):
            if index:
                print("-" * 72)
            for key in sorted(record):
                item = record[key]
                if isinstance(item, (Mapping, list)):
                    rendered = _json_text(item)
                elif item is None:
                    rendered = "null"
                elif isinstance(item, bool):
                    rendered = str(item).lower()
                else:
                    rendered = str(item).replace("\n", "\\n")
                print(f"{key}: {rendered}")
        return
    raise ValueError(f"Unknown output format: {output_format}")


def print_detail(value: Mapping[str, Any], title: str | None = None) -> None:
    if title:
        console.print(f"[bold]{escape(title)}[/bold]")
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="cyan", no_wrap=True)
    table.add_column(overflow="fold")
    for key, item in value.items():
        if item in (None, "", [], {}):
            continue
        rendered = _json_text(item, pretty=True) if isinstance(item, (Mapping, list)) else str(item)
        table.add_row(key.replace("_", " ").title(), escape(rendered))
    console.print(table)


def print_courses(courses: Sequence[Mapping[str, Any]]) -> None:
    table = Table(title=f"Canvas courses ({len(courses)})", expand=True)
    table.add_column("Semester", no_wrap=True)
    table.add_column("Course", no_wrap=True)
    table.add_column("Name", ratio=1, overflow="fold")
    table.add_column("Availability", no_wrap=True)
    table.add_column("Role", overflow="fold")
    table.add_column("ID", no_wrap=True)
    for course in courses:
        table.add_row(
            str(course.get("term_folder_name") or ""),
            str(course.get("course_code") or ""),
            str(course.get("name") or ""),
            str(course.get("availability_status") or "unknown").title(),
            ", ".join(str(role) for role in course.get("enrollment_roles") or []),
            str(course.get("id") or ""),
        )
    console.print(table)


def _content_item_type(item: Mapping[str, Any]) -> str:
    direct = item.get("kind") or item.get("type")
    if direct:
        return str(direct)
    enrollments = item.get("enrollments")
    if isinstance(enrollments, Sequence) and not isinstance(enrollments, (str, bytes)):
        roles = list(
            dict.fromkeys(
                str(enrollment.get("role") or enrollment.get("type"))
                for enrollment in enrollments
                if isinstance(enrollment, Mapping)
                and (enrollment.get("role") or enrollment.get("type"))
            )
        )
        if roles:
            return ", ".join(roles)
    return ""


def _print_shared_local_path(items: Sequence[Mapping[str, Any]]) -> tuple[bool, str | None]:
    local_paths = {
        str(item.get("local_path")) for item in items if item.get("local_path") not in (None, "")
    }
    shared_local_path = next(iter(local_paths)) if len(local_paths) == 1 else None
    if shared_local_path:
        console.print(f"[cyan]Local file:[/cyan] {escape(shared_local_path)}")
    return bool(local_paths), shared_local_path


def print_content_list(items: Sequence[Mapping[str, Any]]) -> None:
    for item in items:
        if item.get("inaccessible") is not True:
            continue
        link = str(item.get("html_url") or item.get("url") or item.get("download_url") or "")
        error = str(item.get("access_error") or "Canvas denied access")
        console.print(
            f"[yellow]Inaccessible Canvas link:[/yellow] {escape(link)} ({escape(error)})"
        )
    has_local_paths, shared_local_path = _print_shared_local_path(items)
    table = Table(title=f"Canvas items ({len(items)})", expand=True)
    table.add_column("ID", no_wrap=True)
    has_types = any(_content_item_type(item) for item in items)
    if has_types:
        table.add_column("Type", no_wrap=True)
    table.add_column("Name", ratio=1, overflow="fold")
    has_canvas_links = any(
        item.get("html_url") or item.get("url") or item.get("download_url") for item in items
    )
    has_status = any(
        item.get("inaccessible") is True or item.get("download_error") for item in items
    )
    if has_canvas_links:
        table.add_column("Canvas link", ratio=1, overflow="fold")
    if has_status:
        table.add_column("Status", ratio=1, overflow="fold")
    if has_local_paths and not shared_local_path:
        table.add_column("Local path", ratio=1, overflow="fold")
    for item in items:
        row = [str(item.get("id") or item.get("key") or item.get("url") or "")]
        if has_types:
            row.append(_content_item_type(item))
        row.append(str(item.get("name") or item.get("title") or item.get("display_name") or ""))
        if has_canvas_links:
            row.append(
                str(item.get("html_url") or item.get("url") or item.get("download_url") or "")
            )
        if has_status:
            if item.get("inaccessible") is True:
                row.append(f"Inaccessible: {item.get('access_error') or 'Canvas denied access'}")
            elif item.get("download_error"):
                row.append(f"Download failed: {item.get('download_error')}")
            else:
                row.append("")
        if has_local_paths and not shared_local_path:
            row.append(str(item.get("local_path") or ""))
        table.add_row(*row)
    console.print(table)


def print_group_list(items: Sequence[Mapping[str, Any]]) -> None:
    _print_shared_local_path(items)
    table = Table(title=f"Canvas groups ({len(items)})", expand=True)
    table.add_column("ID", no_wrap=True)
    table.add_column("Group set", overflow="fold")
    table.add_column("Name", ratio=1, overflow="fold")
    table.add_column("Members", no_wrap=True)
    table.add_column("Member names", ratio=2, overflow="fold")
    has_membership = any(item.get("is_current_user_member") is True for item in items)
    if has_membership:
        table.add_column("My group", no_wrap=True)
    has_canvas_links = any(item.get("html_url") for item in items)
    if has_canvas_links:
        table.add_column("Canvas link", ratio=1, overflow="fold")
    for item in items:
        category_value = item.get("group_category")
        category = category_value if isinstance(category_value, Mapping) else {}
        users = item.get("users")
        member_names = [
            str(user.get("name") or user.get("display_name") or "")
            for user in users or []
            if isinstance(user, Mapping) and (user.get("name") or user.get("display_name"))
        ]
        row = [
            str(item.get("id") or ""),
            str(category.get("name") or item.get("group_category_id") or ""),
            str(item.get("name") or ""),
            str(
                item.get("members_count")
                if item.get("members_count") is not None
                else len(member_names)
            ),
            ", ".join(member_names),
        ]
        if has_membership:
            row.append("Yes" if item.get("is_current_user_member") is True else "")
        if has_canvas_links:
            row.append(str(item.get("html_url") or ""))
        table.add_row(*row)
    console.print(table)


def print_activity_list(items: Sequence[Mapping[str, Any]], title: str) -> None:
    if not items:
        console.print(f"No {escape(title.casefold())} found.")
        return
    table = Table(title=f"{title} ({len(items)})", expand=True)
    table.add_column("ID", no_wrap=True)
    table.add_column("Title", ratio=1, overflow="fold")
    table.add_column("When", no_wrap=True)
    table.add_column("Context", ratio=1, overflow="fold")
    table.add_column("Canvas link", ratio=1, overflow="fold")
    for item in items:
        assignment_value = item.get("assignment")
        assignment = assignment_value if isinstance(assignment_value, Mapping) else {}
        quiz_value = item.get("quiz")
        quiz = quiz_value if isinstance(quiz_value, Mapping) else {}
        table.add_row(
            str(item.get("id") or assignment.get("id") or quiz.get("id") or ""),
            str(
                item.get("title")
                or item.get("name")
                or assignment.get("name")
                or quiz.get("title")
                or ""
            ),
            str(
                item.get("start_at")
                or item.get("all_day_date")
                or item.get("due_at")
                or assignment.get("due_at")
                or quiz.get("due_at")
                or ""
            ),
            str(item.get("context_name") or item.get("course_id") or ""),
            str(
                item.get("html_url")
                or item.get("url")
                or assignment.get("html_url")
                or quiz.get("html_url")
                or ""
            ),
        )
    console.print(table)


def print_empty_home(value: Mapping[str, Any]) -> None:
    resource = str(value.get("resource") or "content").casefold()
    messages = {
        "modules": "No modules have been defined for this course.",
        "assignments": "No assignments have been defined for this course.",
        "pages": "No pages have been defined for this course.",
        "activity_stream": "No activity has been posted for this course.",
    }
    console.print(messages.get(resource, "No Home content has been defined for this course."))


def print_course_detail(value: Mapping[str, Any], fallback_code: str) -> None:
    course_value = value.get("course")
    course: Mapping[str, Any] = course_value if isinstance(course_value, Mapping) else {}
    term_value = course.get("term")
    term: Mapping[str, Any] = term_value if isinstance(term_value, Mapping) else {}
    print_detail(
        {
            "course_code": course.get("course_code") or fallback_code,
            "name": course.get("name"),
            "id": course.get("id"),
            "availability": value.get("availability_status"),
            "term": term.get("name"),
            "enrollment_state": course.get("enrollment_state"),
            "roles": course.get("enrollment_roles")
            or list(
                dict.fromkeys(
                    str(section.get("enrollment_role"))
                    for section in course.get("enrolled_sections") or []
                    if isinstance(section, Mapping) and section.get("enrollment_role")
                )
            ),
            "workflow_state": course.get("workflow_state"),
            "default_view": course.get("default_view"),
            "sections": list(
                dict.fromkeys(
                    section.get("name")
                    for section in course.get("enrolled_sections") or []
                    if isinstance(section, Mapping) and section.get("name")
                )
            ),
            "available_content": [
                section.get("label") or section.get("id")
                for section in value.get("available_sections") or []
                if isinstance(section, Mapping)
            ],
            "course_path": value.get("course_path"),
            "local_path": value.get("local_path"),
        },
        str(course.get("course_code") or fallback_code),
    )


def _fetcher(args: argparse.Namespace) -> CanvasFetcher:
    return CanvasFetcher(
        data_path=args.data_path,
        base_url=args.base_url,
        site_name=args.site_name,
        timeout=args.timeout,
        login_wait_seconds=args.login_wait_seconds,
    )


def _refresh_values(args: argparse.Namespace) -> tuple[bool, bool]:
    return args.refresh_mode != "none", args.refresh_mode == "force"


def handle_auth(args: argparse.Namespace) -> int:
    if args.auth_command == "status":
        status = check_auth_status(
            base_url=args.base_url,
            site_name=args.site_name,
            timeout=args.timeout,
        )
        value = {
            "authenticated": status.authenticated,
            "name": status.name or None,
            "email": status.email or None,
            "user_id": status.user_id or None,
            "error": status.error or None,
        }
        if args.format:
            print_formatted(value, args.format)
        elif status.authenticated:
            identity = status.name or status.email
            console.print(f"Authenticated{f' as {escape(identity)}' if identity else ''}.")
        else:
            console.print("Not authenticated.")
            if status.error:
                console.print(f"[dim]{escape(status.error)}[/dim]")
        return 0 if status.authenticated else 1
    if args.auth_command == "login":
        status = login(
            base_url=args.base_url,
            site_name=args.site_name,
            login_wait_seconds=args.wait_seconds or args.login_wait_seconds,
            refresh=args.refresh,
        )
        identity = status.name or status.email
        console.print(f"Authenticated{f' as {escape(identity)}' if identity else ''}.")
        return 0
    if args.auth_command == "logout":
        console.print(logout(site_name=args.site_name))
        return 0
    raise AssertionError(f"Unknown auth command: {args.auth_command}")


def handle_sync(args: argparse.Namespace) -> int:
    course_selectors = [item for group in args.course for item in group]
    result = sync_canvas(
        data_path=args.data_path,
        base_url=args.base_url,
        site_name=args.site_name,
        max_courses=args.max_courses,
        login_wait_seconds=args.login_wait_seconds,
        course_selectors=course_selectors,
        refresh_course=args.refresh_course,
        refresh_people=args.refresh_people,
        refresh_content=args.refresh_content,
        refresh_announcements=args.refresh_announcements,
        refresh_discussions=args.refresh_discussions,
        refresh_pages=args.refresh_pages,
        refresh_syllabus=args.refresh_syllabus,
        refresh_modules=args.refresh_modules,
        refresh_assignments=args.refresh_assignments,
        refresh_files=args.refresh_files,
        skip_announcements=args.skip_announcements,
        skip_discussions=args.skip_discussions,
        skip_people=args.skip_people,
        skip_pages=args.skip_pages,
        skip_syllabus=args.skip_syllabus,
        skip_modules=args.skip_modules,
        skip_assignments=args.skip_assignments,
        skip_files=args.skip_files,
        login_only=args.login_only,
        show_progress=True,
        console=console,
    )
    if args.login_only:
        console.print("Canvas login check complete.")
        return 0
    console.print(
        f"Sync complete; cache contains {result.course_count} course(s) in "
        f"{result.data_path.resolve()}"
    )
    console.print(f"Index: {result.index_path.resolve()}")
    return 0


def handle_student(args: argparse.Namespace) -> int:
    refresh, _ = _refresh_values(args)
    student = _fetcher(args).student(refresh=refresh)
    if args.format:
        print_formatted(student, args.format)
    else:
        print_detail(student, "Canvas student")
    return 0


def handle_list(args: argparse.Namespace) -> int:
    refresh, _ = _refresh_values(args)
    courses = _fetcher(args).courses(semester=args.semester, refresh=refresh)
    if args.format:
        print_formatted(courses, args.format)
    else:
        print_courses(courses)
    return 0


def handle_calendar_events(args: argparse.Namespace) -> int:
    if args.start and args.end and args.start > args.end:
        raise ValueError("--start must be on or before --end.")
    items = _fetcher(args).calendar_events(
        start=args.start,
        end=args.end,
        event_type=args.event_type,
    )
    if args.format:
        print_formatted(items, args.format)
    else:
        print_activity_list(items, "Canvas calendar events")
    return 0


def handle_todo(args: argparse.Namespace) -> int:
    items = _fetcher(args).todo()
    if args.format:
        print_formatted(items, args.format)
    else:
        print_activity_list(items, "Canvas To-Do items")
    return 0


def handle_upcoming(args: argparse.Namespace) -> int:
    items = _fetcher(args).upcoming()
    if args.format:
        print_formatted(items, args.format)
    else:
        print_activity_list(items, "Upcoming Canvas events")
    return 0


def handle_course(args: argparse.Namespace) -> int:
    refresh, force = _refresh_values(args)
    fetcher = _fetcher(args)
    try:
        if args.resource is None:
            value = fetcher.course(
                args.course_code,
                semester=args.semester,
                refresh=refresh,
                force=force,
            )
            if args.format:
                print_formatted(value, args.format)
            else:
                print_course_detail(value, args.course_code)
            return 0
        if args.resource.casefold() == "path":
            if args.item != "list":
                raise ValueError("`course CODE path` does not accept an item selector.")
            path = fetcher.course_path(
                args.course_code,
                semester=args.semester,
                refresh=refresh,
                force=force,
            ).as_posix()
            if args.format:
                print_formatted({"path": path}, args.format)
            else:
                print(path)
            return 0
        value = fetcher.content(
            args.course_code,
            args.resource,
            args.item,
            semester=args.semester,
            refresh=refresh,
            force=force,
        )
        if isinstance(value, Path):
            output: Any = {"path": value.resolve().as_posix()}
            if args.format:
                print_formatted(output, args.format)
            else:
                print(output["path"])
        elif args.format:
            print_formatted(value, args.format)
        elif isinstance(value, list):
            if args.resource.casefold() in {"group", "groups"}:
                print_group_list(value)
            else:
                print_content_list(value)
        elif isinstance(value, Mapping):
            if args.resource.casefold() == "home" and value.get("count") == 0:
                print_empty_home(value)
            else:
                print_detail(value)
        else:
            print(value)
        return 0
    finally:
        selected = fetcher.last_selected_course
        if selected and selected.get("availability_status") == "retired":
            label = selected.get("course_code") or selected.get("name") or args.course_code
            error_console.print(
                "[yellow]Notice:[/yellow] "
                f"{escape(str(label))} has been retired and is no longer accessible on Canvas; "
                "showing data from the archived cache.",
                soft_wrap=True,
            )


def handle_api(args: argparse.Namespace) -> int:
    client = CanvasClient(
        base_url=args.base_url,
        site_name=args.site_name,
        timeout=args.timeout,
    )
    payload = client.request(
        args.method,
        args.url,
        args.data,
        params=args.param or None,
        headers=dict(args.header) if args.header else None,
    )
    if isinstance(payload, str):
        sys.stdout.write(payload)
        if payload and not payload.endswith("\n"):
            sys.stdout.write("\n")
    else:
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    return 0


def handle_playwright_cli(args: argparse.Namespace) -> int:
    executable = playwright_cli_executable()
    ensure_session_available(executable, args.session)
    status = check_auth_status(
        base_url=args.base_url,
        site_name=args.site_name,
        timeout=args.timeout,
    )
    if not status.authenticated:
        raise CanvasAuthError(
            "No valid saved Canvas login. Run `canvas auth login` before opening an "
            "authenticated playwright-cli session."
        )
    url = args.url or args.base_url
    open_authenticated_session(
        executable=executable,
        session_id=args.session,
        site_name=args.site_name,
        url=url,
        headed=args.headed,
    )
    identity = status.name or status.email
    if identity:
        console.print(f"Authenticated as {escape(identity)}.")
    console.print(
        f"@playwright/cli session [cyan]{escape(args.session)}[/cyan] is open at {escape(url)}."
    )
    console.print(f"Use: playwright-cli -s={escape(args.session)} <command>")
    return 0


def _run_unlocked(args: argparse.Namespace) -> int:
    if args.command == "auth":
        return handle_auth(args)
    if args.command == "sync":
        return handle_sync(args)
    if args.command == "student":
        return handle_student(args)
    if args.command == "list":
        return handle_list(args)
    if args.command == "calendar-events":
        return handle_calendar_events(args)
    if args.command == "todo":
        return handle_todo(args)
    if args.command == "upcoming":
        return handle_upcoming(args)
    if args.command == "course":
        return handle_course(args)
    if args.command == "api":
        return handle_api(args)
    if args.command == "playwright-cli":
        return handle_playwright_cli(args)
    raise AssertionError(f"Unknown command: {args.command}")


def run(args: argparse.Namespace) -> int:
    cache_write = args.command == "sync" or (
        args.command in {"student", "list", "course"}
        and getattr(args, "refresh_mode", "none") != "none"
    )
    if not cache_write:
        return _run_unlocked(args)
    lock_path = Path(args.data_path).expanduser().resolve() / ".cache.lock"
    with exclusive_file_lock(lock_path):
        return _run_unlocked(args)


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        raise SystemExit(run(args))
    except (CanvasAPIError, PlaywrightError, RuntimeError, TimeoutError, ValueError) as exc:
        error_console.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise SystemExit(exit_code_for_error(exc)) from exc


if __name__ == "__main__":
    main()
