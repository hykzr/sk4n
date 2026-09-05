from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from sk4n.paths import nusmods_data_dir

from .client import (
    DEFAULT_API_BASE_URL,
    DEFAULT_CACHE_TTL_SECONDS,
    NUSModsAPIError,
    NUSModsClient,
    normalize_academic_year,
)
from .schedule import (
    SEMESTER_NAMES,
    SINGAPORE_TZ,
    ScheduleStore,
    apply_selection_edits,
    available_slots,
    current_academic_year,
    current_semester,
    decimal_credits,
    export_share_url,
    format_weeks,
    import_share_url,
    new_course_record,
    parse_semester,
    resolve_course_lessons,
    schedule_for_date,
    semester_data,
    semester_for_date,
    student_to_ta,
    ta_to_student,
)

console = Console()
error_console = Console(stderr=True)

OUTPUT_FORMATS = ("json", "jsonl", "plain")
ATTRIBUTE_DESCRIPTIONS = {
    "year": "Year long course",
    "su": "Has S/U option for Undergraduate students only",
    "grsu": "Has S/U option for relevant Graduate (Research) students only",
    "ssgf": "SkillsFuture funded",
    "sfs": "SkillsFuture series",
    "lab": "Lab based course",
    "ism": "Independent study course",
    "urop": "Undergraduate Research Opportunities Program",
    "fyp": "Honours / Final Year Project",
    "mpes1": "Included in Semester 1's Course Planning Exercise",
    "mpes2": "Included in Semester 2's Course Planning Exercise",
}


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return parsed


def academic_year_argument(value: str) -> str:
    try:
        return normalize_academic_year(value)[0]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def semester_argument(value: str) -> int:
    try:
        return parse_semester(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def regular_semester_argument(value: str) -> int:
    semester = semester_argument(value)
    if semester not in {1, 2}:
        raise argparse.ArgumentTypeError("schedule commands support only s1 or s2")
    return semester


def iso_date_argument(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO date such as 2026-08-10") from exc


def level_argument(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be 1-9 or a level such as 1000") from exc
    if 1 <= number <= 9:
        return number
    if number % 1000 == 0 and 1000 <= number <= 9000:
        return number // 1000
    raise argparse.ArgumentTypeError("must be 1-9 or a level such as 1000")


def decimal_argument(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


def add_format_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        choices=OUTPUT_FORMATS,
        default=None,
        help="Structured output format. Default: human-friendly tables.",
    )


def add_cache_policy_arguments(
    parser: argparse.ArgumentParser,
    *,
    preserve_parent_default: bool = False,
) -> None:
    default: str = argparse.SUPPRESS if preserve_parent_default else "default"
    cache_policy = parser.add_mutually_exclusive_group()
    cache_policy.add_argument(
        "--refresh",
        action="store_const",
        const="refresh",
        default=default,
        dest="cache_policy",
        help="Bypass cached reads, fetch every required resource, and update its cache.",
    )
    cache_policy.add_argument(
        "--no-refresh",
        action="store_const",
        const="cache-only",
        default=default,
        dest="cache_policy",
        help="Use cached resources of any age and never make a network request.",
    )


def add_schedule_semester_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sem",
        type=regular_semester_argument,
        default=None,
        metavar="s1|s2",
        help="Semester. Default: the current NUSMods semester.",
    )


def build_parser() -> argparse.ArgumentParser:
    default_data_path = nusmods_data_dir()
    parser = argparse.ArgumentParser(
        prog="nusmods",
        description="Search public NUSMods course data and manage a local timetable.",
    )
    parser.add_argument(
        "--data-path",
        type=Path,
        default=default_data_path,
        help=f"Schedule and API cache directory. Default: {default_data_path}",
    )
    parser.add_argument(
        "--academic-year",
        type=academic_year_argument,
        default=academic_year_argument(os.getenv("NUSMODS_ACADEMIC_YEAR", current_academic_year())),
        metavar="YYYY/YYYY",
        help="Course-data academic year. Default: current NUSMods year.",
    )
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("NUSMODS_API_BASE_URL", DEFAULT_API_BASE_URL),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("NUSMODS_TIMEOUT", "30")),
        help="HTTP timeout in seconds. Default: 30.",
    )
    parser.add_argument(
        "--cache-ttl",
        type=nonnegative_int,
        default=int(os.getenv("NUSMODS_CACHE_TTL", str(DEFAULT_CACHE_TTL_SECONDS))),
        metavar="SECONDS",
        help="API cache lifetime. Default: 86400 (one day).",
    )
    add_cache_policy_arguments(parser)

    commands = parser.add_subparsers(dest="command", required=True)

    search = commands.add_parser(
        "search",
        help="Search courses using the same facets as the NUSMods finder.",
    )
    search.add_argument(
        "query", help="Text matched against code, title, description, and metadata."
    )
    search.add_argument(
        "--sem",
        action="append",
        type=semester_argument,
        dest="semesters",
        metavar="SEM",
        help="Offered semester (s1, s2, st1, or st2). Repeat for OR.",
    )
    search.add_argument(
        "--no-exam",
        action="store_true",
        help="Only courses with no exam in any offered semester.",
    )
    search.add_argument(
        "--no-exam-clash",
        action="append",
        type=semester_argument,
        metavar="SEM",
        help="Exclude exams clashing with the stored timetable in SEM. Repeatable.",
    )
    search.add_argument(
        "--level",
        action="append",
        type=level_argument,
        metavar="LEVEL",
        help="Course level, for example 1 or 1000. Repeat for OR.",
    )
    search.add_argument(
        "--units",
        action="append",
        type=decimal_argument,
        metavar="N",
        help="Exact unit value. Repeat for OR.",
    )
    search.add_argument("--min-units", type=decimal_argument, metavar="N")
    search.add_argument("--max-units", type=decimal_argument, metavar="N")
    search.add_argument(
        "--faculty",
        action="append",
        metavar="NAME",
        help="Faculty name (case-insensitive substring). Repeat for OR.",
    )
    search.add_argument(
        "--department",
        action="append",
        metavar="NAME",
        help="Department name (case-insensitive substring). Repeat for OR.",
    )
    search.add_argument(
        "--grading",
        action="append",
        metavar="BASIS",
        help="Grading basis (case-insensitive substring). Repeat for OR.",
    )
    search.add_argument(
        "--attribute",
        action="append",
        choices=tuple(ATTRIBUTE_DESCRIPTIONS),
        metavar="KEY",
        help=f"Course attribute. Values: {', '.join(ATTRIBUTE_DESCRIPTIONS)}.",
    )
    search.add_argument(
        "--limit",
        type=positive_int,
        default=None,
        help="Maximum matches. Default: no limit.",
    )
    add_cache_policy_arguments(search, preserve_parent_default=True)
    add_format_argument(search)

    course = commands.add_parser("course", help="Show full course details and timetable slots.")
    course.add_argument("course_code")
    course.add_argument(
        "--sem",
        type=semester_argument,
        default=None,
        metavar="SEM",
        help="Show slots for one semester only. Default: every offered semester.",
    )
    course.add_argument(
        "--comments",
        action="store_true",
        help="Include public NUSMods/Disqus course reviews.",
    )
    add_cache_policy_arguments(course, preserve_parent_default=True)
    add_format_argument(course)

    schedule = commands.add_parser("schedule", help="Manage the locally stored timetable.")
    schedule_commands = schedule.add_subparsers(dest="schedule_command")
    add_schedule_semester_argument(schedule)
    add_cache_policy_arguments(schedule, preserve_parent_default=True)
    add_format_argument(schedule)

    schedule_import = schedule_commands.add_parser(
        "import",
        help="Replace one semester with a NUSMods share URL.",
    )
    schedule_import.add_argument("url")
    add_cache_policy_arguments(schedule_import, preserve_parent_default=True)

    schedule_export = schedule_commands.add_parser(
        "export",
        help="Print the current semester as a NUSMods share URL.",
    )
    add_schedule_semester_argument(schedule_export)

    schedule_add = schedule_commands.add_parser("add", help="Add a course to the timetable.")
    schedule_add.add_argument("course_code")
    schedule_add.add_argument(
        "--ta",
        action="store_true",
        help="Add as a TA course with independently selectable lesson slots.",
    )
    add_schedule_semester_argument(schedule_add)
    add_cache_policy_arguments(schedule_add, preserve_parent_default=True)

    schedule_today = schedule_commands.add_parser(
        "today",
        help="Show lessons to attend today or on a supplied date.",
    )
    schedule_today.add_argument(
        "--date",
        type=iso_date_argument,
        default=None,
        help="ISO date. Default: today in Singapore.",
    )
    add_cache_policy_arguments(schedule_today, preserve_parent_default=True)
    add_format_argument(schedule_today)

    schedule_edit = schedule_commands.add_parser(
        "edit",
        help="Change a course's semester, role, visibility, or selected slots.",
        epilog=(
            "Selectors use TYPE=value. Examples: --set TUT=03; "
            "--ta --set LAB=@1,@3; --set LEC=all; --set TUT=none. "
            "Run edit with no mutations (or --list-slots) to see @N selectors."
        ),
    )
    schedule_edit.add_argument("course_code")
    add_schedule_semester_argument(schedule_edit)
    schedule_edit.add_argument(
        "--move-to",
        type=regular_semester_argument,
        metavar="s1|s2",
        help="Move the course to another semester and select its first available groups.",
    )
    role = schedule_edit.add_mutually_exclusive_group()
    role.add_argument(
        "--ta",
        action="store_true",
        dest="is_ta",
        help="Enable TA mode; current class groups become individual slots.",
    )
    role.add_argument(
        "--student",
        action="store_false",
        dest="is_ta",
        help="Disable TA mode; keep the closest selected class group per type.",
    )
    visibility = schedule_edit.add_mutually_exclusive_group()
    visibility.add_argument("--hidden", action="store_true", dest="hidden")
    visibility.add_argument("--visible", action="store_false", dest="hidden")
    schedule_edit.set_defaults(is_ta=None, hidden=None)
    schedule_edit.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="TYPE=SELECTORS",
        help="Replace a type's selection. Repeatable.",
    )
    schedule_edit.add_argument(
        "--add-slot",
        action="append",
        default=[],
        metavar="TYPE=SELECTORS",
        help="Add TA slots without replacing existing selections. Repeatable.",
    )
    schedule_edit.add_argument(
        "--remove-slot",
        action="append",
        default=[],
        metavar="TYPE=SELECTORS",
        help="Remove TA slots. Repeatable.",
    )
    schedule_edit.add_argument(
        "--clear",
        action="append",
        default=[],
        metavar="TYPE",
        help="Select zero slots for a TA lesson type. Repeatable.",
    )
    schedule_edit.add_argument(
        "--list-slots",
        action="store_true",
        help="Print every @N selector after applying changes.",
    )
    add_cache_policy_arguments(schedule_edit, preserve_parent_default=True)
    add_format_argument(schedule_edit)

    schedule_delete = schedule_commands.add_parser(
        "delete",
        help="Delete a course from a semester.",
    )
    schedule_delete.add_argument("course_code")
    add_schedule_semester_argument(schedule_delete)

    schedule_status = schedule_commands.add_parser(
        "status",
        help="Show today's date, current semester, courses, and non-TA units.",
    )
    add_cache_policy_arguments(schedule_status, preserve_parent_default=True)
    add_format_argument(schedule_status)
    return parser


def _client(args: argparse.Namespace, *, academic_year: str | None = None) -> NUSModsClient:
    return NUSModsClient(
        academic_year=academic_year or args.academic_year,
        base_url=args.api_base_url,
        data_dir=args.data_path,
        timeout=args.timeout,
        cache_ttl_seconds=args.cache_ttl,
        refresh=args.cache_policy == "refresh",
        cache_only=args.cache_policy == "cache-only",
    )


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def print_formatted(value: Any, output_format: str) -> None:
    if output_format == "json":
        print(_json_text(value))
        return
    records = value if isinstance(value, list) else [value]
    if output_format == "jsonl":
        for record in records:
            print(_json_text(record))
        return
    for record_index, record in enumerate(records):
        if record_index:
            print("-" * 40)
        if isinstance(record, Mapping):
            for key, item in record.items():
                text = _json_text(item) if isinstance(item, (dict, list)) else str(item)
                print(f"{key}: {text}")
        else:
            print(record)


def _module_level(module_code: str) -> int | None:
    match = re.search(r"\d", module_code)
    return int(match.group()) if match else None


def _contains_any(value: Any, candidates: Sequence[str] | None) -> bool:
    if not candidates:
        return True
    folded = str(value or "").casefold()
    return any(candidate.casefold() in folded for candidate in candidates)


def _exam_dates(module: Mapping[str, Any], semester: int | None = None) -> set[str]:
    dates: set[str] = set()
    for item in module.get("semesterData") or []:
        if not isinstance(item, Mapping):
            continue
        if semester is not None and int(item.get("semester") or 0) != semester:
            continue
        if item.get("examDate"):
            dates.add(str(item["examDate"]))
    return dates


def module_matches_filters(
    module: Mapping[str, Any],
    args: argparse.Namespace,
    *,
    clash_dates: Mapping[int, set[str]] | None = None,
) -> bool:
    offered = {
        int(item.get("semester") or 0)
        for item in module.get("semesterData") or []
        if isinstance(item, Mapping)
    }
    if args.semesters and not offered.intersection(args.semesters):
        return False
    if args.no_exam and _exam_dates(module):
        return False
    for semester in args.no_exam_clash or []:
        if _exam_dates(module, semester).intersection((clash_dates or {}).get(semester, set())):
            return False
    if args.level and _module_level(str(module.get("moduleCode") or "")) not in args.level:
        return False
    units = decimal_credits(module.get("moduleCredit"))
    if args.units and units not in args.units:
        return False
    if args.min_units is not None and units < args.min_units:
        return False
    if args.max_units is not None and units > args.max_units:
        return False
    if not _contains_any(module.get("faculty"), args.faculty):
        return False
    if not _contains_any(module.get("department"), args.department):
        return False
    if not _contains_any(module.get("gradingBasisDescription"), args.grading):
        return False
    attributes = module.get("attributes")
    active_attributes = attributes if isinstance(attributes, Mapping) else {}
    return not args.attribute or any(active_attributes.get(key) for key in args.attribute)


def _query_score(module: Mapping[str, Any], query: str) -> tuple[int, int, str] | None:
    normalized = " ".join(query.casefold().split())
    code = str(module.get("moduleCode") or "")
    code_folded = code.casefold()
    title = str(module.get("title") or "")
    title_folded = title.casefold()
    searchable = " ".join(
        str(module.get(key) or "") for key in ("moduleCode", "title", "description")
    )
    searchable = searchable.casefold()
    tokens = normalized.split()
    if not all(token in searchable for token in tokens):
        return None
    if code_folded == normalized:
        rank = 0
    elif code_folded.startswith(normalized):
        rank = 1
    elif normalized in code_folded:
        rank = 2
    elif title_folded == normalized:
        rank = 3
    elif title_folded.startswith(normalized):
        rank = 4
    elif normalized in title_folded:
        rank = 5
    else:
        rank = 6
    return rank, len(title), code


def _schedule_clash_dates(
    modules: Sequence[Mapping[str, Any]],
    state: Mapping[str, Any],
    semesters: Sequence[int],
) -> dict[int, set[str]]:
    by_code = {str(module.get("moduleCode")): module for module in modules}
    result: dict[int, set[str]] = {}
    for semester in semesters:
        courses = (
            state.get("semesters", {}).get(str(semester), {}).get("courses", {})
            if isinstance(state.get("semesters"), Mapping)
            else {}
        )
        dates: set[str] = set()
        if isinstance(courses, Mapping):
            for code in courses:
                module = by_code.get(str(code))
                if module:
                    dates.update(_exam_dates(module, semester))
        result[semester] = dates
    return result


def handle_search(args: argparse.Namespace) -> int:
    client = _client(args)
    modules = client.list_modules()
    clash_dates: dict[int, set[str]] = {}
    if args.no_exam_clash:
        state = ScheduleStore(args.data_path).load(academic_year=args.academic_year)
        clash_dates = _schedule_clash_dates(modules, state, args.no_exam_clash)
    ranked: list[tuple[tuple[int, int, str], dict[str, Any]]] = []
    for module in modules:
        score = _query_score(module, args.query)
        if score is None or not module_matches_filters(module, args, clash_dates=clash_dates):
            continue
        ranked.append((score, module))
    sorted_matches = [module for _, module in sorted(ranked, key=lambda item: item[0])]
    matches = sorted_matches[: args.limit] if args.limit is not None else sorted_matches
    if args.format:
        print_formatted(matches, args.format)
        return 0

    table = Table(title=f"NUSMods course search — AY{client.academic_year}")
    table.add_column("Course", style="cyan", no_wrap=True)
    table.add_column("Title")
    table.add_column("Units", justify="right")
    table.add_column("Offered")
    table.add_column("Department")
    for module in matches:
        semesters = [
            SEMESTER_NAMES.get(int(item.get("semester") or 0), "?")
            for item in module.get("semesterData") or []
            if isinstance(item, Mapping)
        ]
        table.add_row(
            str(module.get("moduleCode") or ""),
            str(module.get("title") or ""),
            str(module.get("moduleCredit") or ""),
            ", ".join(semesters),
            str(module.get("department") or ""),
        )
    console.print(table)
    suffix = f", limited to {args.limit}" if args.limit is not None else ""
    console.print(f"{len(matches)} result(s){suffix}.")
    return 0


def _render_workload(workload: Any) -> str:
    if not isinstance(workload, list) or len(workload) != 5:
        return str(workload or "Not available")
    labels = ("Lecture", "Tutorial", "Laboratory", "Project", "Preparation")
    return ", ".join(f"{label} {value}h" for label, value in zip(labels, workload, strict=True))


def _print_module_slots(module: Mapping[str, Any], semester_filter: int | None = None) -> None:
    for data in module.get("semesterData") or []:
        if not isinstance(data, Mapping):
            continue
        semester = int(data.get("semester") or 0)
        if semester_filter is not None and semester != semester_filter:
            continue
        table = Table(title=f"{SEMESTER_NAMES.get(semester, f'Semester {semester}')} slots")
        table.add_column("Type")
        table.add_column("Class")
        table.add_column("Day")
        table.add_column("Time")
        table.add_column("Venue")
        table.add_column("Weeks")
        table.add_column("Size", justify="right")
        raw_timetable = data.get("timetable")
        timetable: list[Any] = raw_timetable if isinstance(raw_timetable, list) else []
        if data.get("examDate"):
            duration = (
                f", {data.get('examDuration')} min" if data.get("examDuration") is not None else ""
            )
            table.caption = f"Exam: {data.get('examDate')}{duration}"
        for lesson in timetable:
            if not isinstance(lesson, Mapping):
                continue
            table.add_row(
                str(lesson.get("lessonType") or ""),
                str(lesson.get("classNo") or ""),
                str(lesson.get("day") or ""),
                f"{lesson.get('startTime', '')}-{lesson.get('endTime', '')}",
                str(lesson.get("venue") or ""),
                format_weeks(lesson.get("weeks")),
                str(lesson.get("size") or ""),
            )
        if timetable:
            console.print(table)
        else:
            suffix = f" {table.caption}." if table.caption else ""
            console.print(f"{table.title}: no timetabled slots.{suffix}")


def handle_course(args: argparse.Namespace) -> int:
    client = _client(args)
    module = client.get_module(args.course_code)
    result = dict(module)
    if args.comments:
        result["comments"] = client.get_comments(
            str(module.get("moduleCode")),
            str(module.get("title") or ""),
        )
    if args.sem is not None:
        result["semesterData"] = [
            item
            for item in result.get("semesterData") or []
            if isinstance(item, Mapping) and int(item.get("semester") or 0) == args.sem
        ]
    if args.format:
        print_formatted(result, args.format)
        return 0

    module_codes = [str(module.get("moduleCode") or "")]
    module_codes.extend(str(item) for item in module.get("aliases") or [])
    code = escape("/".join(module_codes))
    title = escape(str(module.get("title") or ""))
    console.print(f"[bold cyan]{code}[/bold cyan] [bold]{title}[/bold]")
    console.print(
        " • ".join(
            (
                escape(str(module.get("department") or "")),
                escape(str(module.get("faculty") or "")),
                f"{module.get('moduleCredit', '?')} Units",
                escape(str(module.get("gradingBasisDescription") or "Grading unavailable")),
            )
        )
    )
    if module.get("description"):
        console.print(f"\n{escape(str(module['description']))}")
    detail_fields = (
        ("Prerequisite", "prerequisite"),
        ("Prerequisite advisory", "prerequisiteAdvisory"),
        ("Corequisite", "corequisite"),
        ("Preclusion", "preclusion"),
        ("Additional information", "additionalInformation"),
        ("Fulfils requirements", "fulfillRequirements"),
        ("Prerequisite tree", "prereqTree"),
        ("Workload", "workload"),
    )
    detail_table = Table(show_header=False, box=None, pad_edge=False)
    detail_table.add_column(style="bold")
    detail_table.add_column()
    for label, field in detail_fields:
        value = module.get(field)
        if value:
            if field == "fulfillRequirements" and isinstance(value, list):
                rendered = ", ".join(str(item) for item in value)
            elif field == "prereqTree":
                rendered = _json_text(value)
            else:
                rendered = _render_workload(value) if field == "workload" else str(value)
            detail_table.add_row(
                label,
                escape(rendered),
            )
    attributes = module.get("attributes")
    if isinstance(attributes, Mapping):
        labels = [
            ATTRIBUTE_DESCRIPTIONS.get(str(key), str(key))
            for key, enabled in attributes.items()
            if enabled
        ]
        if labels:
            detail_table.add_row("Attributes", escape(", ".join(labels)))
    if detail_table.row_count:
        console.print(detail_table)
    _print_module_slots(module, args.sem)
    if args.comments:
        comment_data = result["comments"]
        console.print(
            f"\n[bold]Reviews[/bold] — {comment_data['returned']} returned"
            f" of {comment_data['count']}"
        )
        for comment in comment_data["comments"]:
            console.print(
                f"\n[cyan]{escape(str(comment.get('author') or 'Anonymous'))}[/cyan] "
                f"[dim]{escape(str(comment.get('createdAt') or ''))} • "
                f"{comment.get('likes', 0)} like(s)[/dim]"
            )
            console.print(escape(str(comment.get("message") or "")))
        if comment_data["hasMore"]:
            console.print(
                "[yellow]Disqus has additional paginated reviews not returned here.[/yellow]"
            )
    return 0


def _load_schedule(args: argparse.Namespace) -> tuple[ScheduleStore, dict[str, Any]]:
    store = ScheduleStore(args.data_path)
    return store, store.load(academic_year=args.academic_year)


def _schedule_semester(args: argparse.Namespace) -> int:
    return args.sem if args.sem is not None else current_semester()


def _semester_courses(state: dict[str, Any], semester: int) -> dict[str, Any]:
    semesters = state.setdefault("semesters", {})
    semester_state = semesters.setdefault(str(semester), {"courses": {}})
    courses = semester_state.setdefault("courses", {})
    if not isinstance(courses, dict):
        raise ValueError("Stored semester courses are invalid.")
    return courses


def _schedule_client(args: argparse.Namespace, state: Mapping[str, Any]) -> NUSModsClient:
    return _client(args, academic_year=str(state.get("academicYear") or args.academic_year))


def _get_schedule_modules(
    client: NUSModsClient,
    courses: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    modules: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for code in courses:
        try:
            modules[str(code)] = client.get_module(str(code))
        except Exception as exc:
            warnings.append(f"{code}: {exc}")
    return modules, warnings


def _course_schedule_record(
    code: str,
    module: Mapping[str, Any],
    course: Mapping[str, Any],
    semester: int,
) -> dict[str, Any]:
    return {
        "moduleCode": code,
        "title": module.get("title"),
        "moduleCredit": module.get("moduleCredit"),
        "isTa": bool(course.get("isTa")),
        "hidden": bool(course.get("hidden")),
        "selections": course.get("selections", {}),
        "lessons": resolve_course_lessons(module, semester, course),
    }


def _print_schedule(
    state: Mapping[str, Any],
    semester: int,
    modules: Mapping[str, Mapping[str, Any]],
) -> None:
    courses = state.get("semesters", {}).get(str(semester), {}).get("courses", {})
    table = Table(title=f"AY{state.get('academicYear')} {SEMESTER_NAMES[semester]} schedule")
    table.add_column("Course", style="cyan")
    table.add_column("Role")
    table.add_column("Type")
    table.add_column("Class")
    table.add_column("Day")
    table.add_column("Time")
    table.add_column("Venue")
    table.add_column("Weeks")
    if isinstance(courses, Mapping):
        for code, course in sorted(courses.items()):
            if not isinstance(course, Mapping):
                continue
            module = modules.get(str(code))
            role = "TA" if course.get("isTa") else "Student"
            if course.get("hidden"):
                role += " (hidden)"
            lessons = resolve_course_lessons(module, semester, course) if module else []
            if not lessons:
                table.add_row(str(code), role, "—", "—", "—", "—", "—", "No slots")
                continue
            for index, lesson in enumerate(lessons):
                table.add_row(
                    str(code) if index == 0 else "",
                    role if index == 0 else "",
                    str(lesson.get("lessonType") or ""),
                    str(lesson.get("classNo") or ""),
                    str(lesson.get("day") or ""),
                    f"{lesson.get('startTime', '')}-{lesson.get('endTime', '')}",
                    str(lesson.get("venue") or ""),
                    format_weeks(lesson.get("weeks")),
                )
    console.print(table)


def handle_schedule_list(args: argparse.Namespace) -> int:
    _, state = _load_schedule(args)
    semester = _schedule_semester(args)
    courses = _semester_courses(state, semester)
    client = _schedule_client(args, state)
    modules, warnings = _get_schedule_modules(client, courses)
    records = [
        _course_schedule_record(str(code), modules[str(code)], course, semester)
        for code, course in sorted(courses.items())
        if isinstance(course, Mapping) and str(code) in modules
    ]
    result = {
        "academicYear": state.get("academicYear"),
        "semester": semester,
        "semesterName": SEMESTER_NAMES[semester],
        "courses": records,
        "warnings": warnings,
    }
    if args.format:
        print_formatted(result, args.format)
    else:
        _print_schedule(state, semester, modules)
        for warning in warnings:
            error_console.print(f"[yellow]Warning: {escape(warning)}[/yellow]")
    return 0


def handle_schedule_import(args: argparse.Namespace) -> int:
    store, state = _load_schedule(args)
    client = _client(args)
    updated, semester, warnings = import_share_url(args.url, client=client, state=state)
    if warnings and args.cache_policy == "cache-only":
        joined = " ".join(warnings)
        raise ValueError(
            f"Cache-only import was not saved because some courses were unavailable: {joined}"
        )
    path = store.save(updated)
    course_count = len(_semester_courses(updated, semester))
    console.print(
        f"Imported {course_count} course(s) into AY{updated['academicYear']} "
        f"{SEMESTER_NAMES[semester]} at {path}."
    )
    for warning in warnings:
        error_console.print(f"[yellow]Warning: {escape(warning)}[/yellow]")
    return 0


def handle_schedule_export(args: argparse.Namespace) -> int:
    _, state = _load_schedule(args)
    print(export_share_url(state, _schedule_semester(args)))
    return 0


def handle_schedule_add(args: argparse.Namespace) -> int:
    store, state = _load_schedule(args)
    semester = _schedule_semester(args)
    code = args.course_code.strip().upper()
    courses = _semester_courses(state, semester)
    if code in courses:
        raise ValueError(f"{code} is already in {SEMESTER_NAMES[semester]}.")
    client = _schedule_client(args, state)
    module = client.get_module(code)
    if semester_data(module, semester) is None:
        raise ValueError(f"{code} is not offered in {SEMESTER_NAMES[semester]}.")
    courses[code] = new_course_record(module, semester, is_ta=args.ta)
    store.save(state)
    role = "TA" if args.ta else "student"
    console.print(f"Added {code} to {SEMESTER_NAMES[semester]} as a {role} course.")
    return 0


def _edit_has_mutation(args: argparse.Namespace) -> bool:
    return any(
        (
            args.move_to is not None,
            args.is_ta is not None,
            args.hidden is not None,
            bool(args.set),
            bool(args.add_slot),
            bool(args.remove_slot),
            bool(args.clear),
        )
    )


def _print_edit_slots(
    module: Mapping[str, Any],
    semester: int,
    course: Mapping[str, Any],
) -> None:
    selections = course.get("selections")
    selections = selections if isinstance(selections, Mapping) else {}
    is_ta = bool(course.get("isTa"))
    table = Table(
        title=(
            f"{module.get('moduleCode')} slots — {SEMESTER_NAMES[semester]} — "
            f"{'TA' if is_ta else 'student'}"
        )
    )
    table.add_column("Selector", style="cyan")
    table.add_column("Selected")
    table.add_column("Type")
    table.add_column("Class")
    table.add_column("Day")
    table.add_column("Time")
    table.add_column("Venue")
    table.add_column("Weeks")
    for slot in available_slots(module, semester):
        lesson_type = str(slot.get("lessonType"))
        selected_values = selections.get(lesson_type) or []
        selected = (
            slot["lessonId"] in selected_values
            if is_ta
            else str(slot.get("classNo")) in selected_values
        )
        table.add_row(
            str(slot["selector"]),
            "yes" if selected else "",
            lesson_type,
            str(slot.get("classNo") or ""),
            str(slot.get("day") or ""),
            f"{slot.get('startTime', '')}-{slot.get('endTime', '')}",
            str(slot.get("venue") or ""),
            format_weeks(slot.get("weeks")),
        )
    console.print(table)
    if not table.row_count:
        console.print("This course has no timetabled slots in the selected semester.")


def handle_schedule_edit(args: argparse.Namespace) -> int:
    store, state = _load_schedule(args)
    source_semester = _schedule_semester(args)
    code = args.course_code.strip().upper()
    source_courses = _semester_courses(state, source_semester)
    if code not in source_courses:
        raise ValueError(f"{code} is not in {SEMESTER_NAMES[source_semester]}.")
    course = source_courses[code]
    if not isinstance(course, dict):
        raise ValueError(f"Stored data for {code} is invalid.")
    client = _schedule_client(args, state)
    module = client.get_module(code)

    semester = source_semester
    if args.move_to is not None and args.move_to != source_semester:
        if semester_data(module, args.move_to) is None:
            raise ValueError(f"{code} is not offered in {SEMESTER_NAMES[args.move_to]}.")
        destination = _semester_courses(state, args.move_to)
        if code in destination:
            raise ValueError(f"{code} is already in {SEMESTER_NAMES[args.move_to]}.")
        was_hidden = bool(course.get("hidden"))
        source_courses.pop(code)
        semester = args.move_to
        course = new_course_record(module, semester, is_ta=bool(course.get("isTa")))
        course["hidden"] = was_hidden
        destination[code] = course

    if args.is_ta is not None and args.is_ta != bool(course.get("isTa")):
        selections = course.get("selections")
        selections = selections if isinstance(selections, Mapping) else {}
        if args.is_ta:
            course["selections"] = student_to_ta(module, semester, selections)
        else:
            course["selections"] = ta_to_student(module, semester, selections)
        course["isTa"] = args.is_ta
    if args.hidden is not None:
        course["hidden"] = args.hidden
    apply_selection_edits(
        course,
        module,
        semester,
        set_expressions=args.set,
        add_expressions=args.add_slot,
        remove_expressions=args.remove_slot,
        clear_types=args.clear,
    )

    mutated = _edit_has_mutation(args)
    if mutated:
        store.save(state)
    record = _course_schedule_record(code, module, course, semester)
    record["semester"] = semester
    record["availableSlots"] = available_slots(module, semester)
    if args.format:
        print_formatted(record, args.format)
    else:
        if mutated:
            console.print(f"Updated {code} in {SEMESTER_NAMES[semester]}.")
        if args.list_slots or not mutated:
            _print_edit_slots(module, semester, course)
        else:
            selected = resolve_course_lessons(module, semester, course)
            console.print(f"{len(selected)} selected lesson slot(s).")
    return 0


def handle_schedule_delete(args: argparse.Namespace) -> int:
    store, state = _load_schedule(args)
    semester = _schedule_semester(args)
    code = args.course_code.strip().upper()
    courses = _semester_courses(state, semester)
    if code not in courses:
        raise ValueError(f"{code} is not in {SEMESTER_NAMES[semester]}.")
    courses.pop(code)
    store.save(state)
    console.print(f"Deleted {code} from {SEMESTER_NAMES[semester]}.")
    return 0


def _calendar_and_holidays(client: NUSModsClient) -> tuple[Mapping[str, Any], list[str]]:
    try:
        calendar = client.get_academic_calendar()
    except NUSModsAPIError:
        calendar = {}
    try:
        holidays = client.get_holidays()
    except NUSModsAPIError:
        holidays = []
    return calendar, holidays


def handle_schedule_today(args: argparse.Namespace) -> int:
    _, state = _load_schedule(args)
    target = args.date or datetime.now(SINGAPORE_TZ).date()
    client = _schedule_client(args, state)
    calendar, holidays = _calendar_and_holidays(client)
    semester, _ = semester_for_date(target, str(state.get("academicYear")), calendar)
    courses = _semester_courses(state, semester)
    modules, warnings = _get_schedule_modules(client, courses)
    result = schedule_for_date(
        state,
        modules,
        target,
        calendar=calendar,
        holidays=holidays,
    )
    result["warnings"] = warnings
    if args.format:
        print_formatted(result, args.format)
        return 0

    heading = f"{result['weekday']}, {result['date']} — {result['semesterName']}" + (
        f", Week {result['week']}" if result["week"] else ""
    )
    console.print(f"[bold]{heading}[/bold]")
    if result["holiday"]:
        console.print("Public holiday — NUSMods suppresses scheduled lessons.")
    elif not result["events"]:
        suffix = " remaining" if result["remainingOnly"] else ""
        console.print(f"No{suffix} lessons.")
    else:
        table = Table()
        table.add_column("Time")
        table.add_column("Course", style="cyan")
        table.add_column("Role")
        table.add_column("Type")
        table.add_column("Class")
        table.add_column("Venue")
        for event in result["events"]:
            table.add_row(
                f"{event.get('startTime', '')}-{event.get('endTime', '')}",
                str(event.get("moduleCode") or ""),
                "TA" if event.get("isTa") else "Student",
                str(event.get("lessonType") or ""),
                str(event.get("classNo") or ""),
                str(event.get("venue") or ""),
            )
        console.print(table)
    for warning in warnings:
        error_console.print(f"[yellow]Warning: {escape(warning)}[/yellow]")
    return 0


def handle_schedule_status(args: argparse.Namespace) -> int:
    _, state = _load_schedule(args)
    now = datetime.now(SINGAPORE_TZ)
    today = now.date()
    active_year = current_academic_year(today)
    semester = current_semester(today)
    courses = _semester_courses(state, semester) if state.get("academicYear") == active_year else {}
    client = _client(args, academic_year=active_year)
    modules, warnings = _get_schedule_modules(client, courses)
    course_records = []
    total_units = Decimal(0)
    for code, course in sorted(courses.items()):
        if not isinstance(course, Mapping):
            continue
        module = modules.get(str(code))
        units = decimal_credits(module.get("moduleCredit")) if module else Decimal(0)
        if not course.get("isTa"):
            total_units += units
        course_records.append(
            {
                "moduleCode": code,
                "title": module.get("title") if module else None,
                "moduleCredit": str(units),
                "isTa": bool(course.get("isTa")),
            }
        )
    calendar, _ = _calendar_and_holidays(client)
    try:
        _, week = semester_for_date(today, active_year, calendar)
    except ValueError:
        week = 0
    total_text = format(total_units.normalize(), "f") if total_units else "0"
    result = {
        "date": today.isoformat(),
        "weekday": today.strftime("%A"),
        "academicYear": active_year,
        "semester": semester,
        "semesterName": SEMESTER_NAMES[semester],
        "week": week if week >= 1 else None,
        "courses": course_records,
        "nonTaUnits": total_text,
        "storedAcademicYear": state.get("academicYear"),
        "warnings": warnings,
    }
    if args.format:
        print_formatted(result, args.format)
        return 0

    console.print(f"[bold]{result['weekday']}, {result['date']}[/bold]")
    semester_text = f"AY{active_year} {SEMESTER_NAMES[semester]}"
    if result["week"]:
        semester_text += f", Week {result['week']}"
    else:
        semester_text += " (outside instructional weeks)"
    console.print(f"Current semester: {semester_text}")
    if state.get("academicYear") != active_year:
        console.print(
            f"[yellow]Stored timetable is AY{state.get('academicYear')}; "
            f"current-semester totals are empty.[/yellow]"
        )
    course_text = ", ".join(
        f"{item['moduleCode']}{' (TA)' if item['isTa'] else ''}" for item in course_records
    )
    console.print(f"Courses: {course_text or 'None'}")
    console.print(f"Total non-TA units: {total_text}")
    for warning in warnings:
        error_console.print(f"[yellow]Warning: {escape(warning)}[/yellow]")
    return 0


def handle_schedule(args: argparse.Namespace) -> int:
    handlers = {
        None: handle_schedule_list,
        "import": handle_schedule_import,
        "export": handle_schedule_export,
        "add": handle_schedule_add,
        "today": handle_schedule_today,
        "edit": handle_schedule_edit,
        "delete": handle_schedule_delete,
        "status": handle_schedule_status,
    }
    return handlers[args.schedule_command](args)


def run(args: argparse.Namespace) -> int:
    if args.command == "search":
        return handle_search(args)
    if args.command == "course":
        return handle_course(args)
    if args.command == "schedule":
        return handle_schedule(args)
    raise ValueError(f"Unknown command: {args.command}")


def main() -> None:
    args = build_parser().parse_args()
    try:
        status = run(args)
    except (NUSModsAPIError, ValueError) as exc:
        error_console.print(f"[red]Error:[/red] {escape(str(exc))}")
        status = 2
    raise SystemExit(status)


if __name__ == "__main__":
    main()
