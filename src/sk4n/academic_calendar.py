from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

SINGAPORE_TZ = timezone(timedelta(hours=8), name="Asia/Singapore")
SEMESTER_NAMES = {
    1: "Semester 1",
    2: "Semester 2",
}


def normalize_academic_year(value: str) -> tuple[str, str]:
    """Return an academic year as (display form, API path form)."""
    candidate = value.strip().replace("-", "/")
    parts = candidate.split("/")
    if len(parts) != 2 or not all(part.isdigit() and len(part) == 4 for part in parts):
        raise ValueError("Academic year must look like 2026/2027 or 2026-2027.")
    start, end = (int(part) for part in parts)
    if end != start + 1:
        raise ValueError("Academic year must contain consecutive years.")
    return f"{start:04d}/{end:04d}", f"{start:04d}-{end:04d}"


def current_academic_year(today: date | None = None) -> str:
    """Return the NUS academic year containing *today*."""
    current = today or datetime.now(SINGAPORE_TZ).date()
    start = current.year if current.month >= 7 else current.year - 1
    return f"{start:04d}/{start + 1:04d}"


def current_semester(today: date | None = None) -> int:
    current = today or datetime.now(SINGAPORE_TZ).date()
    return 1 if current.month >= 7 else 2


def _second_monday(year: int, month: int) -> date:
    first = date(year, month, 8)
    return first + timedelta(days=(7 - first.weekday()) % 7)


def semester_start(
    academic_year: str,
    semester: int,
    calendar: Mapping[str, Any] | None,
) -> date:
    display_year, _ = normalize_academic_year(academic_year)
    value = (
        calendar.get(display_year, {}).get(str(semester), {}).get("start")
        if isinstance(calendar, Mapping)
        else None
    )
    if isinstance(value, list) and len(value) == 3:
        return date(int(value[0]), int(value[1]), int(value[2]))
    start_year = int(display_year[:4])
    if semester == 1:
        return _second_monday(start_year, 8)
    if semester == 2:
        return _second_monday(start_year + 1, 1)
    raise ValueError("Only Semester 1 and Semester 2 schedules are supported.")


def semester_for_date(
    target: date,
    academic_year: str,
    calendar: Mapping[str, Any] | None,
) -> tuple[int, int]:
    display_year, _ = normalize_academic_year(academic_year)
    start_year = int(display_year[:4])
    if target < date(start_year, 7, 1) or target >= date(start_year + 1, 7, 1):
        raise ValueError(f"{target.isoformat()} is outside AY{display_year}.")
    semester = 1 if target.year == start_year and target.month >= 7 else 2
    start = semester_start(display_year, semester, calendar)
    calendar_week = (target - start).days // 7
    # NUSMods lesson week numbers count the 13 instructional weeks and skip
    # recess week. Its Today page also treats the weekend immediately before
    # recess/reading week as non-instructional.
    if 0 <= calendar_week <= 5:
        week = calendar_week + 1
        if calendar_week == 5 and target.weekday() >= 5:
            week = 0
    elif 7 <= calendar_week <= 13:
        week = calendar_week
        if calendar_week == 13 and target.weekday() >= 5:
            week = 0
    else:
        week = 0
    return semester, week


def academic_calendar_record(
    target: date,
    academic_year: str,
    calendar: Mapping[str, Any] | None,
    holidays: Iterable[str] = (),
) -> dict[str, Any]:
    """Describe the NUS semester and instructional week containing *target*."""
    display_year, _ = normalize_academic_year(academic_year)
    semester, week = semester_for_date(target, display_year, calendar)
    start = semester_start(display_year, semester, calendar)
    holiday = target.isoformat() in set(holidays)
    week_start = target - timedelta(days=target.weekday()) if week else None
    return {
        "date": target.isoformat(),
        "weekday": target.strftime("%A"),
        "academicYear": display_year,
        "semester": semester,
        "semesterName": SEMESTER_NAMES[semester],
        "semesterStart": start.isoformat(),
        "week": week or None,
        "weekStart": week_start.isoformat() if week_start else None,
        "weekEnd": (week_start + timedelta(days=6)).isoformat() if week_start else None,
        "instructional": week > 0 and not holiday,
        "holiday": holiday,
    }
