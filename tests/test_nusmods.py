from __future__ import annotations

import json
import os
from argparse import Namespace
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_for_nus.academic_calendar import academic_calendar_record
from nusmods.cli import build_parser, module_matches_filters
from nusmods.client import NUSModsAPIError, NUSModsClient, normalize_academic_year
from nusmods.schedule import (
    SINGAPORE_TZ,
    apply_selection_edits,
    available_slots,
    current_academic_year,
    current_semester,
    default_schedule,
    export_share_url,
    import_share_url,
    new_course_record,
    schedule_for_date,
    serialize_lesson,
    student_to_ta,
    ta_to_student,
)


def lesson(
    lesson_type: str,
    class_no: str,
    day: str,
    start: str,
    end: str,
    venue: str,
    weeks: list[int] | dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "classNo": class_no,
        "day": day,
        "startTime": start,
        "endTime": end,
        "venue": venue,
        "lessonType": lesson_type,
        "weeks": weeks if weeks is not None else list(range(1, 14)),
    }


def sample_module(code: str = "CG2028") -> dict[str, Any]:
    return {
        "acadYear": "2026/2027",
        "moduleCode": code,
        "title": "Computer Organization",
        "description": "Computing devices and ARM assembly.",
        "moduleCredit": "2",
        "department": "Computing and Engineering Programme",
        "faculty": "Multi Disciplinary Programme",
        "gradingBasisDescription": "Graded",
        "attributes": {"su": True, "lab": True},
        "semesterData": [
            {
                "semester": 1,
                "examDate": "2026-11-30T01:00:00.000Z",
                "timetable": [
                    lesson("Laboratory", "01", "Wednesday", "0900", "1200", "E4"),
                    lesson("Laboratory", "02", "Thursday", "1400", "1700", "E4"),
                    lesson("Lecture", "01", "Tuesday", "1400", "1500", "E-Learn_A"),
                    lesson("Lecture", "01", "Tuesday", "1500", "1700", "LT"),
                    lesson("Tutorial", "01", "Monday", "1000", "1100", "TR1"),
                    lesson("Tutorial", "02", "Friday", "1000", "1100", "TR2"),
                ],
            },
            {
                "semester": 2,
                "examDate": "2027-05-01T01:00:00.000Z",
                "timetable": [
                    lesson("Lecture", "1", "Monday", "1000", "1200", "LT"),
                    lesson("Tutorial", "1", "Tuesday", "1000", "1100", "TR"),
                ],
            },
        ],
    }


def sample_eg2401a() -> dict[str, Any]:
    module = sample_module("EG2401A")
    module["title"] = "Engineering Professionalism"
    module["semesterData"] = [
        {
            "semester": 1,
            "timetable": [
                lesson("Lecture", "2", "Thursday", "1800", "2000", "E-Learn_A"),
                lesson(
                    "Tutorial",
                    "505",
                    "Friday",
                    "1900",
                    "2000",
                    "E1-06-13",
                    list(range(4, 14)),
                ),
            ],
        }
    ]
    return module


class FakeClient:
    academic_year = "2026/2027"

    def __init__(self, modules: dict[str, dict[str, Any]]) -> None:
        self.modules = modules

    def get_module(self, code: str) -> dict[str, Any]:
        return self.modules[code]


def filter_args(**overrides: Any) -> Namespace:
    values = {
        "semesters": None,
        "no_exam": False,
        "no_exam_clash": None,
        "level": None,
        "units": None,
        "min_units": None,
        "max_units": None,
        "faculty": None,
        "department": None,
        "grading": None,
        "attribute": None,
    }
    values.update(overrides)
    return Namespace(**values)


def test_academic_year_and_semester_switch_in_july() -> None:
    assert current_academic_year(date(2026, 6, 30)) == "2025/2026"
    assert current_academic_year(date(2026, 7, 1)) == "2026/2027"
    assert current_semester(date(2026, 7, 31)) == 1
    assert current_semester(date(2027, 1, 1)) == 2
    assert normalize_academic_year("2026-2027") == ("2026/2027", "2026-2027")


def test_shared_academic_calendar_record_reports_instructional_week() -> None:
    calendar = {"2026/2027": {"1": {"start": [2026, 8, 10]}}}

    result = academic_calendar_record(
        date(2026, 8, 14),
        "2026/2027",
        calendar,
    )

    assert result["semesterName"] == "Semester 1"
    assert result["week"] == 1
    assert result["weekStart"] == "2026-08-10"
    assert result["weekEnd"] == "2026-08-16"
    assert result["instructional"] is True


def test_cli_parser_covers_web_finder_filters() -> None:
    args = build_parser().parse_args(
        [
            "search",
            "computer",
            "--sem",
            "s1",
            "--sem",
            "st2",
            "--no-exam",
            "--no-exam-clash",
            "s2",
            "--level",
            "2000",
            "--units",
            "4",
            "--min-units",
            "2",
            "--max-units",
            "8",
            "--faculty",
            "Computing",
            "--department",
            "Computer Science",
            "--grading",
            "Graded",
            "--attribute",
            "su",
            "--attribute",
            "lab",
            "--format",
            "json",
        ]
    )

    assert args.semesters == [1, 4]
    assert args.no_exam_clash == [2]
    assert args.level == [2]
    assert args.units == [4]
    assert args.attribute == ["su", "lab"]
    assert args.format == "json"


def test_search_limit_defaults_to_unlimited() -> None:
    args = build_parser().parse_args(["search", "computer"])

    assert args.limit is None


def test_refresh_flags_work_before_or_after_commands() -> None:
    parser = build_parser()

    assert parser.parse_args(["--refresh", "course", "CG2028"]).cache_policy == "refresh"
    assert parser.parse_args(["course", "CG2028", "--refresh"]).cache_policy == "refresh"
    assert parser.parse_args(["schedule", "today", "--no-refresh"]).cache_policy == "cache-only"
    assert parser.parse_args(["search", "computer"]).cache_policy == "default"
    with pytest.raises(SystemExit):
        parser.parse_args(["--refresh", "--no-refresh", "search", "computer"])


def test_module_filtering_covers_facets_and_exam_clashes() -> None:
    module = sample_module()
    args = filter_args(
        semesters=[1],
        no_exam_clash=[1],
        level=[2],
        units=[2],
        min_units=2,
        max_units=4,
        faculty=["disciplinary"],
        department=["engineering"],
        grading=["graded"],
        attribute=["su", "lab"],
    )

    assert module_matches_filters(module, args, clash_dates={1: set()})
    assert not module_matches_filters(
        module,
        args,
        clash_dates={1: {"2026-11-30T01:00:00.000Z"}},
    )
    assert not module_matches_filters(module, filter_args(no_exam=True))


def test_lesson_serialization_matches_nusmods_v3() -> None:
    value = serialize_lesson(
        lesson(
            "Tutorial",
            "505",
            "Friday",
            "1900",
            "2000",
            "E1-06-13",
            list(range(4, 14)),
        )
    )
    assert value == "505|FRI|1900|2000|E1-06-13|4_5_6_7_8_9_10_11_12_13"


def test_sample_share_url_import_export_round_trip() -> None:
    url = (
        "https://nusmods.com/timetable/sem-1/share?"
        "CG2028=LAB:02,LEC:01,TUT:01&CP3880=&"
        "EG2401A=LEC:(2|THU|1800|2000|E-Learn_A|1_2_3_4_5_6_7_8_9_10_11_12_13);"
        "TUT:(505|FRI|1900|2000|E1-06-13|4_5_6_7_8_9_10_11_12_13)&ta=EG2401A"
    )
    cp3880 = sample_module("CP3880")
    cp3880["semesterData"] = [{"semester": 1, "timetable": []}]
    client = FakeClient(
        {
            "CG2028": sample_module(),
            "CP3880": cp3880,
            "EG2401A": sample_eg2401a(),
        }
    )

    state, semester, warnings = import_share_url(
        url,
        client=client,  # type: ignore[arg-type]
        state=default_schedule("2026/2027"),
    )

    assert semester == 1
    assert warnings == []
    courses = state["semesters"]["1"]["courses"]
    assert courses["CG2028"]["selections"] == {
        "Laboratory": ["02"],
        "Lecture": ["01"],
        "Tutorial": ["01"],
    }
    assert courses["CP3880"]["selections"] == {}
    assert courses["EG2401A"]["isTa"] is True
    assert export_share_url(state, 1) == url


def test_ta_edit_supports_zero_all_and_individual_slots() -> None:
    module = sample_module()
    course = new_course_record(module, 1, is_ta=True)
    slots = available_slots(module, 1)
    assert len(course["selections"]["Lecture"]) == 2

    apply_selection_edits(
        course,
        module,
        1,
        set_expressions=["LEC=none", "TUT=all"],
        add_expressions=["LAB=@2"],
        remove_expressions=["LAB=@1"],
    )

    assert course["selections"]["Lecture"] == []
    assert len(course["selections"]["Tutorial"]) == 2
    lab_two = next(slot["lessonId"] for slot in slots if slot["selector"] == "LAB@2")
    assert course["selections"]["Laboratory"] == [lab_two]


def test_student_ta_role_conversion_uses_closest_class_group() -> None:
    module = sample_module()
    student = {
        "Lecture": ["01"],
        "Tutorial": ["02"],
        "Laboratory": ["02"],
    }
    ta = student_to_ta(module, 1, student)
    assert len(ta["Lecture"]) == 2
    assert len(ta["Tutorial"]) == 1

    # Add one slot from another tutorial group; the tie resolves to the first
    # class in the module's current timetable.
    tutorial_one = next(
        slot["lessonId"] for slot in available_slots(module, 1) if slot["selector"] == "TUT@1"
    )
    ta["Tutorial"].append(tutorial_one)
    converted = ta_to_student(module, 1, ta)

    assert converted["Lecture"] == ["01"]
    assert converted["Tutorial"] == ["01"]
    assert converted["Laboratory"] == ["02"]


def test_schedule_for_date_uses_calendar_weeks_holidays_and_hidden() -> None:
    module = sample_module()
    state = default_schedule("2026/2027")
    state["semesters"]["1"]["courses"] = {"CG2028": new_course_record(module, 1, is_ta=False)}
    calendar = {"2026/2027": {"1": {"start": [2026, 8, 10]}}}

    result = schedule_for_date(
        state,
        {"CG2028": module},
        date(2026, 8, 11),
        calendar=calendar,
        now=datetime(2026, 8, 10, 12, 0, tzinfo=SINGAPORE_TZ),
    )
    assert result["week"] == 1
    assert [event["lessonType"] for event in result["events"]] == ["Lecture", "Lecture"]

    holiday = schedule_for_date(
        state,
        {"CG2028": module},
        date(2026, 8, 11),
        calendar=calendar,
        holidays=["2026-08-11"],
        now=datetime(2026, 8, 10, 12, 0, tzinfo=SINGAPORE_TZ),
    )
    assert holiday["holiday"] is True
    assert holiday["events"] == []

    # Recess week is omitted, and the following Monday becomes Week 7 rather
    # than Week 8.
    recess = schedule_for_date(
        state,
        {"CG2028": module},
        date(2026, 9, 22),
        calendar=calendar,
        now=datetime(2026, 9, 21, 12, 0, tzinfo=SINGAPORE_TZ),
    )
    after_recess = schedule_for_date(
        state,
        {"CG2028": module},
        date(2026, 9, 29),
        calendar=calendar,
        now=datetime(2026, 9, 28, 12, 0, tzinfo=SINGAPORE_TZ),
    )
    assert recess["week"] is None
    assert recess["events"] == []
    assert after_recess["week"] == 7
    assert len(after_recess["events"]) == 2


class FakeResponse:
    def __init__(self, *, text: str = "", payload: Any = None, status_code: int = 200) -> None:
        self.text = text
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("HTTP error")

    def json(self) -> Any:
        return self.payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def mount(self, *_args: object) -> None:
        pass

    def get(self, url: str, **_kwargs: Any) -> FakeResponse:
        self.calls.append(url)
        return self.response


def test_no_refresh_uses_stale_cache_without_network(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache" / "2026-2027-moduleInformation.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text('[{"moduleCode":"CACHED"}]', encoding="utf-8")
    os.utime(cache_path, (1, 1))
    session = FakeSession(FakeResponse(payload=[{"moduleCode": "REMOTE"}]))
    client = NUSModsClient(
        academic_year="2026/2027",
        data_dir=tmp_path,
        cache_ttl_seconds=0,
        cache_only=True,
        session=session,  # type: ignore[arg-type]
    )

    assert client.list_modules() == [{"moduleCode": "CACHED"}]
    assert session.calls == []


def test_refresh_bypasses_and_replaces_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache" / "2026-2027-moduleInformation.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text('[{"moduleCode":"CACHED"}]', encoding="utf-8")
    session = FakeSession(FakeResponse(payload=[{"moduleCode": "REMOTE"}]))
    client = NUSModsClient(
        academic_year="2026/2027",
        data_dir=tmp_path,
        refresh=True,
        session=session,  # type: ignore[arg-type]
    )

    assert client.list_modules() == [{"moduleCode": "REMOTE"}]
    assert len(session.calls) == 1
    assert json.loads(cache_path.read_text(encoding="utf-8")) == [{"moduleCode": "REMOTE"}]


def test_no_refresh_reports_cache_miss(tmp_path: Path) -> None:
    client = NUSModsClient(
        academic_year="2026/2027",
        data_dir=tmp_path,
        cache_only=True,
        session=FakeSession(FakeResponse(payload=[])),  # type: ignore[arg-type]
    )

    with pytest.raises(NUSModsAPIError, match="No usable cached data"):
        client.list_modules()


def test_public_disqus_comments_are_parsed_without_auth(tmp_path: Path) -> None:
    thread_data = {
        "cursor": {"total": 1, "hasNext": False},
        "response": {
            "posts": [
                {
                    "id": "post-1",
                    "parent": None,
                    "createdAt": "2026-01-01T10:00:00",
                    "author": {"name": "Student"},
                    "likes": 3,
                    "dislikes": 0,
                    "message": "<p>Useful<br>course.</p>",
                }
            ]
        },
    }
    html = (
        "<html><body><script type='text/json' id='disqus-threadData'>"
        f"{json.dumps(thread_data)}</script></body></html>"
    )
    session = FakeSession(FakeResponse(text=html))
    client = NUSModsClient(
        academic_year="2026/2027",
        data_dir=tmp_path,
        session=session,  # type: ignore[arg-type]
    )

    comments = client.get_comments("CG2028", "Computer Organization")

    assert comments["count"] == 1
    assert comments["hasMore"] is False
    assert comments["comments"][0]["author"] == "Student"
    assert comments["comments"][0]["message"] == "Useful\ncourse."
    assert "t_i=CG2028" in session.calls[0]

    cached_comments = client.get_comments("CG2028", "Computer Organization")
    assert cached_comments == comments
    assert len(session.calls) == 1
    assert (tmp_path / "cache" / "comments-CG2028.json").exists()
