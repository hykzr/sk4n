"""Job and company presentation without administrative configuration payloads."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from sk4n.tools.output import html_text, useful_fields

from .client import DEFAULT_APP_BASE_URL

JOB_FIELDS = {
    "_id",
    "slug",
    "title",
    "company",
    "employment_type",
    "job_type",
    "country_code",
    "city",
    "work_arrangement",
    "experience_level",
    "seniority_level",
    "role",
    "industry",
    "start_date",
    "end_date",
    "expired_at",
    "published_at",
    "updated_at",
    "currency",
    "salary_lower_range",
    "salary_upper_range",
    "salary_rate",
    "is_disclose_salary",
    "number_of_vacancies",
    "number_of_applicants",
    "application_type",
    "job_link",
    "is_appliable_via_kinobi",
    "status",
    "is_draft",
    "is_expired",
    "user_has_applied",
    "is_bookmarked",
    "is_unqualified_student",
    "is_ineligible_internship_programme_apply_job",
    "is_limited_apply_job",
    "internship_eligibility",
    "job_application_id",
    "job_application",
    "job_offer",
    "talent_connect_statuses",
    "job_applicant_rule_restriction",
    "record_type",
}
COMPANY_FIELDS = {
    "_id",
    "company_id",
    "slug",
    "name",
    "industry",
    "industries",
    "country_code",
    "address",
    "address_postal_code",
    "number_of_employees",
    "organization_type",
    "web_url",
    "linkedin_url",
    "updated_at",
    "record_type",
}
DETAIL_FIELDS = {
    "document_requirement_notes",
    "to_email_addresses",
    "cc_email_addresses",
    "internship_programmes",
    "related_work_term",
    "work_term",
    "job_additional_resource_assets",
}
WORKFLOW_FIELDS = {
    "_id",
    "id",
    "status",
    "response",
    "created_at",
    "updated_at",
    "submitted_at",
    "applied_at",
    "accepted_at",
    "rejected_at",
    "expired_at",
    "deadline",
    "start_date",
    "end_date",
    "remarks",
    "reason",
    "offer_letter",
    "offer_letter_url",
    "documents",
    "attachments",
}


def compact(value: Any, *, detail: bool = True) -> Any:
    if isinstance(value, list):
        return [compact(item, detail=detail) for item in value]
    if not isinstance(value, dict):
        return value
    if "title" in value and ("_id" in value or "slug" in value):
        result = useful_fields(value, JOB_FIELDS | (DETAIL_FIELDS if detail else set()))
        identifier = value.get("slug") or value.get("_id")
        if identifier:
            result["url"] = f"{DEFAULT_APP_BASE_URL}/jobs/{quote(str(identifier), safe='')}"
        if isinstance(value.get("company"), dict):
            result["company"] = useful_fields(
                value["company"], {"_id", "company_id", "slug", "name"}
            )
        for field in ("job_application", "job_offer"):
            if isinstance(result.get(field), dict):
                result[field] = useful_fields(result[field], WORKFLOW_FIELDS)
        if detail:
            _prose(value, result)
            for field in ("internship_programmes", "related_work_term"):
                if field in result:
                    result[field] = _named_records(result[field])
            if "eligible_internship_programme_versions" in value:
                result["eligible_internship_programme_versions"] = [
                    useful_fields(
                        item,
                        {
                            "_id",
                            "name",
                            "internship_programme_name",
                            "is_eligible",
                            "is_eligible_apart_from_job_applicant_rule",
                            "is_incomplete_prerequisite",
                            "is_restricted_by_job_applicant_rule",
                        },
                    )
                    for item in value["eligible_internship_programme_versions"] or []
                ]
        return result
    if "name" in value and ("company_id" in value or "web_url" in value):
        result = useful_fields(value, COMPANY_FIELDS)
        if detail and value.get("description"):
            result["description"] = html_text(str(value["description"]))
        return result
    return {
        key: compact(item, detail=False if key == "jobs" else detail) for key, item in value.items()
    }


def _prose(value: dict[str, Any], result: dict[str, Any]) -> None:
    description = value.get("description") or value.get("description_text")
    if description:
        result["description"] = html_text(str(description))
    for field in ("responsibilities", "requirements"):
        items = value.get(field) or value.get(f"{field}_text")
        if isinstance(items, list):
            result[field] = [
                html_text(str(item.get("text") or ""))
                if isinstance(item, dict)
                else html_text(str(item))
                for item in items
            ]


def _named_records(value: Any) -> Any:
    if isinstance(value, list):
        return [_named_records(item) for item in value]
    if isinstance(value, dict):
        return useful_fields(value, {"_id", "id", "name", "start_date", "end_date", "is_available"})
    return value
