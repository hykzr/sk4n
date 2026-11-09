"""Compact discovery results while retaining complete course timetables."""

from __future__ import annotations

from typing import Any

from sk4n.tools.output import useful_fields

SEARCH_FIELDS = {
    "moduleCode",
    "title",
    "moduleCredit",
    "department",
    "faculty",
    "gradingBasisDescription",
    "semesterData",
    "attributes",
    "aliases",
}


def compact(value: Any, *, summary: bool = False) -> Any:
    if isinstance(value, list):
        return [compact(item, summary=summary) for item in value]
    if not isinstance(value, dict):
        return value
    if summary and "moduleCode" in value and "title" in value:
        value = useful_fields(value, SEARCH_FIELDS)
    result = {}
    for key, item in value.items():
        if key == "covidZones":
            continue
        # These machine rules duplicate the prerequisite/preclusion prose and tree.
        if key in {"prerequisiteRule", "preclusionRule"} and value.get(key.removesuffix("Rule")):
            continue
        result[key] = compact(item)
    return result
