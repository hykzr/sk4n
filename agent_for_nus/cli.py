from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path

from nusmods.client import NUSModsAPIError, NUSModsClient

from .academic_calendar import (
    SINGAPORE_TZ,
    academic_calendar_record,
    current_academic_year,
    normalize_academic_year,
)
from .doctor import build_doctor_report, package_version, path_report, print_doctor_report
from .paths import nusmods_data_dir
from .skill_install import (
    AGENTS,
    NUS_SKILLS,
    install_skills,
    parse_selection,
    print_actions,
    print_status_report,
    skill_status_report,
    uninstall_skills,
)

OUTPUT_FORMATS = ("human", "json")
STRUCTURED_OUTPUT_FORMATS = ("json", "jsonl", "plain")


def _iso_date_argument(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO date such as 2026-08-10") from exc


def _academic_year_argument(value: str) -> str:
    try:
        return normalize_academic_year(value)[0]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _selection_argument(choices: Sequence[str], label: str):
    def parse(value: str) -> tuple[str, ...]:
        try:
            return parse_selection(value, choices=choices, label=label)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    return parse


def _add_agent_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--agents",
        type=_selection_argument(AGENTS, "agents"),
        default=AGENTS,
        metavar="codex,copilot,claude,all",
        help="Target agents as a comma-separated list. Default: all.",
    )


def _add_skill_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--skills",
        type=_selection_argument(NUS_SKILLS, "skills"),
        default=NUS_SKILLS,
        metavar="nus-canvas,nusmods,nus-talent-connect,all",
        help="Bundled skills as a comma-separated list. Default: all.",
    )


def _add_project_root_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Project root override. Default: discover the containing Git root.",
    )


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

    calendar_parser = commands.add_parser(
        "calendar",
        help="Show the shared NUS semester and instructional week for a date.",
    )
    calendar_parser.add_argument(
        "--date",
        type=_iso_date_argument,
        default=None,
        metavar="YYYY-MM-DD",
        help="Date in Singapore time. Default: today.",
    )
    calendar_parser.add_argument(
        "--academic-year",
        type=_academic_year_argument,
        default=None,
        metavar="YYYY/YYYY",
        help="Academic year. Default: the year containing the requested date.",
    )
    refresh_group = calendar_parser.add_mutually_exclusive_group()
    refresh_group.add_argument(
        "--refresh",
        action="store_const",
        const="force",
        dest="refresh_mode",
        help="Bypass the cached public NUSMods calendar data.",
    )
    refresh_group.add_argument(
        "--no-refresh",
        action="store_const",
        const="none",
        dest="refresh_mode",
        help="Use only cached public NUSMods calendar data.",
    )
    calendar_parser.set_defaults(refresh_mode="default")
    calendar_parser.add_argument(
        "--format",
        choices=STRUCTURED_OUTPUT_FORMATS,
        default=None,
        help="Output format. Default: human-friendly details.",
    )

    browser_parser = commands.add_parser("browser", help="Manage the Python Playwright browser.")
    browser_commands = browser_parser.add_subparsers(dest="browser_command", required=True)
    install_parser = browser_commands.add_parser(
        "install", help="Install a browser for Python Playwright."
    )
    install_parser.add_argument("browser", choices=("chromium",))

    skills_parser = commands.add_parser("skills", help="Install and inspect bundled Agent Skills.")
    skills_commands = skills_parser.add_subparsers(dest="skills_command", required=True)

    skill_install_parser = skills_commands.add_parser(
        "install", help="Install bundled skills into agent discovery roots."
    )
    _add_agent_argument(skill_install_parser)
    _add_skill_argument(skill_install_parser)
    skill_install_parser.add_argument("--scope", choices=("user", "project"), default="user")
    _add_project_root_argument(skill_install_parser)
    skill_install_parser.add_argument(
        "--dry-run", action="store_true", help="Print exact actions without changing files."
    )
    skill_install_parser.add_argument(
        "--force",
        action="store_true",
        help="Take over an unmanaged same-name skill directory while preserving unrelated files.",
    )

    skill_status_parser = skills_commands.add_parser(
        "status", help="Report bundled and installed skill state."
    )
    _add_agent_argument(skill_status_parser)
    skill_status_parser.add_argument("--scope", choices=("user", "project", "all"), default="user")
    _add_project_root_argument(skill_status_parser)
    skill_status_parser.add_argument("--format", choices=OUTPUT_FORMATS, default="human")

    skill_uninstall_parser = skills_commands.add_parser(
        "uninstall", help="Remove only files managed by agent-for-nus."
    )
    _add_agent_argument(skill_uninstall_parser)
    _add_skill_argument(skill_uninstall_parser)
    skill_uninstall_parser.add_argument("--scope", choices=("user", "project"), default="user")
    _add_project_root_argument(skill_uninstall_parser)
    skill_uninstall_parser.add_argument(
        "--dry-run", action="store_true", help="Print exact actions without changing files."
    )
    return parser


def _print_calendar_result(result: dict[str, object], output_format: str | None) -> None:
    if output_format == "json":
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return
    if output_format == "jsonl":
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return
    if output_format == "plain":
        for key in sorted(result):
            value = result[key]
            if isinstance(value, (dict, list)):
                rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
            elif value is None:
                rendered = "null"
            elif isinstance(value, bool):
                rendered = str(value).lower()
            else:
                rendered = str(value)
            print(f"{key}: {rendered}")
        return
    print("NUS academic calendar")
    for key, value in result.items():
        if value not in (None, "", [], {}):
            print(f"{key.replace('_', ' ').title()}: {value}")


def _handle_calendar(args: argparse.Namespace) -> int:
    target = args.date or datetime.now(SINGAPORE_TZ).date()
    academic_year = args.academic_year or current_academic_year(target)
    client = NUSModsClient(
        academic_year=academic_year,
        data_dir=nusmods_data_dir(),
        refresh=args.refresh_mode == "force",
        cache_only=args.refresh_mode == "none",
    )
    warnings: list[str] = []
    try:
        calendar = client.get_academic_calendar()
    except NUSModsAPIError as exc:
        calendar = {}
        warnings.append(f"Academic calendar unavailable; using computed semester starts: {exc}")
    try:
        holidays = client.get_holidays()
    except NUSModsAPIError as exc:
        holidays = []
        warnings.append(f"Public-holiday data unavailable: {exc}")
    result = academic_calendar_record(target, academic_year, calendar, holidays)
    result["source"] = "NUSMods academic calendar" if calendar else "computed fallback"
    result["warnings"] = warnings
    _print_calendar_result(result, args.format)
    if not args.format:
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
    return 0


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
    if args.command == "calendar":
        try:
            return _handle_calendar(args)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "browser" and args.browser_command == "install":
        return subprocess.call([sys.executable, "-m", "playwright", "install", args.browser])
    if args.command == "skills":
        try:
            if args.skills_command == "install":
                actions = install_skills(
                    agents=args.agents,
                    skills=args.skills,
                    scope=args.scope,
                    project_root=args.project_root,
                    dry_run=args.dry_run,
                    force=args.force,
                )
                print_actions(actions)
                failed = any(action["action"] == "error" for action in actions)
                changed = any(action["changed"] for action in actions)
                if changed:
                    print(
                        "Skills installed. Restart the agent if it does not detect them immediately."
                    )
                return 1 if failed else 0
            if args.skills_command == "status":
                report = skill_status_report(
                    agents=args.agents,
                    scope=args.scope,
                    project_root=args.project_root,
                )
                print_status_report(report, output_format=args.format)
                return 0
            if args.skills_command == "uninstall":
                actions = uninstall_skills(
                    agents=args.agents,
                    skills=args.skills,
                    scope=args.scope,
                    project_root=args.project_root,
                    dry_run=args.dry_run,
                )
                print_actions(actions)
                failed = any(action["action"] == "error" for action in actions)
                changed = any(action["changed"] for action in actions)
                if changed:
                    print("Managed skill files removed. Restart the agent if needed.")
                return 1 if failed else 0
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command is None:
        parser.print_help()
    return 0
