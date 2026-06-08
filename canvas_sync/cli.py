from __future__ import annotations

import argparse
import os
from pathlib import Path

from .auth import DEFAULT_LOGIN_WAIT_SECONDS
from .client import CanvasAPIError
from .sync import DEFAULT_BASE_URL, DEFAULT_DATA_PATH, DEFAULT_SITE_NAME, sync_canvas


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch Canvas course metadata and create local course folders."
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
        "--login-wait-seconds",
        type=int,
        default=int(os.getenv("CANVAS_LOGIN_WAIT_SECONDS", str(DEFAULT_LOGIN_WAIT_SECONDS))),
        help="How long to wait for browser login when the saved session is invalid.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        result = sync_canvas(
            data_path=Path(args.data_path),
            base_url=args.base_url,
            site_name=args.site_name,
            max_courses=args.max_courses,
            login_wait_seconds=args.login_wait_seconds,
        )
    except CanvasAPIError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"Synced {result.course_count} course(s) into {result.data_path}")
    print(f"Index: {result.index_path}")
    if result.session_refreshed:
        print("Canvas session was refreshed through browser login.")
    else:
        print("Canvas session was valid; no browser login needed.")


if __name__ == "__main__":
    main()
