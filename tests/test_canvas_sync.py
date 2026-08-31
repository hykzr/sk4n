from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

import canvas_sync.cli as canvas_cli
from canvas_sync.assignments import sync_assignments
from canvas_sync.cli import build_parser
from canvas_sync.client import CanvasAPIError, CanvasAuthError, CanvasClient
from canvas_sync.content import content_available
from canvas_sync.fetcher import (
    CanvasFetcher,
    absolutize_local_paths,
    canonical_semester,
    infer_enrollment_academic_year,
    resolve_semester_filter,
)
from canvas_sync.files import collect_referenced_files, sync_files
from canvas_sync.groups import sync_groups
from canvas_sync.models import CourseRecord
from canvas_sync.people import sync_people
from canvas_sync.utils import read_json, write_json


def test_cli_exposes_auth_sync_info_activity_and_api_commands() -> None:
    parser = build_parser()

    assert parser.parse_args(["auth", "status", "--format", "json"]).auth_command == "status"
    sync = parser.parse_args(["sync", "--course", "CG2028", "--refresh-pages", "--skip-files"])
    assert sync.course == [["CG2028"]]
    assert sync.refresh_pages is True
    assert sync.skip_files is True

    listing = parser.parse_args(["list", "-s", "y3s1", "--no-refresh", "--format", "jsonl"])
    assert listing.semester == "y3s1"
    assert listing.refresh_mode == "none"
    assert listing.format == "jsonl"

    course = parser.parse_args(
        [
            "course",
            "CG2028",
            "assignments",
            "123",
            "--semester",
            "non-academic",
            "--refresh",
            "--format",
            "plain",
        ]
    )
    assert (course.resource, course.item) == ("assignments", "123")
    assert course.refresh_mode == "force"
    assert course.semester == "non-academic"

    api = parser.parse_args(
        [
            "api",
            "/api/v1/users/self",
            "-X",
            "post",
            "--data",
            '{"ok": true}',
            "--param",
            "include[]=term",
            "-H",
            "Accept:application/json",
        ]
    )
    assert api.method == "POST"
    assert api.data == {"ok": True}
    assert api.param == [("include[]", "term")]
    assert api.header == [("Accept", "application/json")]

    playwright = parser.parse_args(
        [
            "playwright-cli",
            "--url",
            "https://canvas.example.test/courses/1",
            "--headed",
            "--session",
            "canvas-debug",
        ]
    )
    assert playwright.url == "https://canvas.example.test/courses/1"
    assert playwright.headed is True
    assert playwright.session == "canvas-debug"

    playwright_defaults = parser.parse_args(["playwright-cli"])
    assert playwright_defaults.url is None
    assert playwright_defaults.headed is False
    assert playwright_defaults.session == "canvas"

    events = parser.parse_args(
        [
            "calendar-events",
            "--start",
            "2026-08-10",
            "--end",
            "2026-08-16",
            "--type",
            "assignment",
            "--format",
            "json",
        ]
    )
    assert events.start.isoformat() == "2026-08-10"
    assert events.end.isoformat() == "2026-08-16"
    assert events.event_type == "assignment"
    assert parser.parse_args(["todo", "--format", "jsonl"]).command == "todo"
    assert parser.parse_args(["upcoming", "--format", "plain"]).command == "upcoming"

    home = parser.parse_args(["course", "CG2028", "home", "--no-refresh"])
    assert (home.resource, home.item) == ("home", "list")

    groups = parser.parse_args(["course", "CG2028", "groups", "215659"])
    assert (groups.resource, groups.item) == ("groups", "215659")


def test_playwright_cli_command_requires_explicit_login(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[dict[str, Any]] = []
    monkeypatch.setattr(canvas_cli, "playwright_cli_executable", lambda: "/bin/playwright-cli")
    monkeypatch.setattr(canvas_cli, "ensure_session_available", lambda *_args: None)
    monkeypatch.setattr(
        canvas_cli,
        "check_auth_status",
        lambda **_kwargs: SimpleNamespace(authenticated=False, name="", email=""),
    )
    login_called = False

    def unexpected_login(**_kwargs: Any) -> None:
        nonlocal login_called
        login_called = True

    monkeypatch.setattr(canvas_cli, "login", unexpected_login)
    monkeypatch.setattr(
        canvas_cli,
        "open_authenticated_session",
        lambda **kwargs: opened.append(kwargs),
    )
    args = build_parser().parse_args(["playwright-cli", "--session", "canvas-test"])

    with pytest.raises(CanvasAuthError, match="canvas auth login"):
        canvas_cli.handle_playwright_cli(args)
    assert not login_called
    assert opened == []


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/api/v1/users/self",
        "//example.test/api/v1/users/self",
        "http://canvas.nus.edu.sg/api/v1/users/self",
    ],
)
def test_canvas_client_rejects_cross_origin_authenticated_urls(url: str) -> None:
    client = CanvasClient("https://canvas.nus.edu.sg", "test")

    with pytest.raises(ValueError, match="configured Canvas origin"):
        client.resolve_url(url)


def test_canvas_client_accepts_paths_and_same_origin_absolute_urls() -> None:
    client = CanvasClient("https://canvas.nus.edu.sg", "test")

    assert client.resolve_url("/api/v1/users/self") == (
        "https://canvas.nus.edu.sg/api/v1/users/self"
    )
    assert client.resolve_url("https://canvas.nus.edu.sg/api/v1/courses") == (
        "https://canvas.nus.edu.sg/api/v1/courses"
    )


def test_canvas_client_activity_endpoints_use_paginated_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CanvasClient("https://canvas.nus.edu.sg", "test")
    calls: list[tuple[str, list[tuple[str, Any]] | None]] = []

    def fake_get_paginated(
        path: str,
        params: list[tuple[str, Any]] | None = None,
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        del max_pages
        calls.append((path, params))
        return [{"id": len(calls)}]

    monkeypatch.setattr(client, "get_paginated", fake_get_paginated)

    assert client.calendar_events(
        start=date(2026, 8, 10),
        end=date(2026, 8, 16),
        event_type="assignment",
    ) == [{"id": 1}]
    assert client.todo() == [{"id": 2}]
    assert client.upcoming_events() == [{"id": 3}]
    assert calls[0] == (
        "/api/v1/calendar_events",
        [
            ("type", "assignment"),
            ("include[]", "context"),
            ("per_page", "100"),
            ("start_date", "2026-08-10"),
            ("end_date", "2026-08-16"),
        ],
    )
    assert calls[1][0] == "/api/v1/users/self/todo"
    assert calls[2][0] == "/api/v1/users/self/upcoming_events"


def test_canvas_client_course_groups_include_members_and_current_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CanvasClient("https://canvas.nus.edu.sg", "test")
    calls: list[tuple[str, list[tuple[str, Any]] | None]] = []

    def fake_get_paginated(
        path: str,
        params: list[tuple[str, Any]] | None = None,
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        del max_pages
        calls.append((path, params))
        if path == "/api/v1/courses/93662/groups":
            return [
                {"id": 1, "name": "Group 1", "users": [{"id": 10, "name": "One"}]},
                {"id": 2, "name": "Group 2", "users": [{"id": 20, "name": "Two"}]},
            ]
        return [
            {"id": 2, "course_id": 93662, "name": "Group 2"},
            {"id": 3, "course_id": 12345, "name": "Another course"},
        ]

    monkeypatch.setattr(client, "get_paginated", fake_get_paginated)

    groups = client.course_groups("93662")

    assert calls == [
        (
            "/api/v1/courses/93662/groups",
            [
                ("include[]", "users"),
                ("include[]", "group_category"),
                ("include[]", "permissions"),
                ("include_inactive_users", "true"),
                ("section_restricted", "true"),
                ("per_page", "100"),
            ],
        ),
        (
            "/api/v1/users/self/groups",
            [("context_type", "Course"), ("per_page", "100")],
        ),
    ]
    assert groups[0]["is_current_user_member"] is False
    assert groups[0].get("html_url") is None
    assert groups[1]["is_current_user_member"] is True
    assert groups[1]["html_url"] == "https://canvas.nus.edu.sg/groups/2"


def test_groups_are_available_under_the_people_tab() -> None:
    assert content_available("groups", {"people"}) is True
    assert content_available("groups", {"assignments"}) is False


def test_semester_filters_are_case_insensitive_and_support_study_years() -> None:
    courses = [
        {"term_folder_name": "2425S1"},
        {"term_folder_name": "2526S2"},
        {"term_folder_name": "Non-Academic"},
    ]
    student = {"enrollment_academic_year": "2425"}

    assert canonical_semester("ay2526s1") == "2526S1"
    assert resolve_semester_filter("LATEST", student, courses) == "2526S2"
    assert resolve_semester_filter("Y3s1", student, courses) == "2627S1"
    assert resolve_semester_filter("non-academic", student, courses) == "Non-Academic"
    assert infer_enrollment_academic_year("2024-07-15T20:35:08Z") == "2425"
    assert infer_enrollment_academic_year("2025-01-01T00:00:00Z") == "2425"


def test_local_paths_are_returned_as_absolute_paths(tmp_path: Path) -> None:
    course_dir = tmp_path / "2526S1" / "CG2028"
    html_path = course_dir / "announcements" / "notice.html"
    json_path = course_dir / "announcements" / "announcements.json"
    html_path.parent.mkdir(parents=True)
    html_path.write_text("notice", encoding="utf-8")
    json_path.write_text("{}", encoding="utf-8")

    value = absolutize_local_paths(
        {
            "message": "notice.html",
            "path": "item.json",
            "html_url": "https://example.test/notice",
        },
        json_path,
    )

    assert value["message"] == html_path.resolve().as_posix()
    assert value["path"] == (json_path.parent / "item.json").resolve().as_posix()
    assert value["html_url"] == "https://example.test/notice"


class PeopleClient:
    def __init__(self) -> None:
        self.calls = 0

    def course_people(self, _course_id: str) -> list[dict[str, Any]]:
        self.calls += 1
        return [{"id": 1, "name": "Student"}]


def test_people_sync_checks_remote_but_does_not_rewrite_unchanged_cache(tmp_path: Path) -> None:
    client = PeopleClient()
    first = sync_people(
        client=client,  # type: ignore[arg-type]
        course_id="1",
        course_dir=tmp_path,
        synced_at="first",
        force=False,
    )
    people_path = tmp_path / "people.json"
    first_text = people_path.read_text(encoding="utf-8")
    second = sync_people(
        client=client,  # type: ignore[arg-type]
        course_id="1",
        course_dir=tmp_path,
        synced_at="second",
        force=False,
    )

    assert first["status"] == "created"
    assert second["status"] == "unchanged"
    assert second["checked"] is True
    assert client.calls == 2
    assert people_path.read_text(encoding="utf-8") == first_text


class GroupsClient:
    def __init__(self) -> None:
        self.calls = 0

    def course_groups(self, _course_id: str) -> list[dict[str, Any]]:
        self.calls += 1
        return [{"id": 1, "name": "Project Group 1", "members_count": 2}]


def test_groups_sync_checks_remote_but_does_not_rewrite_unchanged_cache(tmp_path: Path) -> None:
    client = GroupsClient()
    first = sync_groups(
        client=client,  # type: ignore[arg-type]
        course_id="1",
        course_dir=tmp_path,
        synced_at="first",
        force=False,
    )
    groups_path = tmp_path / "groups.json"
    first_text = groups_path.read_text(encoding="utf-8")
    second = sync_groups(
        client=client,  # type: ignore[arg-type]
        course_id="1",
        course_dir=tmp_path,
        synced_at="second",
        force=False,
    )

    assert first["status"] == "created"
    assert second["status"] == "unchanged"
    assert second["checked"] is True
    assert client.calls == 2
    assert groups_path.read_text(encoding="utf-8") == first_text


def test_concurrent_canvas_cache_writes_use_unique_temporary_files(tmp_path: Path) -> None:
    path = tmp_path / "course.json"

    def write(writer: int) -> None:
        write_json(path, {"writer": writer, "payload": "x" * 10_000})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(32)))

    payload = read_json(path)
    assert payload is not None
    assert payload["writer"] in range(32)
    assert payload["payload"] == "x" * 10_000
    assert list(tmp_path.glob(".course.json.*.tmp")) == []


class EmptyAssignmentsClient:
    def course_assignments(self, _course_id: str) -> list[dict[str, Any]]:
        return []


def test_empty_assignments_are_cached_as_a_successful_result(tmp_path: Path) -> None:
    metadata = {"course": {"default_view": "assignments"}}
    first = sync_assignments(
        client=EmptyAssignmentsClient(),  # type: ignore[arg-type]
        course_id="1",
        course_dir=tmp_path,
        tabs=[],
        synced_at="first",
        force=False,
        course_metadata=metadata,
    )
    second = sync_assignments(
        client=EmptyAssignmentsClient(),  # type: ignore[arg-type]
        course_id="1",
        course_dir=tmp_path,
        tabs=[],
        synced_at="second",
        force=False,
        course_metadata=metadata,
    )

    payload = read_json(tmp_path / "assignments" / "assignments.json")
    assert first["status"] == "created"
    assert second["status"] == "unchanged"
    assert payload is not None
    assert payload["count"] == 0
    assert payload["items"] == []


def test_ambiguous_course_code_is_rejected_with_candidates(tmp_path: Path) -> None:
    fetcher = CanvasFetcher(data_path=tmp_path)
    courses = [
        {
            "id": "1",
            "course_code": "CS1010",
            "term_folder_name": "2425S1",
            "enrollment_roles": ["StudentEnrollment"],
        },
        {
            "id": "2",
            "course_code": "cs1010",
            "term_folder_name": "2526S1",
            "enrollment_roles": ["TaEnrollment"],
        },
    ]

    with pytest.raises(Exception) as exc_info:
        fetcher.resolve_course("CS1010", courses)

    message = str(exc_info.value)
    assert "matched 2 courses" in message
    assert "2425S1 | ID 1 | StudentEnrollment" in message
    assert "2526S1 | ID 2 | TaEnrollment" in message
    assert fetcher.resolve_course("1", courses)["id"] == "1"
    assert fetcher.resolve_course("CS1010", [courses[0]])["id"] == "1"


def test_course_record_exposes_unique_student_and_ta_roles() -> None:
    record = CourseRecord(
        id="1",
        course={
            "sections": [{"enrollment_role": "TaEnrollment"}],
            "enrollments": [
                {"role": "Student Tutor"},
                {"role": "Student Tutor"},
            ],
        },
    )

    assert record.enrollment_roles == ["TaEnrollment", "Student Tutor"]


def test_shared_people_path_is_printed_once_outside_table(monkeypatch: pytest.MonkeyPatch) -> None:
    output = StringIO()
    monkeypatch.setattr(
        canvas_cli,
        "console",
        Console(file=output, force_terminal=False, width=120),
    )

    canvas_cli.print_content_list(
        [
            {"id": 1, "name": "One", "local_path": "/tmp/people.json"},
            {"id": 2, "name": "Two", "local_path": "/tmp/people.json"},
        ]
    )

    rendered = output.getvalue()
    assert rendered.count("/tmp/people.json") == 1
    assert "Local file:" in rendered
    assert "Local path" not in rendered


def test_people_human_output_derives_type_from_enrollments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = StringIO()
    monkeypatch.setattr(
        canvas_cli,
        "console",
        Console(file=output, force_terminal=False, width=160),
    )

    canvas_cli.print_content_list(
        [
            {
                "id": 1,
                "name": "Student One",
                "enrollments": [
                    {"type": "StudentEnrollment", "role": "Student"},
                    {"type": "StudentEnrollment", "role": "Student"},
                ],
            }
        ]
    )

    rendered = output.getvalue()
    assert "Type" in rendered
    assert "Student" in rendered


def test_human_output_omits_type_column_when_no_type_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = StringIO()
    monkeypatch.setattr(
        canvas_cli,
        "console",
        Console(file=output, force_terminal=False, width=120),
    )

    canvas_cli.print_content_list([{"id": 1, "name": "Untyped item"}])

    assert "Type" not in output.getvalue()


def test_groups_human_output_includes_membership_and_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = StringIO()
    monkeypatch.setattr(
        canvas_cli,
        "console",
        Console(file=output, force_terminal=False, width=220),
    )

    canvas_cli.print_group_list(
        [
            {
                "id": 215659,
                "name": "Project Groups 24",
                "group_category": {"id": 26098, "name": "Project Groups"},
                "members_count": 2,
                "users": [{"id": 1, "name": "One"}, {"id": 2, "name": "Two"}],
                "is_current_user_member": True,
                "html_url": "https://canvas.nus.edu.sg/groups/215659",
            }
        ]
    )

    rendered = output.getvalue()
    assert "Group set" in rendered
    assert "Project Groups 24" in rendered
    assert "One, Two" in rendered
    assert "My group" in rendered
    assert "Yes" in rendered
    assert "https://canvas.nus.edu.sg/groups/215659" in rendered


def test_json_and_jsonl_outputs_remove_internal_canvas_sync_metadata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = [
        {
            "id": 1,
            "_canvas_sync": {"signature": "large"},
            "nested": {"_canvas_sync_detail_error": "internal", "name": "visible"},
        }
    ]

    canvas_cli.print_formatted(value, "json")
    assert json.loads(capsys.readouterr().out) == [{"id": 1, "nested": {"name": "visible"}}]

    canvas_cli.print_formatted(value, "jsonl")
    assert json.loads(capsys.readouterr().out) == {"id": 1, "nested": {"name": "visible"}}


def test_empty_module_home_uses_canvas_style_human_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = StringIO()
    monkeypatch.setattr(
        canvas_cli,
        "console",
        Console(file=output, force_terminal=False, width=120),
    )

    canvas_cli.print_empty_home({"resource": "modules", "count": 0, "items": []})

    assert output.getvalue().strip() == "No modules have been defined for this course."


def test_inaccessible_file_link_is_visible_in_human_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = StringIO()
    monkeypatch.setattr(
        canvas_cli,
        "console",
        Console(file=output, force_terminal=False, width=180),
    )
    canvas_cli.print_content_list(
        [
            {
                "id": 99,
                "display_name": "Canvas file 99",
                "url": "https://canvas.example.test/files/99/preview",
                "inaccessible": True,
                "access_error": "Canvas denied access",
            }
        ]
    )

    rendered = output.getvalue()
    assert "https://canvas.example.test/files/99/preview" in rendered
    assert "Inaccessible: Canvas denied access" in rendered
    assert "Local path" not in rendered


@pytest.mark.parametrize("output_format", ["json", "jsonl", "plain"])
def test_inaccessible_file_link_is_visible_in_machine_outputs(
    output_format: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canvas_cli.print_formatted(
        {
            "id": 99,
            "url": "https://canvas.example.test/files/99/preview",
            "inaccessible": True,
            "access_error": "Canvas denied access",
        },
        output_format,
    )

    rendered = capsys.readouterr().out
    assert "https://canvas.example.test/files/99/preview" in rendered
    assert "Canvas denied access" in rendered


def test_cross_course_embedded_file_reference_is_ignored(tmp_path: Path) -> None:
    announcements = tmp_path / "announcements"
    announcements.mkdir()
    (announcements / "notice.html").write_text(
        (
            '<img src="/courses/2/files/200/preview">'
            '<a href="/courses/1/files/100/download">same course</a>'
        ),
        encoding="utf-8",
    )

    references = collect_referenced_files(tmp_path, "1")

    assert set(references) == {"100"}
    assert references["100"]["urls"] == {"/courses/1/files/100/download"}


class InaccessibleFileClient:
    def api_url(self, path: str) -> str:
        return f"https://canvas.example.test{path}"

    def course_folders(self, _course_id: str) -> list[dict[str, Any]]:
        return []

    def file_details(self, _file_id: str) -> dict[str, Any]:
        raise CanvasAPIError("Canvas denied access")


def test_inaccessible_same_course_file_reference_is_cached_and_returned(tmp_path: Path) -> None:
    announcements = tmp_path / "announcements"
    announcements.mkdir()
    (announcements / "notice.html").write_text(
        '<img src="/courses/1/files/99/preview">',
        encoding="utf-8",
    )

    result = sync_files(
        client=InaccessibleFileClient(),  # type: ignore[arg-type]
        course_id="1",
        course_dir=tmp_path,
        tabs=[],
        synced_at="now",
        force=False,
    )
    payload = read_json(tmp_path / "files" / "files.json")

    assert result["count"] == 1
    assert payload is not None
    item = payload["files"][0]
    assert item["inaccessible"] is True
    assert item["access_error"] == "Canvas denied access"
    assert item["url"] == "https://canvas.example.test/courses/1/files/99/preview"
    assert item["downloaded"] is False
    assert "path" not in item

    unchanged = sync_files(
        client=InaccessibleFileClient(),  # type: ignore[arg-type]
        course_id="1",
        course_dir=tmp_path,
        tabs=[],
        synced_at="later",
        force=False,
    )
    assert unchanged["status"] == "unchanged"


def test_inaccessible_content_id_reference_gets_a_canvas_link(tmp_path: Path) -> None:
    (tmp_path / "modules.json").write_text(
        json.dumps({"items": [{"type": "File", "content_id": 77}]}),
        encoding="utf-8",
    )

    sync_files(
        client=InaccessibleFileClient(),  # type: ignore[arg-type]
        course_id="1",
        course_dir=tmp_path,
        tabs=[],
        synced_at="now",
        force=False,
    )
    payload = read_json(tmp_path / "files" / "files.json")

    assert payload is not None
    item = payload["files"][0]
    assert item["url"] == "https://canvas.example.test/courses/1/files/77"
    assert item["reference_urls"] == ["https://canvas.example.test/courses/1/files/77"]


def test_cached_content_item_loads_detail_json_with_absolute_paths(tmp_path: Path) -> None:
    course_dir = tmp_path / "2526S1" / "CG2028"
    assignment_dir = course_dir / "assignments" / "Lab"
    assignment_dir.mkdir(parents=True)
    (course_dir / "course.json").write_text(
        json.dumps({"course": {"id": "1", "course_code": "CG2028"}}),
        encoding="utf-8",
    )
    (assignment_dir / "content.html").write_text("content", encoding="utf-8")
    (assignment_dir / "assignment.json").write_text(
        json.dumps({"id": 123, "name": "Lab", "content": "content.html"}),
        encoding="utf-8",
    )
    assignments_path = course_dir / "assignments" / "assignments.json"
    assignments_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": 123,
                        "kind": "assignment",
                        "name": "Lab",
                        "path": "Lab/assignment.json",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    index = {
        "student": {},
        "courses": [
            {
                "id": "1",
                "course_code": "CG2028",
                "term_folder_name": "2526S1",
                "metadata_path": "2526S1/CG2028/course.json",
            }
        ],
    }
    (tmp_path / "index.json").write_text(json.dumps(index), encoding="utf-8")

    detail = CanvasFetcher(data_path=tmp_path).content(
        "CG2028", "assignments", "123", refresh=False
    )

    assert detail["local_path"] == (assignment_dir / "assignment.json").resolve().as_posix()
    assert detail["content"] == (assignment_dir / "content.html").resolve().as_posix()


def test_unavailable_course_section_is_not_reported_as_a_cache_error(tmp_path: Path) -> None:
    course_dir = tmp_path / "2526S1" / "CG2028"
    course_dir.mkdir(parents=True)
    (course_dir / "course.json").write_text(
        json.dumps(
            {
                "course": {"id": "1", "course_code": "CG2028"},
                "all_tabs": [
                    {"id": "home", "label": "Home", "hidden": False},
                    {"id": "modules", "label": "Modules", "hidden": False},
                ],
                "content": {"sections": {"files": {"status": "closed"}}},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "index.json").write_text(
        json.dumps(
            {
                "student": {},
                "courses": [
                    {
                        "id": "1",
                        "course_code": "CG2028",
                        "term_folder_name": "2526S1",
                        "metadata_path": "2526S1/CG2028/course.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CanvasAPIError) as exc_info:
        CanvasFetcher(data_path=tmp_path).content("CG2028", "assignments", refresh=False)

    assert str(exc_info.value) == (
        "The 'assignments' section is not available for course 'CG2028'. "
        "Accessible Canvas sections: Home, Modules. "
        "Queryable with `canvas course`: home, modules."
    )

    with pytest.raises(CanvasAPIError) as exc_info:
        CanvasFetcher(data_path=tmp_path).content("CG2028", "files", refresh=False)

    assert str(exc_info.value) == (
        "The 'files' section is not available for course 'CG2028'. "
        "Accessible Canvas sections: Home, Modules. "
        "Queryable with `canvas course`: home, modules."
    )


def test_hidden_default_view_is_queryable_and_home_resolves_it(tmp_path: Path) -> None:
    course_dir = tmp_path / "2627S1" / "CP3880"
    course_dir.mkdir(parents=True)
    (course_dir / "course.json").write_text(
        json.dumps(
            {
                "course": {
                    "id": "1",
                    "course_code": "CP3880",
                    "default_view": "modules",
                    "html_url": "https://canvas.example.test/courses/1",
                },
                "all_tabs": [{"id": "home", "label": "Home", "hidden": False}],
                "content": {"sections": {"modules": {"status": "unchanged"}}},
            }
        ),
        encoding="utf-8",
    )
    (course_dir / "modules.json").write_text(
        json.dumps({"course_id": "1", "count": 0, "fingerprint": "empty", "items": []}),
        encoding="utf-8",
    )
    (tmp_path / "index.json").write_text(
        json.dumps(
            {
                "student": {},
                "courses": [
                    {
                        "id": "1",
                        "course_code": "CP3880",
                        "term_folder_name": "2627S1",
                        "metadata_path": "2627S1/CP3880/course.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    fetcher = CanvasFetcher(data_path=tmp_path)
    assert fetcher.content("CP3880", "modules", refresh=False) == []
    assert fetcher.content("CP3880", "home", refresh=False) == {
        "course_id": "1",
        "default_view": "modules",
        "resource": "modules",
        "html_url": "https://canvas.example.test/courses/1",
        "count": 0,
        "items": [],
    }
