from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from .auth import (
    DEFAULT_LOGIN_WAIT_SECONDS,
    DEFAULT_SITE_NAME,
    check_auth_status,
    login,
    logout,
)
from .authenticated_client import AuthenticatedKinobiClient, WORKFLOW_STATUSES
from .client import (
    DEFAULT_API_BASE_URL,
    DEFAULT_APP_BASE_URL,
    DEFAULT_PAGE_SIZE,
    KinobiAPIError,
    KinobiClient,
)
from .storage import (
    DEFAULT_DATABASE_PATH,
    TalentConnectStore,
    UpsertStats,
    job_matches_filters,
    summarize_job,
)

console = Console()
error_console = Console(stderr=True)

FILTER_OPTIONS = {
    "company": "companies",
    "company-type": "company_types",
    "exclude-company-type": "exclude_company_types",
    "employment-type": "employment_types",
    "exclude-employment-type": "exclude_employment_types",
    "work-arrangement": "work_arrangements",
    "country": "country_codes",
    "city": "cities",
    "industry": "industries",
    "role": "roles",
    "work-term": "work_terms",
    "related-work-term": "related_work_terms",
    "programme": "programs",
    "internship-programme": "internship_programmes",
    "application-type": "application_types",
    "hard-skill": "hard_industry_skill_value_ids",
    "soft-skill": "soft_industry_skill_value_ids",
}

LOCAL_FILTER_KEYS = {"is_qualified", "posted_after"}
PERSONAL_FILTER_KEYS = {
    "include_expired_if_applied",
    "is_applied",
    "is_drafted",
    "is_my_jobs",
    "is_qualified",
    "is_recommended",
    "talent_connect_statuses",
}
OUTPUT_FORMATS = ("json", "jsonl", "plain")


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def iso_datetime(value: str) -> str:
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be an ISO-8601 date or datetime (for example 2026-07-01)"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def add_filter_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-q",
        "--query",
        help="Free-text job query.",
    )
    for option, destination in FILTER_OPTIONS.items():
        parser.add_argument(
            f"--{option}",
            action="append",
            dest=destination,
            metavar="VALUE",
            help=f"Kinobi {option.replace('-', ' ')} filter. Repeat for multiple values.",
        )
    applied = parser.add_mutually_exclusive_group()
    applied.add_argument(
        "--applied",
        action="store_true",
        dest="is_applied",
        help="Show jobs the authenticated user has applied to.",
    )
    applied.add_argument(
        "--not-applied",
        action="store_false",
        dest="is_applied",
        help="Show jobs the authenticated user has not applied to.",
    )
    drafted = parser.add_mutually_exclusive_group()
    drafted.add_argument(
        "--drafted",
        action="store_true",
        dest="is_drafted",
        help="Show jobs with a saved application draft.",
    )
    drafted.add_argument(
        "--not-drafted",
        action="store_false",
        dest="is_drafted",
        help="Show jobs without a saved application draft.",
    )
    saved = parser.add_mutually_exclusive_group()
    saved.add_argument(
        "--saved",
        action="store_true",
        dest="is_my_jobs",
        help="Show jobs bookmarked by the authenticated user.",
    )
    saved.add_argument(
        "--not-saved",
        action="store_false",
        dest="is_my_jobs",
        help="Show jobs not bookmarked by the authenticated user.",
    )
    recommended = parser.add_mutually_exclusive_group()
    recommended.add_argument(
        "--recommended",
        action="store_true",
        dest="is_recommended",
        help="Use Kinobi's authenticated recommended-jobs search.",
    )
    recommended.add_argument(
        "--not-recommended",
        action="store_false",
        dest="is_recommended",
        help="Use the regular job search (the web UI's Recommended: No behavior).",
    )
    special_needs = parser.add_mutually_exclusive_group()
    special_needs.add_argument(
        "--special-needs",
        action="store_true",
        dest="is_open_for_special_need",
        help="Show roles open to applications from students with special needs.",
    )
    special_needs.add_argument(
        "--not-special-needs",
        action="store_false",
        dest="is_open_for_special_need",
        help="Show roles not marked open to students with special needs.",
    )
    parser.add_argument(
        "--qualified",
        action="store_true",
        dest="is_qualified",
        help="Show only jobs for which Kinobi marks the authenticated user qualified.",
    )
    parser.add_argument(
        "--status",
        action="append",
        choices=WORKFLOW_STATUSES,
        dest="talent_connect_statuses",
        metavar="STATUS",
        help=(
            "Profile workflow status. Repeat to form a union. "
            f"Values: {', '.join(WORKFLOW_STATUSES)}."
        ),
    )
    parser.add_argument(
        "--posted-after",
        type=iso_datetime,
        help=(
            "Show jobs published after this ISO-8601 date/datetime. "
            "Kinobi does not support this remotely, so the CLI filters all matches."
        ),
    )
    parser.add_argument(
        "--include-expired-if-applied",
        action="store_true",
        help="Include expired jobs when the user has applied.",
    )
    parser.set_defaults(
        is_applied=None,
        is_drafted=None,
        is_my_jobs=None,
        is_recommended=None,
        is_open_for_special_need=None,
    )


def add_no_login_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-login",
        action="store_true",
        help="Use Kinobi's public job endpoint and omit per-user job fields.",
    )


def add_format_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        choices=OUTPUT_FORMATS,
        default=None,
        help="Output format. Default: human-friendly rich output.",
    )


def filters_from_args(args: argparse.Namespace, *, query: str | None = None) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    effective_query = query if query is not None else getattr(args, "query", None)
    if effective_query:
        filters["query"] = effective_query
    for destination in FILTER_OPTIONS.values():
        value = getattr(args, destination, None)
        if value:
            filters[destination] = value
    for destination in (
        "is_applied",
        "is_drafted",
        "is_my_jobs",
        "is_recommended",
        "is_open_for_special_need",
    ):
        value = getattr(args, destination, None)
        if value is not None:
            filters[destination] = value
    if getattr(args, "include_expired_if_applied", False):
        filters["include_expired_if_applied"] = True
    if getattr(args, "is_qualified", False):
        filters["is_qualified"] = True
    if getattr(args, "talent_connect_statuses", None):
        filters["talent_connect_statuses"] = args.talent_connect_statuses
    if getattr(args, "posted_after", None):
        filters["posted_after"] = args.posted_after
    return filters


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="talent-connect",
        description="Fetch and persist NUS TalentConnect jobs from Kinobi.",
    )
    parser.add_argument(
        "--data-path",
        type=Path,
        default=Path(os.getenv("TALENT_CONNECT_DATA_PATH", str(DEFAULT_DATABASE_PATH))),
        help=f"SQLite file or containing directory. Default: {DEFAULT_DATABASE_PATH}",
    )
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("TALENT_CONNECT_API_BASE_URL", DEFAULT_API_BASE_URL),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--app-base-url",
        default=os.getenv("TALENT_CONNECT_APP_BASE_URL", DEFAULT_APP_BASE_URL),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--site-name",
        default=os.getenv("TALENT_CONNECT_SITE_NAME", DEFAULT_SITE_NAME),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("TALENT_CONNECT_TIMEOUT", "30")),
        help="HTTP timeout in seconds. Default: 30.",
    )

    commands = parser.add_subparsers(dest="command", required=True)

    auth_parser = commands.add_parser("auth", help="Manage the saved NUS login.")
    auth_commands = auth_parser.add_subparsers(dest="auth_command", required=True)
    auth_commands.add_parser("status", help="Check the saved login.")
    login_parser = auth_commands.add_parser("login", help="Open a browser for NUS login.")
    login_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore the saved session and perform a fresh login.",
    )
    login_parser.add_argument(
        "--wait-seconds",
        type=positive_int,
        default=DEFAULT_LOGIN_WAIT_SECONDS,
        help=f"Login detection timeout. Default: {DEFAULT_LOGIN_WAIT_SECONDS}.",
    )
    auth_commands.add_parser(
        "logout",
        help="Delete the CLI's saved session without signing other browsers out of NUS SSO.",
    )

    fetch_parser = commands.add_parser(
        "fetch",
        help="Search remote jobs, upsert matches, and print them.",
    )
    add_filter_arguments(fetch_parser)
    add_no_login_argument(fetch_parser)
    fetch_parser.add_argument(
        "--cached",
        action="store_true",
        help="Search only the local job database; do not contact Kinobi.",
    )
    fetch_parser.add_argument(
        "--max-jobs",
        type=positive_int,
        default=None,
        help="Maximum matching jobs. Default: no limit.",
    )
    fetch_parser.add_argument(
        "--page-size",
        type=positive_int,
        default=DEFAULT_PAGE_SIZE,
        help=f"Remote page size. Default: {DEFAULT_PAGE_SIZE}.",
    )
    fetch_parser.add_argument(
        "--no-details",
        action="store_true",
        help="Skip dedicated job-detail requests; persist authenticated list records only.",
    )
    fetch_parser.add_argument(
        "--refresh-details",
        action="store_true",
        help=(
            "Force detail requests for every matched job. By default details refresh "
            "only when Kinobi's updated_at changed."
        ),
    )
    fetch_parser.add_argument(
        "--updated-only",
        action="store_true",
        help="Print only jobs that were new or changed in this remote fetch.",
    )
    add_format_argument(fetch_parser)

    job_parser = commands.add_parser("job", help="Show one job by ID or slug.")
    job_parser.add_argument("job_id", help="Kinobi _id or slug.")
    refresh_group = job_parser.add_mutually_exclusive_group()
    refresh_group.add_argument(
        "--refresh",
        action="store_true",
        dest="refresh",
        help="Refresh from Kinobi (default).",
    )
    refresh_group.add_argument(
        "--no-refresh",
        action="store_false",
        dest="refresh",
        help="Use only the stored record.",
    )
    job_parser.set_defaults(refresh=True)
    add_no_login_argument(job_parser)
    add_format_argument(job_parser)

    company_parser = commands.add_parser(
        "company",
        help="Show a company and its current jobs.",
    )
    company_parser.add_argument("company_id", help="Kinobi _id, company_id, or slug.")
    company_refresh = company_parser.add_mutually_exclusive_group()
    company_refresh.add_argument(
        "--refresh",
        action="store_true",
        dest="refresh",
        help="Refresh the company and its jobs from Kinobi (default).",
    )
    company_refresh.add_argument(
        "--no-refresh",
        action="store_false",
        dest="refresh",
        help="Use only stored company and job records.",
    )
    company_parser.set_defaults(refresh=True)
    add_no_login_argument(company_parser)
    company_parser.add_argument(
        "--max-jobs",
        type=positive_int,
        default=None,
        help="Maximum company jobs. Default: no limit.",
    )
    add_format_argument(company_parser)

    search_parser = commands.add_parser(
        "search",
        help="Search job or company IDs without dedicated detail requests.",
    )
    search_parser.add_argument("resource", choices=("job", "company"))
    search_parser.add_argument("search_query")
    add_filter_arguments(search_parser)
    search_parser.add_argument(
        "--cached",
        action="store_true",
        help="Search only stored records.",
    )
    add_no_login_argument(search_parser)
    search_parser.add_argument(
        "--max-results",
        type=positive_int,
        default=20,
        help="Maximum results. Default: 20.",
    )
    add_format_argument(search_parser)
    return parser


def _client(args: argparse.Namespace) -> KinobiClient:
    return KinobiClient(base_url=args.api_base_url, timeout=args.timeout)


def _job_client(args: argparse.Namespace):
    if getattr(args, "no_login", False):
        return _client(args)
    status = check_auth_status(
        site_name=args.site_name,
        app_base_url=args.app_base_url,
    )
    if not status.authenticated:
        console.print("TalentConnect login is required for per-user job fields.")
        login(
            site_name=args.site_name,
            app_base_url=args.app_base_url,
            login_wait_seconds=DEFAULT_LOGIN_WAIT_SECONDS,
        )
    return AuthenticatedKinobiClient(
        app_base_url=args.app_base_url,
        site_name=args.site_name,
        timeout=args.timeout,
    )


def _validate_login_filters(args: argparse.Namespace, filters: Mapping[str, Any]) -> None:
    authenticated_filters = PERSONAL_FILTER_KEYS & filters.keys()
    if filters.get("is_recommended") is False:
        authenticated_filters -= {"is_recommended"}
    if getattr(args, "no_login", False) and authenticated_filters:
        raise ValueError(
            "Applied, saved, recommended, qualified, and status filters require "
            "authentication; remove --no-login."
        )


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _plain_records(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        return [value]
    return [{"value": value}]


def print_formatted(
    value: Any,
    output_format: str,
    *,
    jsonl_records: Sequence[Mapping[str, Any]] | None = None,
) -> None:
    if output_format == "json":
        print(_json_text(value))
        return
    records = list(jsonl_records) if jsonl_records is not None else _plain_records(value)
    if output_format == "jsonl":
        for record in records:
            print(_json_text(record))
        return
    if output_format == "plain":
        for index, record in enumerate(records):
            if index:
                print("-" * 72)
            for key in sorted(record):
                field_value = record[key]
                if isinstance(field_value, (Mapping, list)):
                    rendered = _json_text(field_value)
                elif field_value is None:
                    rendered = "null"
                elif isinstance(field_value, bool):
                    rendered = str(field_value).lower()
                else:
                    rendered = str(field_value).replace("\n", "\\n")
                print(f"{key}: {rendered}")
        return
    raise ValueError(f"Unknown output format: {output_format}")


def _progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    )


def _progress_callback(progress: Progress, task_id: TaskID):
    def update(completed: int, total: int | None) -> None:
        kwargs: dict[str, Any] = {"completed": completed}
        if total is not None:
            kwargs["total"] = total
        progress.update(task_id, **kwargs)

    return update


def _split_job_filters(
    filters: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    remote = {
        key: value
        for key, value in filters.items()
        if key not in LOCAL_FILTER_KEYS and key != "is_recommended"
    }
    local = {key: value for key, value in filters.items() if key in LOCAL_FILTER_KEYS}
    recommended = filters.get("is_recommended") is True
    return remote, local, recommended


def _list_remote_jobs(
    job_client: Any,
    *,
    filters: Mapping[str, Any],
    max_jobs: int | None,
    page_size: int = DEFAULT_PAGE_SIZE,
    progress_callback: Any = None,
) -> list[dict[str, Any]]:
    workflow_statuses = filters.get("talent_connect_statuses")
    if workflow_statuses:
        if not isinstance(job_client, AuthenticatedKinobiClient):
            raise ValueError("Workflow status filters require authentication.")
        statuses = (
            list(workflow_statuses)
            if isinstance(workflow_statuses, Sequence)
            and not isinstance(workflow_statuses, (str, bytes))
            else [str(workflow_statuses)]
        )
        jobs = job_client.list_workflow_jobs(
            statuses=statuses,
            query=str(filters.get("query") or "") or None,
            page_size=page_size,
            progress_callback=progress_callback,
        )
        local_filters = {
            key: value
            for key, value in filters.items()
            if key != "talent_connect_statuses"
        }
        jobs = [job for job in jobs if job_matches_filters(job, local_filters)]
        return jobs[:max_jobs] if max_jobs is not None else jobs

    remote_filters, local_filters, recommended = _split_job_filters(filters)
    remote_max = None if local_filters else max_jobs
    kwargs: dict[str, Any] = {
        "filters": remote_filters,
        "max_jobs": remote_max,
        "page_size": page_size,
        "progress_callback": progress_callback,
    }
    if isinstance(job_client, AuthenticatedKinobiClient):
        kwargs["recommended"] = recommended
    jobs = job_client.list_jobs(**kwargs)
    if local_filters:
        jobs = [job for job in jobs if job_matches_filters(job, local_filters)]
    return jobs[:max_jobs] if max_jobs is not None else jobs


def _format_date(value: Any) -> str:
    text = str(value or "")
    return text[:10] if len(text) >= 10 else text


def _format_salary(job: Mapping[str, Any]) -> str:
    if job.get("is_disclose_salary") is False:
        return "not disclosed"
    lower = job.get("salary_lower_range")
    upper = job.get("salary_upper_range")
    if lower in (None, "") and upper in (None, ""):
        return ""
    currency = str(job.get("currency") or "")
    rate = str(job.get("salary_rate") or "")
    if isinstance(lower, (int, float)) and isinstance(upper, (int, float)) and lower > upper:
        lower, upper = upper, lower
    if lower == upper or upper in (None, ""):
        amount = str(lower)
    elif lower in (None, ""):
        amount = str(upper)
    else:
        amount = f"{lower}-{upper}"
    return " ".join(part for part in (currency, amount, rate) if part)


def _job_url(job: Mapping[str, Any]) -> str:
    slug = str(job.get("slug") or job.get("_id") or "")
    return f"{DEFAULT_APP_BASE_URL}/jobs/{slug}" if slug else ""


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def print_jobs(jobs: Sequence[Mapping[str, Any]], *, title: str = "Jobs") -> None:
    table = Table(title=f"{title} ({len(jobs)})", show_lines=False, expand=True)
    table.add_column("ID", no_wrap=True, width=24)
    table.add_column("Job", ratio=1, overflow="fold")
    table.add_column("Type / state", no_wrap=True)
    table.add_column("Deadline", no_wrap=True)
    for job in jobs:
        company = _dict_value(job.get("company"))
        title_text = escape(str(job.get("title") or ""))
        company_text = str(company.get("name") or "")
        job_text = (
            f"{title_text}\n[dim]{escape(company_text)}[/dim]" if company_text else title_text
        )
        states = []
        if job.get("user_has_applied") is True:
            states.append("applied")
        if job.get("is_bookmarked") is True:
            states.append("bookmarked")
        if job.get("is_unqualified_student") is True:
            states.append("unqualified")
        workflow_statuses = job.get("talent_connect_statuses")
        if isinstance(workflow_statuses, list):
            states.extend(str(status) for status in workflow_statuses)
        type_and_state = str(job.get("employment_type") or "")
        if states:
            type_and_state = f"{type_and_state}\n[green]{'/'.join(states)}[/green]"
        table.add_row(
            str(job.get("_id") or ""),
            job_text,
            type_and_state,
            _format_date(job.get("expired_at")),
        )
    console.print(table)


def print_job_detail(job: Mapping[str, Any]) -> None:
    company = _dict_value(job.get("company"))
    application = _dict_value(job.get("job_application"))
    offer = _dict_value(job.get("job_offer"))
    console.print(f"[bold]{escape(str(job.get('title') or '(untitled)'))}[/bold]")
    console.print(
        f"{escape(str(company.get('name') or 'Unknown company'))} "
        f"• {escape(str(job.get('employment_type') or 'unknown type'))}"
    )
    fields = [
        ("ID", job.get("_id")),
        ("Slug", job.get("slug")),
        ("Location", ", ".join(filter(None, [job.get("city"), job.get("country_code")]))),
        ("Arrangement", job.get("work_arrangement")),
        ("Experience", job.get("experience_level")),
        ("Role", job.get("role")),
        ("Published", _format_date(job.get("published_at"))),
        ("Deadline", _format_date(job.get("expired_at"))),
        ("Salary", _format_salary(job)),
        ("Application", job.get("application_type")),
        (
            "Applied",
            "yes" if job.get("user_has_applied") is True else "no",
        )
        if "user_has_applied" in job
        else ("Applied", ""),
        (
            "Bookmarked",
            "yes" if job.get("is_bookmarked") is True else "no",
        )
        if "is_bookmarked" in job
        else ("Bookmarked", ""),
        (
            "Qualified",
            "no" if job.get("is_unqualified_student") is True else "yes",
        )
        if "is_unqualified_student" in job
        else ("Qualified", ""),
        ("Application status", application.get("status")),
        ("Application ID", job.get("job_application_id") or application.get("_id")),
        (
            "Workflow status",
            ", ".join(str(value) for value in (job.get("talent_connect_statuses") or [])),
        ),
        ("Offer response", offer.get("response")),
        ("Offer status", offer.get("status")),
        ("Offer ID", offer.get("_id")),
        ("External link", job.get("job_link")),
        ("TalentConnect", _job_url(job)),
    ]
    detail = Table(show_header=False, box=None, pad_edge=False)
    detail.add_column(style="cyan", no_wrap=True)
    detail.add_column(overflow="fold")
    for label, value in fields:
        if value not in (None, "", []):
            detail.add_row(label, escape(str(value)))
    console.print(detail)
    description = str(job.get("description_text") or job.get("description") or "").strip()
    if description:
        console.print("\n[bold]Description[/bold]")
        console.print(description)
    for heading, key in (
        ("Responsibilities", "responsibilities"),
        ("Requirements", "requirements"),
    ):
        values = job.get(key)
        if not isinstance(values, list) or not values:
            continue
        console.print(f"\n[bold]{heading}[/bold]")
        for item in values:
            text = item.get("text") if isinstance(item, dict) else item
            if text:
                console.print(f"• {text}")


def print_companies(
    companies: Sequence[Mapping[str, Any]],
    *,
    title: str = "Companies",
) -> None:
    table = Table(title=f"{title} ({len(companies)})")
    table.add_column("ID", no_wrap=True, max_width=24)
    table.add_column("Company ID", overflow="fold")
    table.add_column("Slug", overflow="fold")
    table.add_column("Name", overflow="fold")
    table.add_column("Country", no_wrap=True)
    for company in companies:
        table.add_row(
            str(company.get("_id") or ""),
            str(company.get("company_id") or ""),
            str(company.get("slug") or ""),
            str(company.get("name") or ""),
            str(company.get("country_code") or ""),
        )
    console.print(table)


def _print_upsert(stats: UpsertStats, path: Path) -> None:
    console.print(
        f"Stored in {path}: {stats.inserted} new, {stats.updated} updated, "
        f"{stats.unchanged} unchanged."
    )


def handle_auth(args: argparse.Namespace) -> int:
    if args.auth_command == "status":
        status = check_auth_status(
            site_name=args.site_name,
            app_base_url=args.app_base_url,
        )
        if status.authenticated:
            identity = status.display_name
            if status.email:
                identity = f"{identity} <{status.email}>" if identity else status.email
            console.print(f"Authenticated{f' as {identity}' if identity else ''}.")
            return 0
        console.print("Not authenticated.")
        if status.error:
            console.print(f"[dim]{escape(status.error)}[/dim]")
        return 1
    if args.auth_command == "login":
        status = login(
            refresh=args.refresh,
            site_name=args.site_name,
            app_base_url=args.app_base_url,
            login_wait_seconds=args.wait_seconds,
        )
        identity = status.display_name or status.email
        console.print(f"Authenticated{f' as {identity}' if identity else ''}.")
        return 0
    if args.auth_command == "logout":
        console.print(logout(site_name=args.site_name))
        return 0
    raise AssertionError(f"Unknown auth command: {args.auth_command}")


def handle_fetch(args: argparse.Namespace, store: TalentConnectStore) -> int:
    filters = filters_from_args(args)
    workflow_fetch = bool(filters.get("talent_connect_statuses"))
    _validate_login_filters(args, filters)
    if args.cached:
        if args.updated_only:
            raise ValueError("--updated-only cannot be combined with --cached.")
        jobs = store.find_jobs(filters=filters, max_jobs=args.max_jobs)
        stats = None
        shown_jobs = jobs
    else:
        job_client = _job_client(args)
        progress = _progress() if args.format is None else None
        if progress:
            progress.start()
        try:
            search_task = (
                progress.add_task("Fetching matching jobs", total=None) if progress else None
            )
            jobs = _list_remote_jobs(
                job_client,
                filters=filters,
                max_jobs=args.max_jobs,
                page_size=args.page_size,
                progress_callback=(
                    _progress_callback(progress, search_task)
                    if progress is not None and search_task is not None
                    else None
                ),
            )
            if progress is not None and search_task is not None:
                progress.update(search_task, completed=len(jobs), total=len(jobs))
            stats, changed_jobs = store.upsert_jobs_with_changes(
                jobs,
                detail_level=2 if workflow_fetch else 1,
            )
            if not args.no_details and not workflow_fetch:
                detail_candidates = []
                for job in jobs:
                    identifier = str(job.get("slug") or job.get("_id") or "")
                    job_id = str(job.get("_id") or "")
                    needs_refresh = store.job_needs_detail_refresh(
                        job_id,
                        str(job.get("updated_at") or ""),
                    )
                    if identifier and (args.refresh_details or needs_refresh):
                        detail_candidates.append(identifier)
                details_task = (
                    progress.add_task(
                        "Refreshing job details",
                        total=len(detail_candidates),
                    )
                    if progress and detail_candidates
                    else None
                )
                details = job_client.get_jobs(
                    detail_candidates,
                    progress_callback=(
                        _progress_callback(progress, details_task)
                        if progress is not None and details_task is not None
                        else None
                    ),
                )
                if details:
                    store.upsert_jobs(details, detail_level=2)
                detail_errors = getattr(job_client, "detail_errors", {})
                if detail_errors:
                    error_console.print(
                        f"[yellow]Warning:[/yellow] {len(detail_errors)} job detail "
                        "request(s) failed; list records were still stored."
                    )
            elif workflow_fetch:
                detail_errors = getattr(job_client, "detail_errors", {})
                if detail_errors:
                    error_console.print(
                        f"[yellow]Warning:[/yellow] {len(detail_errors)} workflow job "
                        "detail request(s) failed; available status records were stored."
                    )
        finally:
            if progress:
                progress.stop()
        if args.updated_only:
            shown_jobs = [store.get_job(str(job.get("_id") or "")) or job for job in changed_jobs]
        else:
            shown_jobs = [store.get_job(str(job.get("_id") or "")) or job for job in jobs]
    if args.format:
        print_formatted(shown_jobs, args.format)
    else:
        title = (
            "Cached jobs"
            if args.cached
            else ("New or updated jobs" if args.updated_only else "Matched jobs")
        )
        print_jobs(shown_jobs, title=title)
        if stats is not None:
            _print_upsert(stats, store.path)
    return 0


def handle_job(args: argparse.Namespace, store: TalentConnectStore) -> int:
    job = store.get_job(args.job_id)
    if args.refresh:
        identifier = str(job.get("slug") or job.get("_id")) if job else args.job_id
        job = _job_client(args).get_job(identifier)
        stats = store.upsert_jobs([job], detail_level=2)
    else:
        stats = None
    if not job:
        raise KinobiAPIError(f"Job {args.job_id!r} is not stored. Remove --no-refresh to fetch it.")
    if args.format:
        print_formatted(job, args.format)
    else:
        print_job_detail(job)
        if stats is not None:
            _print_upsert(stats, store.path)
    return 0


def _company_identifier_type(company: Mapping[str, Any], identifier: str) -> str | None:
    if identifier == str(company.get("_id") or ""):
        return "id"
    if identifier == str(company.get("company_id") or ""):
        return "company_id"
    if identifier == str(company.get("slug") or ""):
        return "slug"
    return None


def handle_company(args: argparse.Namespace, store: TalentConnectStore) -> int:
    company = store.get_company(args.company_id)
    if args.refresh:
        if company:
            identifier_type = _company_identifier_type(company, args.company_id)
            identifier = str(
                company.get(
                    {"id": "_id", "company_id": "company_id", "slug": "slug"}.get(
                        identifier_type or "", "slug"
                    )
                )
                or args.company_id
            )
        else:
            identifier_type = None
            identifier = args.company_id
        company = _client(args).get_company(
            identifier,
            identifier_type=identifier_type,
        )
        store.upsert_companies([company], detail_level=2)
        company_filter = str(company.get("company_id") or company.get("name") or "")
        jobs = _list_remote_jobs(
            _job_client(args),
            filters={"companies": [company_filter]},
            max_jobs=args.max_jobs,
        )
        stats = store.upsert_jobs(jobs, detail_level=1)
    else:
        if not company:
            raise KinobiAPIError(
                f"Company {args.company_id!r} is not stored. Remove --no-refresh to fetch it."
            )
        identifiers = {
            str(company.get("_id") or "").casefold(),
            str(company.get("company_id") or "").casefold(),
            str(company.get("name") or "").casefold(),
            str(company.get("slug") or "").casefold(),
        }
        jobs = [
            job
            for job in store.all_jobs()
            if isinstance(job.get("company"), dict)
            and {
                str(job["company"].get("_id") or "").casefold(),
                str(job["company"].get("company_id") or "").casefold(),
                str(job["company"].get("name") or "").casefold(),
                str(job["company"].get("slug") or "").casefold(),
            }
            & identifiers
        ]
        if args.max_jobs is not None:
            jobs = jobs[: args.max_jobs]
        stats = None

    assert company is not None
    if args.format:
        records = [
            {"record_type": "company", **company},
            *({"record_type": "job", **job} for job in jobs),
        ]
        print_formatted(
            {"company": company, "jobs": jobs},
            args.format,
            jsonl_records=records,
        )
    else:
        console.print(f"[bold]{escape(str(company.get('name') or '(unnamed company)'))}[/bold]")
        company_details = Table(show_header=False, box=None, pad_edge=False)
        for label, value in (
            ("ID", company.get("_id")),
            ("Company ID", company.get("company_id")),
            ("Slug", company.get("slug")),
            ("Industry", company.get("industry")),
            ("Country", company.get("country_code")),
            ("Employees", company.get("number_of_employees")),
            ("Website", company.get("web_url")),
            ("Address", company.get("address")),
        ):
            if value not in (None, "", []):
                company_details.add_row(label, escape(str(value)))
        console.print(company_details)
        description = str(company.get("description") or "").strip()
        if description:
            console.print("\n[bold]Description[/bold]")
            console.print(description)
        print_jobs(jobs, title="Current jobs")
        if stats is not None:
            _print_upsert(stats, store.path)
    return 0


def handle_search(args: argparse.Namespace, store: TalentConnectStore) -> int:
    if args.resource == "company":
        if args.cached:
            results = store.find_companies(
                args.search_query,
                max_companies=args.max_results,
            )
        else:
            results = _client(args).list_companies(
                query=args.search_query,
                max_companies=args.max_results,
            )
            store.upsert_companies(results, detail_level=1)
        if args.format:
            print_formatted(results, args.format)
        else:
            print_companies(results, title="Company search")
        return 0

    filters = filters_from_args(args, query=args.search_query)
    _validate_login_filters(args, filters)
    if args.cached:
        results = store.find_jobs(filters=filters, max_jobs=args.max_results)
    else:
        full_records = _list_remote_jobs(
            _job_client(args),
            filters=filters,
            max_jobs=args.max_results,
        )
        results = [summarize_job(job) for job in full_records]
        store.upsert_jobs(results, detail_level=0)
    if args.format:
        print_formatted(results, args.format)
    else:
        print_jobs(results, title="Job search")
    return 0


def run(args: argparse.Namespace) -> int:
    if args.command == "auth":
        return handle_auth(args)
    with TalentConnectStore(args.data_path) as store:
        if args.command == "fetch":
            return handle_fetch(args, store)
        if args.command == "job":
            return handle_job(args, store)
        if args.command == "company":
            return handle_company(args, store)
        if args.command == "search":
            return handle_search(args, store)
    raise AssertionError(f"Unknown command: {args.command}")


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        raise SystemExit(run(args))
    except (KinobiAPIError, ValueError, TimeoutError) as exc:
        error_console.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
