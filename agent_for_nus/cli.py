from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence

from .doctor import build_doctor_report, package_version, path_report, print_doctor_report

OUTPUT_FORMATS = ("human", "json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-for-nus",
        description="Manage the local Agent for NUS command-line tools.",
    )
    parser.add_argument(
        "--version", "-v", action="version", version=f"%(prog)s {package_version()}"
    )
    commands = parser.add_subparsers(dest="command")

    doctor_parser = commands.add_parser("doctor", help="Diagnose the local installation.")
    doctor_parser.add_argument("--format", choices=OUTPUT_FORMATS, default="human")
    doctor_parser.add_argument(
        "--browser-smoke",
        action="store_true",
        help="Open and close a temporary playwright-cli browser session.",
    )

    paths_parser = commands.add_parser("paths", help="Show application data paths.")
    paths_parser.add_argument("--format", choices=OUTPUT_FORMATS, default="human")

    browser_parser = commands.add_parser("browser", help="Manage the Python Playwright browser.")
    browser_commands = browser_parser.add_subparsers(dest="browser_command", required=True)
    install_parser = browser_commands.add_parser(
        "install", help="Install a browser for Python Playwright."
    )
    install_parser.add_argument("browser", choices=("chromium",))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "doctor":
        report = build_doctor_report(browser_smoke=args.browser_smoke)
        print_doctor_report(report, output_format=args.format)
        return 0 if report["ok"] else 1
    if args.command == "paths":
        paths = path_report()
        if args.format == "json":
            print(json.dumps(paths, ensure_ascii=False, sort_keys=True))
        else:
            for name, path in paths.items():
                print(f"{name}: {path}")
        return 0
    if args.command == "browser" and args.browser_command == "install":
        return subprocess.call([sys.executable, "-m", "playwright", "install", args.browser])
    if args.command is None:
        parser.print_help()
    return 0
