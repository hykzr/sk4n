from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

import pytest

from sk4n.canvas import cli as canvas
from sk4n.canvas import output as canvas_output
from sk4n.nusmods import cli as nusmods
from sk4n.outlook import cli as outlook
from sk4n.talent_connect import cli as talent_connect
from sk4n.tools.output import html_text


@pytest.mark.parametrize(
    ("module", "record", "preserved", "removed"),
    [
        (
            outlook,
            {
                "ItemId": {"Id": "mail-1", "ChangeKey": "revision"},
                "Subject": "A message",
                "Body": {"BodyType": "Text", "Value": "Entire message body"},
                "IsRead": False,
                "InternetMessageHeaders": [{"Value": "spam header" * 100}],
                "_outlook": {"path": "/cache/message.json", "body_current": True},
            },
            {"Subject": "A message", "IsRead": False},
            "InternetMessageHeaders",
        ),
        (
            canvas,
            {
                "id": 1,
                "name": "Student",
                "enrollments": [
                    {"role": "StudentEnrollment", "role_id": 3, "grades": {"html_url": "grades"}}
                ],
                "created_at": "long ago",
                "local_path": "/cache/people.json",
            },
            {"id": 1, "name": "Student", "local_path": "/cache/people.json"},
            "created_at",
        ),
        (
            talent_connect,
            {
                "_id": "job-1",
                "title": "Engineer",
                "is_unqualified_student": False,
                "description": "<p>All job details</p>",
                "restriction": {"academic_majors": ["major"] * 100},
                "job_offer": {"_id": "offer", "response": "pending", "status": "sent"},
            },
            {"_id": "job-1", "title": "Engineer", "is_unqualified_student": False},
            "restriction",
        ),
        (
            nusmods,
            {
                "moduleCode": "CS1231",
                "title": "Discrete Structures",
                "prerequisite": "A prerequisite",
                "prerequisiteRule": "duplicated rule",
                "semesterData": [
                    {"semester": 1, "timetable": [{"classNo": "01", "weeks": [1, 2]}]}
                ],
            },
            {"moduleCode": "CS1231", "prerequisite": "A prerequisite"},
            "prerequisiteRule",
        ),
    ],
)
def test_compact_json_jsonl_and_full_contract(
    module: Any,
    record: dict[str, Any],
    preserved: dict[str, Any],
    removed: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = copy.deepcopy(record)
    printer = module.print_result if module is outlook else module.print_formatted
    printer([record], "json")
    compact = json.loads(capsys.readouterr().out)[0]
    for key, item in preserved.items():
        assert compact[key] == item
    assert removed not in compact
    printer([record], "jsonl")
    assert json.loads(capsys.readouterr().out) == compact
    printer([record], "json-full")
    assert json.loads(capsys.readouterr().out) == [original]
    assert record == original


def test_outlook_compact_keeps_recipients_attachments_preview_and_send_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    record = {
        "ItemId": {"Id": "draft"},
        "IsDraft": True,
        "ToRecipients": [
            {"Name": "Recipient", "EmailAddress": "person@example.test", "AADObjectId": "noise"}
        ],
        "CcRecipients": [{"EmailAddress": "cc@example.test"}],
        "BccRecipients": [{"EmailAddress": "bcc@example.test"}],
        "Body": {"BodyType": "Text", "Value": "Complete body"},
        "Preview": "partial",
        "Attachments": [
            {"Name": "file.pdf", "Size": 0, "url": "attachment-url", "Content": "base64-noise"}
        ],
        "_outlook": {
            "path": "/cache/message.json",
            "body_downloaded": True,
            "body_current": False,
            "attachments": [
                {
                    "id": "attachment-1",
                    "name": "file.pdf",
                    "status": "cached",
                    "path": "/cache/file.pdf",
                    "url": "attachment-url",
                }
            ],
            "preview_path": "/cache/preview.png",
            "preview_viewport_only": True,
            "preview_reused": False,
            "preview_output_path": "/export/preview.png",
        },
        "status": "submitted",
        "pending": True,
        "cache_refreshed": True,
        "warning": "Check Sent Items before retrying",
    }
    outlook.print_result(record, "json")
    result = json.loads(capsys.readouterr().out)
    assert result["Body"]["Value"] == "Complete body"
    assert "Preview" not in result
    assert result["_outlook"] == record["_outlook"]
    assert result["ToRecipients"] == [{"Name": "Recipient", "EmailAddress": "person@example.test"}]
    assert result["CcRecipients"] == record["CcRecipients"]
    assert result["BccRecipients"] == record["BccRecipients"]
    assert result["Attachments"] == [{"Name": "file.pdf", "Size": 0, "url": "attachment-url"}]
    assert result["status"] == "submitted"
    assert result["pending"] is True and result["cache_refreshed"] is True
    assert result["warning"] == record["warning"]


@pytest.mark.parametrize("output_format", ["json", "jsonl"])
@pytest.mark.parametrize(
    "message_type",
    ["MeetingRequestMessageType", "MeetingCancellationMessageType", "MeetingResponseMessageType"],
)
def test_outlook_compact_keeps_meeting_schedule_and_responses(
    output_format: str,
    message_type: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    schedule = {
        "__type": f"{message_type}:#Exchange",
        "Start": "2026-11-10T02:00:00Z",
        "End": "2026-11-10T03:00:00Z",
        "TimeZone": "Singapore Standard Time",
        "StartTimeZoneId": "Singapore Standard Time",
        "EndTimeZoneId": "Singapore Standard Time",
        "Location": "Meeting room",
        "IsMeeting": True,
        "IsCancelled": message_type == "MeetingCancellationMessageType",
        "IsAllDayEvent": False,
        "IsRecurring": True,
        "Recurrence": {"DailyRecurrence": {"Interval": 1}},
        "CalendarItemType": "RecurringMaster",
        "MeetingMessageType": "MeetingRequest",
        "MeetingRequestType": "FullUpdate",
        "ResponseType": "Accept",
        "IsResponseRequested": True,
    }
    record = {
        **schedule,
        "ItemId": {"Id": "invitation", "ChangeKey": "revision"},
        "Subject": "Team meeting",
        "Body": {"BodyType": "Text", "Value": "Discuss progress."},
        "Organizer": {
            "Mailbox": {
                "Name": "Organizer",
                "EmailAddress": "organizer@example.test",
                "Id": "noise",
            }
        },
        "RequiredAttendees": [
            {
                "Mailbox": {
                    "Name": "Attendee",
                    "EmailAddress": "attendee@example.test",
                    "Id": "noise",
                },
                "ResponseType": "Accept",
                "LastResponseTime": "2026-11-09T01:00:00Z",
                "ProposedStart": None,
            }
        ],
        "OptionalAttendees": [
            {"Mailbox": {"EmailAddress": "optional@example.test"}, "ResponseType": "Tentative"}
        ],
        "Resources": [{"Mailbox": {"Name": "Meeting room"}, "ResponseType": "Accept"}],
        "AssociatedCalendarItemId": {"Id": "event", "ChangeKey": "revision"},
        "InternetMessageHeaders": [{"Value": "transport metadata"}],
    }
    original = copy.deepcopy(record)
    outlook.print_result(record, output_format)
    result = json.loads(capsys.readouterr().out)
    assert {key: result[key] for key in schedule} == schedule
    assert result["Organizer"] == {
        "Mailbox": {"Name": "Organizer", "EmailAddress": "organizer@example.test"}
    }
    assert result["RequiredAttendees"] == [
        {
            "Mailbox": {"Name": "Attendee", "EmailAddress": "attendee@example.test"},
            "ResponseType": "Accept",
            "LastResponseTime": "2026-11-09T01:00:00Z",
        }
    ]
    assert result["OptionalAttendees"] == record["OptionalAttendees"]
    assert result["Resources"] == record["Resources"]
    assert result["AssociatedCalendarItemId"] == {"Id": "event"}
    assert "InternetMessageHeaders" not in result
    assert record == original


def test_canvas_compact_preserves_access_errors_grades_and_quiz_answers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    record = {
        "id": 1,
        "kind": "assignment",
        "content": "/cache/instructions.html",
        "assignment": {
            "submission_types": ["online_upload"],
            "description": "<p>Instructions</p>",
            "due_at": "2026-11-10",
            "locked_for_user": True,
            "lock_explanation": "Closed",
            "secure_params": "noise",
        },
        "self_submission": {
            "attempt": 1,
            "submission_type": "online_upload",
            "grade": "A",
            "score": 5,
        },
        "questions": [
            {
                "question_html": "<p>Question</p>",
                "result": {"correct": False, "answer": "B"},
                "raw_session_item": {"noise": True},
            }
        ],
        "new_quiz_result": {
            "preview_url": "preview-url",
            "session_items": ["duplicate"],
            "quiz_session": {
                "status": "complete",
                "attempt": 1,
                "authoritative_result": {
                    "score": 0,
                    "points_possible": 5,
                    "requires_additional_review": False,
                },
            },
        },
        "files": [
            {
                "filename": "file.pdf",
                "inaccessible": True,
                "access_error": "Forbidden",
                "url": "file-url",
            }
        ],
    }
    canvas.print_formatted(record, "json")
    result = json.loads(capsys.readouterr().out)
    assert result["assignment"] == {
        "submission_types": ["online_upload"],
        "description": "Instructions",
        "due_at": "2026-11-10",
        "locked_for_user": True,
        "lock_explanation": "Closed",
    }
    assert result["self_submission"]["grade"] == "A"
    assert result["questions"] == [
        {"question_text": "Question", "result": {"correct": False, "answer": "B"}}
    ]
    assert result["files"] == record["files"]
    assert result["new_quiz_result"]["quiz_session"] == record["new_quiz_result"]["quiz_session"]
    assert "session_items" not in result["new_quiz_result"]


@pytest.mark.parametrize("output_format", ["json", "jsonl"])
def test_canvas_submission_history_verifies_attempts_and_submission_times(
    output_format: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    attempts = [
        {
            "attempt": None,
            "submitted_at": None,
            "submission_type": None,
            "workflow_state": "unsubmitted",
        },
        {
            "attempt": 1,
            "submitted_at": "2026-11-01T10:00:00Z",
            "submission_type": "online_upload",
            "workflow_state": "submitted",
            "late": False,
            "missing": False,
            "score": 0,
            "attachments": [
                {
                    "id": 1,
                    "filename": "first.pdf",
                    "url": "https://canvas.example/files/1",
                    "size": 100,
                }
            ],
            "body": "Repeated submission contents",
            "grader_id": 123,
        },
        {
            "attempt": 2,
            "submitted_at": "2026-11-02T11:00:00Z",
            "submission_type": "online_upload",
            "workflow_state": "graded",
            "late": True,
            "seconds_late": 3600,
            "grade": "A",
            "score": 10,
            "preview_url": "https://canvas.example/submissions/1?attempt=2",
        },
    ]
    record = {"attempt": 2, "submission_type": "online_upload", "submission_history": attempts}
    original = copy.deepcopy(record)
    canvas.print_formatted(record, output_format)
    result = json.loads(capsys.readouterr().out)
    history = result["submission_history"]
    assert history[0] == {"attempt": None, "submitted_at": None, "workflow_state": "unsubmitted"}
    assert [item["attempt"] for item in history] == [None, 1, 2]
    assert [item["submitted_at"] for item in history] == [item["submitted_at"] for item in attempts]
    assert history[1]["late"] is False and history[1]["score"] == 0
    assert history[1]["attachments"] == attempts[1]["attachments"]
    assert history[2] == attempts[2]
    assert "body" not in history[1] and "grader_id" not in history[1]
    canvas.print_formatted(record, "json-full")
    assert json.loads(capsys.readouterr().out) == original
    assert record == original
    canvas.print_formatted({**record, "submission_history": []}, output_format)
    assert json.loads(capsys.readouterr().out)["submission_history"] == []


def test_talent_company_jsonl_has_same_details_as_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    company = {
        "_id": "company",
        "company_id": "c1",
        "name": "Employer",
        "description": "<p>About us</p>",
        "guide_order": 1,
    }
    job = {
        "_id": "job",
        "title": "Engineer",
        "description": "<p>Full description</p>",
        "company": company,
    }
    talent_connect.print_formatted({"company": company, "jobs": [job]}, "json")
    result = json.loads(capsys.readouterr().out)
    talent_connect.print_formatted(
        {"company": company, "jobs": [job]},
        "jsonl",
        jsonl_records=[{"record_type": "company", **company}, {"record_type": "job", **job}],
    )
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [
        {"record_type": "company", **result["company"]},
        {"record_type": "job", **result["jobs"][0]},
    ]
    assert "description" not in result["jobs"][0]
    assert result["company"]["description"] == "About us"


def test_nusmods_summary_keeps_exams_and_details_keep_all_lessons(
    capsys: pytest.CaptureFixture[str],
) -> None:
    course = {
        "moduleCode": "CS1231",
        "title": "Discrete Structures",
        "moduleCredit": "4",
        "description": "Full description",
        "prerequisite": "Prerequisite",
        "semesterData": [
            {
                "semester": 1,
                "examDate": "2026-11-27",
                "examDuration": 120,
                "timetable": [
                    {
                        "classNo": "01",
                        "day": "Monday",
                        "weeks": [1, 2],
                        "startTime": "1000",
                        "endTime": "1200",
                    }
                ],
                "covidZones": ["obsolete"],
            }
        ],
    }
    nusmods.print_formatted([course], "json", summary=True)
    summary = json.loads(capsys.readouterr().out)[0]
    assert "description" not in summary
    assert summary["semesterData"][0]["examDate"] == "2026-11-27"
    nusmods.print_formatted(course, "json")
    detail = json.loads(capsys.readouterr().out)
    assert detail["description"] == "Full description"
    assert detail["semesterData"][0]["timetable"] == course["semesterData"][0]["timetable"]


@pytest.mark.parametrize("module", [outlook, canvas, talent_connect, nusmods])
def test_already_small_results_remain_unchanged(
    module: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    record = {"path": "/cache/file", "count": 0, "available": False}
    printer = module.print_result if module is outlook else module.print_formatted
    printer(record, "json")
    assert json.loads(capsys.readouterr().out) == record


@pytest.mark.parametrize("module", [outlook, canvas, talent_connect])
def test_low_level_api_does_not_accept_format(module: Any) -> None:
    args = module.build_parser().parse_args(["api", "/api/example"])
    assert not hasattr(args, "format")
    for output_format in ("json", "json-full", "jsonl", "plain"):
        with pytest.raises(SystemExit) as exc:
            module.build_parser().parse_args(["api", "/api/example", "--format", output_format])
        assert exc.value.code == 2


@pytest.mark.parametrize(
    ("module", "client_class", "payload"),
    [
        (
            canvas,
            canvas.CanvasClient,
            {
                "data": [{"id": 1, "name": "Student", "enrollments": [{"role_id": 3}]}],
                "_canvas": {"metadata": "retained"},
            },
        ),
        (
            outlook,
            outlook.OutlookClient,
            {
                "Body": {
                    "Items": [
                        {
                            "ItemId": {"Id": "mail-1", "ChangeKey": "revision"},
                            "InternetMessageHeaders": [
                                {"HeaderName": "X-Custom", "Value": "retained"}
                            ],
                        }
                    ]
                },
            },
        ),
        (
            talent_connect,
            talent_connect.AuthenticatedKinobiClient,
            {
                "data": [
                    {
                        "_id": "job-1",
                        "title": "Engineer",
                        "restriction": {"academic_majors": ["retained"]},
                    }
                ],
            },
        ),
    ],
)
def test_low_level_api_outputs_response_data_without_compaction(
    module: Any,
    client_class: Any,
    payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(client_class, "request", lambda *_a, **_kw: payload)
    monkeypatch.setattr(
        module, "compact", lambda *_a, **_kw: pytest.fail("API output must not be compacted")
    )
    assert module.run(module.build_parser().parse_args(["api", "/api/example"])) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == payload
    assert captured.err == ""


def test_html_text_keeps_prose_links_and_line_boundaries() -> None:
    assert (
        html_text(
            '<style>noise</style><p>Hello <b>world</b>.</p><p><a href="https://example.test">Apply</a></p>'
        )
        == "Hello world.\nApply (https://example.test)"
    )
    assert (
        html_text("Plain text <email@example.test> remains intact")
        == "Plain text <email@example.test> remains intact"
    )
    assert html_text("Fill in <name> and <duration>.") == "Fill in <name> and <duration>."
    assert html_text("<pre>for x in xs:\n    print(x)</pre>") == "for x in xs:\n    print(x)"
    assert html_text("<table><tr><td>1</td><td>2</td></tr></table>") == "1 2"
    assert (
        html_text('<p><img src="images/formula.png" alt="x + y"></p>')
        == "x + y (images/formula.png)"
    )


def test_html_text_preserves_ten_or_more_code_blocks() -> None:
    blocks = [f"block_{index}_line_1\n    block_{index}_line_2" for index in range(12)]
    html = "".join(f"<pre>{block}</pre>" for block in blocks)
    assert html_text(html) == "\n".join(blocks)


def test_html_text_preserves_image_inside_link() -> None:
    assert (
        html_text(
            '<p><a href="https://example.test/full"><img src="images/chart.png" alt="Chart"></a></p>'
        )
        == "Chart (images/chart.png) (https://example.test/full)"
    )


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<p>Evaluate 2<sup>10</sup>.</p>", "Evaluate 2^(10)."),
        ("<p>Compare x<sub>2</sub> and x2.</p>", "Compare x_(2) and x2."),
        ("x<sup>n + 1</sup>", "x^(n + 1)"),
        ("H<sub>2</sub>O", "H_(2)O"),
        ("<p>x<sub>i<sup>2</sup></sub></p>", "x_(i^(2))"),
        ("<p>x<sup><i>n</i> &minus; 1</sup></p>", "x^(n \N{MINUS SIGN} 1)"),
        (
            "<pre>x<sub>i</sub> = 2<sup>10</sup>\n    y = x</pre>",
            "x_(i) = 2^(10)\n    y = x",
        ),
    ],
)
def test_canvas_compact_preserves_mathematical_notation(
    html: str, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert html_text(html) == expected
    record = {"question_html": html, "answers": [{"html": html, "selected": True, "correct": True}]}
    canvas.print_formatted(record, "json")
    question = json.loads(capsys.readouterr().out)
    assert question["question_text"] == expected
    assert question["answers"][0]["text"] == expected


@pytest.mark.parametrize("output_format", ["json", "jsonl"])
def test_canvas_original_html_preserves_markup_and_compacts_other_fields(
    output_format: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    html = '<p class="equation" style="color: red">x<sup>2</sup> &nbsp; H<sub>2</sub>O</p>\n'
    record = {
        "course": {"name": "Course", "course_code": "CG2028", "syllabus_body": html},
        "announcements": [
            {"id": 1, "discussion_type": "threaded", "message": html, "subscription_hold": "noise"}
        ],
        "assignment": {
            "submission_types": ["online_text_entry"],
            "description": html,
            "secure_params": "noise",
        },
        "self_submission": {"attempt": 1, "submission_type": "online_text_entry", "body": html},
        "questions": [
            {
                "question_html": html,
                "question_text": "Existing text",
                "classes": ["noise"],
                "raw_session_item": {"noise": True},
                "answers": [
                    {"html": html, "text": "Existing text", "selected": True, "correct": False}
                ],
            }
        ],
        "_canvas": {"signature": "noise"},
    }
    original = copy.deepcopy(record)
    monkeypatch.setattr(
        canvas_output, "html_text", lambda *_a: pytest.fail("Original HTML must not be converted")
    )
    canvas.print_formatted([record], output_format, original_html=True)
    value = json.loads(capsys.readouterr().out)
    result = value[0] if output_format == "json" else value
    assert result["course"]["syllabus_body"] == html
    assert result["announcements"] == [{"id": 1, "discussion_type": "threaded", "message": html}]
    assert result["assignment"] == {"submission_types": ["online_text_entry"], "description": html}
    assert result["self_submission"]["body"] == html
    assert result["questions"] == [
        {
            "question_html": html,
            "question_text": "Existing text",
            "answers": [
                {"html": html, "text": "Existing text", "selected": True, "correct": False}
            ],
        }
    ]
    assert "_canvas" not in result
    assert record == original


@pytest.mark.parametrize("output_format", ["json", "jsonl", "json-full"])
@pytest.mark.parametrize(
    ("command", "method"),
    [
        (["course", "CG2028", "--no-refresh"], "course"),
        (["course", "CG2028", "announcements", "list", "--no-refresh"], "content"),
        (["calendar-events"], "calendar_events"),
        (["todo"], "todo"),
        (["upcoming"], "upcoming"),
    ],
)
def test_canvas_original_html_cli_routes_to_output(
    command: list[str],
    method: str,
    output_format: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record = {"description": '<p style="color: red">Keep this HTML</p>'}
    fetcher = SimpleNamespace(last_selected_course=None)
    setattr(fetcher, method, lambda *_a, **_kw: record)
    monkeypatch.setattr(canvas, "_fetcher", lambda *_a: fetcher)
    args = canvas.build_parser().parse_args(
        [*command, "--format", output_format, "--original-html"]
    )
    assert canvas.run(args) == 0
    assert json.loads(capsys.readouterr().out) == record


@pytest.mark.parametrize("format_args", [[], ["--format", "plain"]])
def test_canvas_original_html_rejects_non_json_output_before_reading(
    format_args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canvas, "_fetcher", lambda *_a: pytest.fail("Must validate before reading"))
    args = canvas.build_parser().parse_args(["todo", "--original-html", *format_args])
    with pytest.raises(ValueError, match="--original-html requires --format"):
        canvas.run(args)
