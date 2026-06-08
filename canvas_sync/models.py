from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MULTISPACE = re.compile(r"\s+")
ACADEMIC_TERM = re.compile(
    r"(?P<start>\d{4})\s*/\s*(?P<end>\d{4})\s+Semester\s+(?P<semester>\d+)",
    re.IGNORECASE,
)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def card_course_id(card: dict[str, Any]) -> str | None:
    for key in ("id", "course_id"):
        value = card.get(key)
        if value is not None:
            return str(value)
    asset = str(card.get("assetString") or card.get("asset_string") or "")
    match = re.search(r"course_(\d+)", asset)
    if match:
        return match.group(1)
    return None


def sanitize_folder_name(name: str, fallback: str) -> str:
    cleaned = INVALID_PATH_CHARS.sub("_", name)
    cleaned = MULTISPACE.sub(" ", cleaned).strip(" .")
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or fallback


def normalize_term_name(term_name: str | None) -> str:
    if not term_name:
        return "Unknown Term"
    term_name = MULTISPACE.sub(" ", term_name).strip()
    match = ACADEMIC_TERM.search(term_name)
    if match:
        start = match.group("start")[-2:]
        end = match.group("end")[-2:]
        semester = match.group("semester")
        return f"{start}{end}S{semester}"
    return sanitize_folder_name(term_name, fallback="Unknown Term")


def compact_term(term: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(term, dict):
        return None
    return {
        key: term.get(key)
        for key in ("id", "name", "start_at", "end_at", "workflow_state")
        if key in term
    }


def compact_section(section: dict[str, Any]) -> dict[str, Any]:
    return {
        key: section.get(key)
        for key in ("id", "name", "start_at", "end_at", "enrollment_role")
        if key in section
    }


def compact_tab(tab: dict[str, Any], base_url: str) -> dict[str, Any]:
    html_url = tab.get("html_url")
    if isinstance(html_url, str) and html_url.startswith("/"):
        html_url = base_url.rstrip("/") + html_url
    hidden = bool(tab.get("hidden"))
    return {
        "id": tab.get("id"),
        "label": tab.get("label"),
        "type": tab.get("type"),
        "available": not hidden,
        "hidden": tab.get("hidden"),
        "visibility": tab.get("visibility"),
        "html_url": html_url,
        "url": tab.get("url"),
    }


@dataclass
class CourseRecord:
    id: str
    course: dict[str, Any] | None = None
    favorite_course: dict[str, Any] | None = None
    dashboard_card: dict[str, Any] | None = None
    sources: list[str] | None = None

    def __post_init__(self) -> None:
        if self.sources is None:
            self.sources = []

    def add_source(self, source: str, item: dict[str, Any]) -> None:
        if source not in self.sources:
            self.sources.append(source)
        if source in {"course", "active_course", "past_course"}:
            self.course = item
        elif source == "favorite_course":
            self.favorite_course = item
        elif source == "dashboard_card":
            self.dashboard_card = item

    @property
    def name(self) -> str | None:
        for item, keys in (
            (self.course, ("name",)),
            (self.favorite_course, ("name",)),
            (self.dashboard_card, ("longName", "originalName", "shortName")),
        ):
            if not item:
                continue
            for key in keys:
                value = item.get(key)
                if value:
                    return str(value)
        return None

    @property
    def course_code(self) -> str | None:
        for item, keys in (
            (self.course, ("course_code",)),
            (self.favorite_course, ("course_code",)),
            (self.dashboard_card, ("courseCode", "shortName")),
        ):
            if not item:
                continue
            for key in keys:
                value = item.get(key)
                if value:
                    return str(value)
        return None

    @property
    def cover_image_url(self) -> str | None:
        for item, keys in (
            (self.course, ("image_download_url", "course_image")),
            (self.favorite_course, ("image_download_url", "course_image")),
            (self.dashboard_card, ("image",)),
        ):
            if not item:
                continue
            for key in keys:
                value = item.get(key)
                if value:
                    return str(value)
        return None

    @property
    def term_name(self) -> str | None:
        for item in (self.course, self.favorite_course):
            term = item.get("term") if item else None
            if isinstance(term, dict) and term.get("name"):
                return str(term["name"])
        if self.dashboard_card and self.dashboard_card.get("term"):
            return str(self.dashboard_card["term"])
        return None

    @property
    def enrollment_state(self) -> str | None:
        if self.course and self.course.get("_canvas_sync_enrollment_state"):
            return str(self.course["_canvas_sync_enrollment_state"])
        return None

    def term_folder_base_name(self) -> str:
        return normalize_term_name(self.term_name)

    def folder_base_name(self) -> str:
        label = self.course_code or self.name or f"course-{self.id}"
        return sanitize_folder_name(label, fallback=f"course-{self.id}")

    def to_json_record(
        self,
        *,
        base_url: str,
        term_folder_name: str,
        folder_name: str,
        tabs: list[dict[str, Any]],
        synced_at: str,
    ) -> dict[str, Any]:
        course = self.course or self.favorite_course or {}
        sections = course.get("sections")
        if not isinstance(sections, list):
            sections = []
        compact_tabs = [compact_tab(tab, base_url) for tab in tabs]
        available_sections = [tab for tab in compact_tabs if tab["available"]]
        cover_image_url = self.cover_image_url
        card = self.dashboard_card or {}
        return {
            "schema_version": 1,
            "synced_at": synced_at,
            "base_url": base_url,
            "term_folder_name": term_folder_name,
            "folder_name": folder_name,
            "sources": self.sources,
            "course": {
                "id": self.id,
                "name": self.name,
                "course_code": self.course_code,
                "enrollment_state": self.enrollment_state,
                "workflow_state": course.get("workflow_state"),
                "default_view": course.get("default_view"),
                "start_at": course.get("start_at"),
                "end_at": course.get("end_at"),
                "time_zone": course.get("time_zone"),
                "term": compact_term(course.get("term")),
                "enrolled_sections": [
                    compact_section(section)
                    for section in sections
                    if isinstance(section, dict)
                ],
            },
            "cover_image": {
                "present": bool(cover_image_url),
                "url": cover_image_url,
            },
            "dashboard": {
                "color": card.get("color"),
                "href": card.get("href"),
                "position": card.get("position"),
                "published": card.get("published"),
                "is_favorited": card.get("isFavorited"),
            },
            "available_sections": available_sections,
            "all_tabs": compact_tabs,
        }


def merge_course_records(
    courses: list[dict[str, Any]],
    favorite_courses: list[dict[str, Any]],
    dashboard_cards: list[dict[str, Any]],
) -> list[CourseRecord]:
    records: dict[str, CourseRecord] = {}

    def ensure(course_id: str) -> CourseRecord:
        return records.setdefault(course_id, CourseRecord(id=course_id))

    for item in courses:
        course_id = item.get("id")
        if course_id is not None:
            source = str(item.get("_canvas_sync_source") or "course")
            ensure(str(course_id)).add_source(source, item)

    for item in favorite_courses:
        course_id = item.get("id")
        if course_id is not None:
            ensure(str(course_id)).add_source("favorite_course", item)

    for item in dashboard_cards:
        course_id = card_course_id(item)
        if course_id is not None:
            ensure(str(course_id)).add_source("dashboard_card", item)

    return sorted(records.values(), key=lambda record: (record.course_code or record.name or "", record.id))


def unique_folder_names(records: list[CourseRecord]) -> dict[str, str]:
    counts: dict[str, int] = {}
    bases = [record.folder_base_name() for record in records]
    for base in bases:
        counts[base.casefold()] = counts.get(base.casefold(), 0) + 1

    used: set[str] = set()
    mapping: dict[str, str] = {}
    for record, base in zip(records, bases, strict=True):
        folder_name = base
        if counts[base.casefold()] > 1:
            folder_name = f"{base} - {record.id}"
        candidate = folder_name
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{folder_name} ({suffix})"
            suffix += 1
        used.add(candidate.casefold())
        mapping[record.id] = candidate
    return mapping


def unique_course_folder_names_by_term(records: list[CourseRecord]) -> dict[str, str]:
    grouped: dict[str, list[CourseRecord]] = {}
    for record in records:
        grouped.setdefault(record.term_folder_base_name().casefold(), []).append(record)

    mapping: dict[str, str] = {}
    for group_records in grouped.values():
        mapping.update(unique_folder_names(group_records))
    return mapping


def path_for_course(root: Path, term_folder_name: str, folder_name: str) -> Path:
    return root / term_folder_name / folder_name
