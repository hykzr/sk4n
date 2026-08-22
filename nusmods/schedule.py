from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from agent_for_nus import academic_calendar as _academic_calendar

from .client import DEFAULT_DATA_DIR, NUSModsClient

SINGAPORE_TZ = _academic_calendar.SINGAPORE_TZ
current_academic_year = _academic_calendar.current_academic_year
current_semester = _academic_calendar.current_semester
normalize_academic_year = _academic_calendar.normalize_academic_year
semester_for_date = _academic_calendar.semester_for_date
semester_start = _academic_calendar.semester_start

SCHEDULE_SCHEMA_VERSION = 1
SCHEDULE_FILENAME = "schedule.json"

SEMESTER_NAMES = {
    1: "Semester 1",
    2: "Semester 2",
    3: "Special Term I",
    4: "Special Term II",
}
SEMESTER_PATHS = {1: "sem-1", 2: "sem-2", 3: "st-i", 4: "st-ii"}
SEMESTER_ALIASES = {
    "1": 1,
    "s1": 1,
    "sem1": 1,
    "sem-1": 1,
    "semester1": 1,
    "semester-1": 1,
    "2": 2,
    "s2": 2,
    "sem2": 2,
    "sem-2": 2,
    "semester2": 2,
    "semester-2": 2,
    "3": 3,
    "st1": 3,
    "st-i": 3,
    "4": 4,
    "st2": 4,
    "st-ii": 4,
}

LESSON_TYPE_ABBREV = {
    "Design Lecture": "DLEC",
    "Laboratory": "LAB",
    "Lecture": "LEC",
    "Packaged Laboratory": "PLAB",
    "Packaged Lecture": "PLEC",
    "Packaged Tutorial": "PTUT",
    "Recitation": "REC",
    "Sectional Teaching": "SEC",
    "Seminar-Style Module Class": "SEM",
    "Tutorial": "TUT",
    "Tutorial Type 2": "TUT2",
    "Tutorial Type 3": "TUT3",
    "Workshop": "WS",
}
LESSON_ABBREV_TYPE = {value: key for key, value in LESSON_TYPE_ABBREV.items()}
DAY_ABBREV = {
    "Monday": "MON",
    "Tuesday": "TUE",
    "Wednesday": "WED",
    "Thursday": "THU",
    "Friday": "FRI",
    "Saturday": "SAT",
    "Sunday": "SUN",
}
ABBREV_DAY = {value: key for key, value in DAY_ABBREV.items()}


def parse_semester(value: str | int) -> int:
    if isinstance(value, int):
        semester = value
    else:
        candidate = re.sub(r"\s+", "", value.strip().lower())
        semester = SEMESTER_ALIASES.get(candidate, 0)
    if semester not in SEMESTER_NAMES:
        raise ValueError("Semester must be s1, s2, st1, or st2.")
    return semester


def default_schedule(academic_year: str) -> dict[str, Any]:
    display_year, _ = normalize_academic_year(academic_year)
    return {
        "schemaVersion": SCHEDULE_SCHEMA_VERSION,
        "academicYear": display_year,
        "semesters": {"1": {"courses": {}}, "2": {"courses": {}}},
        "updatedAt": None,
    }


class ScheduleStore:
    def __init__(self, data_dir: Path = DEFAULT_DATA_DIR) -> None:
        self.path = Path(data_dir) / SCHEDULE_FILENAME

    def load(self, *, academic_year: str) -> dict[str, Any]:
        if not self.path.exists():
            return default_schedule(academic_year)
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read schedule data from {self.path}: {exc}") from exc
        if not isinstance(state, dict) or state.get("schemaVersion") != SCHEDULE_SCHEMA_VERSION:
            raise ValueError(f"Unsupported schedule data in {self.path}.")
        if not isinstance(state.get("semesters"), dict):
            raise ValueError(f"Schedule data in {self.path} has no semesters object.")
        return state

    def save(self, state: Mapping[str, Any]) -> Path:
        value = deepcopy(dict(state))
        value["schemaVersion"] = SCHEDULE_SCHEMA_VERSION
        value["updatedAt"] = datetime.now(SINGAPORE_TZ).isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temporary_name, self.path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return self.path


def semester_data(module: Mapping[str, Any], semester: int) -> dict[str, Any] | None:
    for item in module.get("semesterData") or []:
        if isinstance(item, dict) and int(item.get("semester") or 0) == semester:
            return item
    return None


def semester_timetable(module: Mapping[str, Any], semester: int) -> list[dict[str, Any]]:
    data = semester_data(module, semester)
    if not data or not isinstance(data.get("timetable"), list):
        return []
    return [lesson for lesson in data["timetable"] if isinstance(lesson, dict)]


def serialize_weeks(weeks: Any) -> str:
    if isinstance(weeks, list):
        return "_" if not weeks else "_".join(str(int(week)) for week in weeks)
    if not isinstance(weeks, Mapping):
        return "_"
    start = str(weeks.get("start") or "")
    end = str(weeks.get("end") or "")
    interval = int(weeks.get("weekInterval") or 0)
    serialized = f"{start}_{end}_{interval}"
    if "weeks" not in weeks:
        return serialized
    selected = weeks.get("weeks")
    if not isinstance(selected, list) or not selected:
        return f"{serialized}__"
    return f"{serialized}_{serialize_weeks(selected)}"


def serialize_lesson(lesson: Mapping[str, Any]) -> str:
    day = str(lesson.get("day") or "")
    day_abbreviation = DAY_ABBREV.get(day)
    if not day_abbreviation:
        raise ValueError(f"Unsupported lesson day: {day!r}")
    return "|".join(
        (
            str(lesson.get("classNo") or ""),
            day_abbreviation,
            str(lesson.get("startTime") or ""),
            str(lesson.get("endTime") or ""),
            str(lesson.get("venue") or ""),
            serialize_weeks(lesson.get("weeks")),
        )
    )


_LESSON_ID_RE = re.compile(
    r"^(?P<class_no>.*)\|(?P<day>MON|TUE|WED|THU|FRI|SAT|SUN)"
    r"\|(?P<start>\d{4})\|(?P<end>\d{4})\|(?P<venue>.*)"
    r"\|(?P<weeks>[0-9_-]+)$"
)


def deserialize_weeks(value: str) -> list[int] | dict[str, Any]:
    if re.fullmatch(r"(?:_*\d*)*", value):
        if value == "_":
            return []
        return [int(item) for item in value.split("_") if item]
    match = re.fullmatch(
        r"(?P<start>\d{4}-\d{2}-\d{2})_(?P<end>\d{4}-\d{2}-\d{2})_"
        r"(?P<interval>\d+)_?(?P<weeks>(?:_*\d*)*)",
        value,
    )
    if not match:
        raise ValueError("Serialized lesson weeks are malformed.")
    result: dict[str, Any] = {
        "start": match.group("start"),
        "end": match.group("end"),
    }
    interval = int(match.group("interval"))
    if interval:
        result["weekInterval"] = interval
    serialized_numbers = match.group("weeks")
    if serialized_numbers:
        result["weeks"] = [int(item) for item in serialized_numbers.split("_") if item]
    return result


def deserialize_lesson(lesson_id: str, lesson_type: str) -> dict[str, Any]:
    match = _LESSON_ID_RE.fullmatch(lesson_id)
    if not match:
        raise ValueError("Serialized lesson ID is malformed.")
    return {
        "classNo": match.group("class_no"),
        "day": ABBREV_DAY[match.group("day")],
        "startTime": match.group("start"),
        "endTime": match.group("end"),
        "venue": match.group("venue"),
        "weeks": deserialize_weeks(match.group("weeks")),
        "lessonType": lesson_type,
    }


def lesson_map(module: Mapping[str, Any], semester: int) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for lesson in semester_timetable(module, semester):
        lesson_type = str(lesson.get("lessonType") or "")
        try:
            lesson_id = serialize_lesson(lesson)
        except ValueError:
            continue
        result.setdefault(lesson_type, {})[lesson_id] = lesson
    return result


def first_student_selections(module: Mapping[str, Any], semester: int) -> dict[str, list[str]]:
    selections: dict[str, list[str]] = {}
    for lesson in semester_timetable(module, semester):
        lesson_type = str(lesson.get("lessonType") or "")
        class_no = str(lesson.get("classNo") or "")
        if lesson_type and class_no and lesson_type not in selections:
            selections[lesson_type] = [class_no]
    return selections


def student_to_ta(
    module: Mapping[str, Any],
    semester: int,
    selections: Mapping[str, Sequence[str]],
) -> dict[str, list[str]]:
    mapped = lesson_map(module, semester)
    result: dict[str, list[str]] = {}
    for lesson_type, identifiers in selections.items():
        class_numbers = set(identifiers)
        result[lesson_type] = [
            lesson_id
            for lesson_id, lesson in mapped.get(lesson_type, {}).items()
            if str(lesson.get("classNo")) in class_numbers
        ]
    return result


def ta_to_student(
    module: Mapping[str, Any],
    semester: int,
    selections: Mapping[str, Sequence[str]],
) -> dict[str, list[str]]:
    mapped = lesson_map(module, semester)
    result: dict[str, list[str]] = {}
    for lesson_type, available in mapped.items():
        selected_classes = [
            str(available[lesson_id].get("classNo"))
            for lesson_id in selections.get(lesson_type, [])
            if lesson_id in available
        ]
        if selected_classes:
            counts = Counter(selected_classes)
            order = {
                str(lesson.get("classNo")): index for index, lesson in enumerate(available.values())
            }
            class_no = min(counts, key=lambda item: (-counts[item], order.get(item, 10**9)))
        else:
            first_lesson = next(iter(available.values()), None)
            if not first_lesson:
                continue
            class_no = str(first_lesson.get("classNo"))
        result[lesson_type] = [class_no]
    return result


def new_course_record(
    module: Mapping[str, Any],
    semester: int,
    *,
    is_ta: bool,
) -> dict[str, Any]:
    student = first_student_selections(module, semester)
    selections = student_to_ta(module, semester, student) if is_ta else student
    return {
        "isTa": is_ta,
        "hidden": False,
        "selections": selections,
    }


def available_slots(module: Mapping[str, Any], semester: int) -> list[dict[str, Any]]:
    indexes: Counter[str] = Counter()
    slots: list[dict[str, Any]] = []
    for lesson in semester_timetable(module, semester):
        lesson_type = str(lesson.get("lessonType") or "")
        indexes[lesson_type] += 1
        item = dict(lesson)
        item["lessonId"] = serialize_lesson(lesson)
        item["slotIndex"] = indexes[lesson_type]
        item["selector"] = (
            f"{LESSON_TYPE_ABBREV.get(lesson_type, lesson_type)}@{indexes[lesson_type]}"
        )
        slots.append(item)
    return slots


def normalize_lesson_type(value: str, module: Mapping[str, Any], semester: int) -> str:
    candidate = value.strip()
    available = {str(item.get("lessonType")) for item in semester_timetable(module, semester)}
    by_fold = {item.casefold(): item for item in available}
    if candidate.casefold() in by_fold:
        return by_fold[candidate.casefold()]
    expanded = LESSON_ABBREV_TYPE.get(candidate.upper())
    if expanded in available:
        return expanded
    choices = ", ".join(
        sorted(f"{LESSON_TYPE_ABBREV.get(item, item)} ({item})" for item in available)
    )
    raise ValueError(f"Unknown lesson type {candidate!r}. Available types: {choices or 'none'}.")


def _selectors_for_type(
    module: Mapping[str, Any],
    semester: int,
    lesson_type: str,
    raw_selectors: Sequence[str],
    *,
    is_ta: bool,
) -> list[str]:
    available = [
        slot for slot in available_slots(module, semester) if slot.get("lessonType") == lesson_type
    ]
    if len(raw_selectors) == 1 and raw_selectors[0].casefold() == "none":
        if not is_ta:
            raise ValueError("Student courses must select one class group per lesson type.")
        return []
    if len(raw_selectors) == 1 and raw_selectors[0].casefold() == "all":
        if not is_ta:
            class_numbers = list(dict.fromkeys(str(slot.get("classNo")) for slot in available))
            if len(class_numbers) != 1:
                raise ValueError("Use one class number for a student course, not 'all'.")
            return class_numbers
        return [str(slot["lessonId"]) for slot in available]

    selected: list[str] = []
    for raw_selector in raw_selectors:
        selector = raw_selector.strip()
        if not selector:
            continue
        if selector.startswith("@"):
            try:
                index = int(selector[1:])
            except ValueError as exc:
                raise ValueError(f"Invalid slot selector {selector!r}; expected @N.") from exc
            slot = next((item for item in available if item["slotIndex"] == index), None)
            if slot is None:
                raise ValueError(f"No {lesson_type} slot {selector}.")
            value = str(slot["lessonId"]) if is_ta else str(slot.get("classNo"))
        elif "|" in selector:
            slot = next((item for item in available if item["lessonId"] == selector), None)
            if slot is None:
                raise ValueError(f"Lesson ID does not match an available {lesson_type} slot.")
            value = str(slot["lessonId"]) if is_ta else str(slot.get("classNo"))
        else:
            matching = [item for item in available if str(item.get("classNo")) == selector]
            if not matching:
                raise ValueError(f"No {lesson_type} class group {selector!r}.")
            if is_ta:
                for item in matching:
                    lesson_id = str(item["lessonId"])
                    if lesson_id not in selected:
                        selected.append(lesson_id)
                continue
            value = selector
        if value not in selected:
            selected.append(value)
    if not is_ta and len(selected) != 1:
        raise ValueError("Student courses require exactly one class group per lesson type.")
    return selected


def parse_selection_expression(
    expression: str,
    module: Mapping[str, Any],
    semester: int,
    *,
    is_ta: bool,
) -> tuple[str, list[str]]:
    if "=" not in expression:
        raise ValueError("Selection must look like TYPE=SELECTOR[,SELECTOR...].")
    raw_type, raw_values = expression.split("=", 1)
    lesson_type = normalize_lesson_type(raw_type, module, semester)
    selectors = [item.strip() for item in raw_values.split(",")]
    return lesson_type, _selectors_for_type(
        module,
        semester,
        lesson_type,
        selectors,
        is_ta=is_ta,
    )


def apply_selection_edits(
    course: dict[str, Any],
    module: Mapping[str, Any],
    semester: int,
    *,
    set_expressions: Iterable[str] = (),
    add_expressions: Iterable[str] = (),
    remove_expressions: Iterable[str] = (),
    clear_types: Iterable[str] = (),
) -> None:
    set_expressions = list(set_expressions)
    add_expressions = list(add_expressions)
    remove_expressions = list(remove_expressions)
    clear_types = list(clear_types)
    is_ta = bool(course.get("isTa"))
    selections = course.setdefault("selections", {})
    if not isinstance(selections, dict):
        raise ValueError("Stored course selections are invalid.")
    for expression in set_expressions:
        lesson_type, selected = parse_selection_expression(
            expression, module, semester, is_ta=is_ta
        )
        selections[lesson_type] = selected
    if (add_expressions or remove_expressions or clear_types) and not is_ta:
        raise ValueError(
            "--add-slot, --remove-slot, and --clear are available only for TA courses."
        )
    for expression in add_expressions:
        lesson_type, selected = parse_selection_expression(expression, module, semester, is_ta=True)
        existing = list(selections.get(lesson_type) or [])
        selections[lesson_type] = list(dict.fromkeys([*existing, *selected]))
    for expression in remove_expressions:
        lesson_type, selected = parse_selection_expression(expression, module, semester, is_ta=True)
        removed = set(selected)
        selections[lesson_type] = [
            item for item in selections.get(lesson_type) or [] if item not in removed
        ]
    for raw_type in clear_types:
        lesson_type = normalize_lesson_type(raw_type, module, semester)
        selections[lesson_type] = []


def resolve_course_lessons(
    module: Mapping[str, Any],
    semester: int,
    course: Mapping[str, Any],
) -> list[dict[str, Any]]:
    selections = course.get("selections")
    if not isinstance(selections, Mapping):
        return []
    is_ta = bool(course.get("isTa"))
    mapped = lesson_map(module, semester)
    resolved: list[dict[str, Any]] = []
    for lesson_type, raw_identifiers in selections.items():
        if not isinstance(raw_identifiers, list):
            continue
        if is_ta:
            for lesson_id in raw_identifiers:
                lesson = mapped.get(str(lesson_type), {}).get(str(lesson_id))
                if lesson is None:
                    try:
                        lesson = deserialize_lesson(str(lesson_id), str(lesson_type))
                    except ValueError:
                        continue
                resolved.append(dict(lesson))
        else:
            class_numbers = {str(item) for item in raw_identifiers}
            for lesson in semester_timetable(module, semester):
                if (
                    lesson.get("lessonType") == lesson_type
                    and str(lesson.get("classNo")) in class_numbers
                ):
                    resolved.append(dict(lesson))
    return resolved


def _parse_ta_v1(value: str) -> dict[str, str]:
    return {
        match.group("code").upper(): match.group("config")
        for match in re.finditer(
            r"(?P<code>[A-Za-z0-9]+)\((?P<config>[^()]*)\)(?:,|$)",
            value,
        )
    }


def _split_config(value: str) -> list[tuple[str, list[str], bool]]:
    if not value:
        return []
    if value.endswith(")"):
        parts = value.split(";")
        result: list[tuple[str, list[str], bool]] = []
        for part in parts:
            match = re.fullmatch(r"([^:]+):\((.*)\)", part)
            if not match:
                continue
            identifiers = match.group(2).split(",") if match.group(2) else []
            result.append((match.group(1), identifiers, True))
        return result
    result = []
    for part in value.split(","):
        if ":" not in part:
            continue
        abbreviation, class_no = part.split(":", 1)
        result.append((abbreviation, [class_no] if class_no else [], False))
    return result


def _validated_import_selections(
    values: Sequence[str],
    module: Mapping[str, Any],
    semester: int,
    *,
    is_ta: bool,
    ta_v1_override: str | None,
) -> dict[str, list[str]]:
    timetable = semester_timetable(module, semester)
    mapped = lesson_map(module, semester)
    selections: dict[str, list[str]] = {}
    effective_values = [ta_v1_override] if ta_v1_override is not None else list(values)
    for value in effective_values:
        for abbreviation, identifiers, wrapped in _split_config(value):
            lesson_type = LESSON_ABBREV_TYPE.get(abbreviation)
            if not lesson_type or lesson_type not in mapped:
                continue
            if wrapped:
                lesson_ids: list[str] = []
                for identifier in identifiers:
                    if identifier.isdigit():
                        index = int(identifier)
                        if 0 <= index < len(timetable):
                            indexed_lesson = timetable[index]
                            if indexed_lesson.get("lessonType") == lesson_type:
                                lesson_ids.append(serialize_lesson(indexed_lesson))
                    else:
                        lesson_ids.append(identifier)
                if is_ta:
                    valid = set(mapped[lesson_type])
                    selected = [lesson_id for lesson_id in lesson_ids if lesson_id in valid]
                    selections[lesson_type] = list(
                        dict.fromkeys([*selections.get(lesson_type, []), *selected])
                    )
                else:
                    class_no = None
                    if lesson_ids:
                        first = mapped[lesson_type].get(lesson_ids[0])
                        if first:
                            candidate = str(first.get("classNo"))
                            group = {
                                lesson_id
                                for lesson_id, lesson in mapped[lesson_type].items()
                                if str(lesson.get("classNo")) == candidate
                            }
                            if set(lesson_ids) == group:
                                class_no = candidate
                    selections[lesson_type] = [class_no] if class_no else []
            else:
                class_no = identifiers[0] if identifiers else ""
                matches = [
                    lesson_id
                    for lesson_id, lesson in mapped[lesson_type].items()
                    if str(lesson.get("classNo")) == class_no
                ]
                if is_ta:
                    selections[lesson_type] = list(
                        dict.fromkeys([*selections.get(lesson_type, []), *matches])
                    )
                else:
                    selections[lesson_type] = [class_no] if matches else []

    if is_ta:
        total = sum(len(value) for value in selections.values())
        if total == 0 and mapped:
            return {
                lesson_type: [next(iter(lessons))]
                for lesson_type, lessons in mapped.items()
                if lessons
            }
        return selections

    recovered: dict[str, list[str]] = {}
    for lesson_type, lessons in mapped.items():
        selected = selections.get(lesson_type) or []
        class_numbers = {str(lesson.get("classNo")) for lesson in lessons.values()}
        if len(selected) == 1 and selected[0] in class_numbers:
            recovered[lesson_type] = selected
        else:
            first_lesson = next(iter(lessons.values()), None)
            if first_lesson:
                recovered[lesson_type] = [str(first_lesson.get("classNo"))]
    return recovered


def import_share_url(
    url: str,
    *,
    client: NUSModsClient,
    state: dict[str, Any],
) -> tuple[dict[str, Any], int, list[str]]:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
        "nusmods.com",
        "www.nusmods.com",
    }:
        raise ValueError("Expected a https://nusmods.com timetable share URL.")
    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts or path_parts[0] != "timetable":
        raise ValueError("URL is not a NUSMods timetable share URL.")
    semester = next(
        (SEMESTER_ALIASES[part.lower()] for part in path_parts if part.lower() in SEMESTER_ALIASES),
        0,
    )
    if semester not in {1, 2}:
        raise ValueError("Only Semester 1 and Semester 2 schedules are supported.")
    if "share" not in {part.lower() for part in path_parts}:
        raise ValueError("URL must use NUSMods' /share timetable route.")

    query = parse_qs(parsed.query, keep_blank_values=True)
    ta_values = query.get("ta") or []
    last_ta = ta_values[-1] if ta_values else ""
    ta_v1 = _parse_ta_v1(last_ta) if last_ta.endswith(")") else {}
    ta_codes = set(ta_v1)
    if last_ta and not ta_v1:
        ta_codes.update(code.upper() for code in last_ta.split(",") if code)
    hidden_values = query.get("hidden") or []
    hidden = {
        code.upper() for code in (hidden_values[-1].split(",") if hidden_values else []) if code
    }

    course_params = {
        code.upper(): values
        for code, values in query.items()
        if code.lower() not in {"ta", "hidden"}
    }
    imported: dict[str, Any] = {}
    warnings: list[str] = []
    for course_code, values in sorted(course_params.items()):
        try:
            module = client.get_module(course_code)
        except Exception as exc:
            warnings.append(f"Skipped {course_code}: {exc}")
            continue
        is_ta = course_code in ta_codes
        imported[course_code] = {
            "isTa": is_ta,
            "hidden": course_code in hidden,
            "selections": _validated_import_selections(
                values,
                module,
                semester,
                is_ta=is_ta,
                ta_v1_override=ta_v1.get(course_code),
            ),
        }

    updated = deepcopy(state)
    updated["academicYear"] = client.academic_year
    updated.setdefault("semesters", {})[str(semester)] = {"courses": imported}
    return updated, semester, warnings


def serialize_course_config(course: Mapping[str, Any]) -> str:
    selections = course.get("selections")
    if not isinstance(selections, Mapping):
        return ""
    is_ta = bool(course.get("isTa"))
    parts: list[str] = []
    for lesson_type, identifiers in selections.items():
        abbreviation = LESSON_TYPE_ABBREV.get(str(lesson_type))
        if not abbreviation or not isinstance(identifiers, list):
            continue
        joined = ",".join(str(identifier) for identifier in identifiers)
        parts.append(f"{abbreviation}:{f'({joined})' if is_ta else joined}")
    return (";" if is_ta else ",").join(parts)


def export_share_url(state: Mapping[str, Any], semester: int) -> str:
    courses = (
        state.get("semesters", {}).get(str(semester), {}).get("courses", {})
        if isinstance(state.get("semesters"), Mapping)
        else {}
    )
    if not isinstance(courses, Mapping):
        courses = {}
    params = [
        f"{course_code}={serialize_course_config(course)}"
        for course_code, course in sorted(courses.items())
        if isinstance(course, Mapping)
    ]
    hidden = [
        course_code
        for course_code, course in sorted(courses.items())
        if isinstance(course, Mapping) and course.get("hidden")
    ]
    ta = [
        course_code
        for course_code, course in sorted(courses.items())
        if isinstance(course, Mapping) and course.get("isTa")
    ]
    if hidden:
        params.append(f"hidden={','.join(hidden)}")
    if ta:
        params.append(f"ta={','.join(ta)}")
    path = SEMESTER_PATHS[semester]
    return f"https://nusmods.com/timetable/{path}/share?{'&'.join(params)}"


def format_weeks(weeks: Any) -> str:
    if isinstance(weeks, Mapping):
        start = weeks.get("start", "?")
        end = weeks.get("end", "?")
        interval = int(weeks.get("weekInterval") or 1)
        suffix = f", every {interval} weeks" if interval != 1 else ""
        return f"{start} to {end}{suffix}"
    if not isinstance(weeks, list) or not weeks:
        return "No weeks"
    numbers = list(dict.fromkeys(int(item) for item in weeks))
    if numbers == list(range(1, 14)):
        return "Weeks 1-13"
    ranges: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return f"Weeks {', '.join(ranges)}"


def lesson_occurs_on(lesson: Mapping[str, Any], target: date, week: int) -> bool:
    if lesson.get("day") != target.strftime("%A"):
        return False
    weeks = lesson.get("weeks")
    if isinstance(weeks, list):
        return week in {int(item) for item in weeks}
    if isinstance(weeks, Mapping):
        try:
            current = date.fromisoformat(str(weeks["start"]))
            end = min(date.fromisoformat(str(weeks["end"])), target)
        except (KeyError, ValueError):
            return False
        while current <= end:
            if current == target:
                return True
            current += timedelta(days=7)
    return False


def schedule_for_date(
    state: Mapping[str, Any],
    modules: Mapping[str, Mapping[str, Any]],
    target: date,
    *,
    calendar: Mapping[str, Any] | None,
    holidays: Iterable[str] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    academic_year = str(state.get("academicYear"))
    semester, week = semester_for_date(target, academic_year, calendar)
    holiday = target.isoformat() in set(holidays)
    courses = state.get("semesters", {}).get(str(semester), {}).get("courses", {})
    events: list[dict[str, Any]] = []
    if isinstance(courses, Mapping) and not holiday and week > 0:
        for code, course in courses.items():
            if not isinstance(course, Mapping) or course.get("hidden"):
                continue
            module = modules.get(str(code))
            if not module:
                continue
            for lesson in resolve_course_lessons(module, semester, course):
                if lesson_occurs_on(lesson, target, week):
                    event = dict(lesson)
                    event.update(
                        {
                            "moduleCode": code,
                            "title": module.get("title"),
                            "isTa": bool(course.get("isTa")),
                        }
                    )
                    events.append(event)
    events.sort(key=lambda item: (str(item.get("startTime")), str(item.get("moduleCode"))))

    current = now or datetime.now(SINGAPORE_TZ)
    is_today = target == current.astimezone(SINGAPORE_TZ).date()
    if is_today:
        current_time = current.hour * 100 + current.minute
        events = [event for event in events if int(event.get("endTime") or 0) > current_time]
    return {
        "date": target.isoformat(),
        "weekday": target.strftime("%A"),
        "academicYear": academic_year,
        "semester": semester,
        "semesterName": SEMESTER_NAMES[semester],
        "week": week if week >= 1 else None,
        "holiday": holiday,
        "remainingOnly": is_today,
        "events": events,
    }


def decimal_credits(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)
