from __future__ import annotations

import argparse
import os
from pathlib import Path

import pyrootutils
from rich.console import Console

pyroot = pyrootutils.setup_root(__file__, dotenv=True, pythonpath=True, cwd=True)

try:
    from .auth import DEFAULT_LOGIN_WAIT_SECONDS
    from .client import CanvasAPIError
    from .sync import (
        DEFAULT_BASE_URL,
        DEFAULT_DATA_PATH,
        DEFAULT_SITE_NAME,
        sync_canvas,
    )
except ImportError:
    from auth import DEFAULT_LOGIN_WAIT_SECONDS
    from client import CanvasAPIError
    from sync import DEFAULT_BASE_URL, DEFAULT_DATA_PATH, DEFAULT_SITE_NAME, sync_canvas


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch Canvas course metadata and opened course content."
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("CANVAS_BASE_URL", DEFAULT_BASE_URL),
        help=f"Canvas base URL. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--site-name",
        default=os.getenv("CANVAS_SITE_NAME", DEFAULT_SITE_NAME),
        help=f"Saved Canvas session name. Default: {DEFAULT_SITE_NAME}",
    )
    parser.add_argument(
        "--data-path",
        default=os.getenv("CANVAS_DATA_PATH", str(DEFAULT_DATA_PATH)),
        help=f"Destination folder. Default: {DEFAULT_DATA_PATH}",
    )
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
        help="Refresh existing course.json metadata, tabs, cover image, and syllabus.",
    )
    parser.add_argument(
        "--refresh-people",
        action="store_true",
        help="Refresh people.json for existing courses.",
    )
    parser.add_argument(
        "--refresh-content",
        action="store_true",
        help="Force refresh all supported content tabs.",
    )
    parser.add_argument(
        "--refresh-announcements",
        action="store_true",
        help="Force refresh announcements even when cached signatures match.",
    )
    parser.add_argument(
        "--refresh-discussions",
        action="store_true",
        help="Force refresh discussions and discussion reply views.",
    )
    parser.add_argument(
        "--refresh-pages",
        action="store_true",
        help="Force refresh page bodies even when Canvas page summaries look unchanged.",
    )
    parser.add_argument(
        "--refresh-syllabus",
        action="store_true",
        help="Force refresh syllabus body.",
    )
    parser.add_argument(
        "--refresh-modules",
        action="store_true",
        help="Force refresh modules.json.",
    )
    parser.add_argument(
        "--refresh-assignments",
        action="store_true",
        help="Force refresh assignments, quizzes, images, and submitted files.",
    )
    parser.add_argument(
        "--refresh-files",
        action="store_true",
        help="Force refresh files metadata and re-download files.",
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
            help=f"Skip syncing {content_type}.",
        )
    parser.add_argument(
        "--login-only",
        action="store_true",
        help="Validate or refresh the saved Canvas login, then exit without syncing.",
    )
    parser.add_argument(
        "--login-wait-seconds",
        type=int,
        default=int(os.getenv("CANVAS_LOGIN_WAIT_SECONDS", str(DEFAULT_LOGIN_WAIT_SECONDS))),
        help="How long to wait for browser login when the saved session is invalid.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    console = Console()
    course_selectors = [item for group in args.course for item in group]
    try:
        result = sync_canvas(
            data_path=Path(args.data_path),
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
    except CanvasAPIError as exc:
        raise SystemExit(str(exc)) from exc

    if args.login_only:
        console.print("Canvas login check complete.")
        if result.session_refreshed:
            console.print("Canvas session was refreshed through browser login.")
        else:
            console.print("Canvas session was valid; no browser login needed.")
        return

    console.print(f"Synced {result.course_count} course(s) into {result.data_path}")
    console.print(f"Index: {result.index_path}")
    if result.session_refreshed:
        console.print("Canvas session was refreshed through browser login.")
    else:
        console.print("Canvas session was valid; no browser login needed.")


if __name__ == "__main__":
    main()
