"""Canvas presentation fields; full cache records stay available via json-full."""

from __future__ import annotations

from typing import Any

from sk4n.tools.output import html_text, useful_fields

PATH_FIELDS = {"local_path", "path", "content", "html_url", "url"}
PERSON_FIELDS = {
    "id",
    "name",
    "short_name",
    "display_name",
    "email",
    "login_id",
    "sis_user_id",
    "pronouns",
    "html_url",
    "enrollments",
    "role",
}
TOPIC_FIELDS = PATH_FIELDS | {
    "id",
    "title",
    "message",
    "author",
    "user_name",
    "posted_at",
    "created_at",
    "updated_at",
    "last_reply_at",
    "assignment_id",
    "discussion_type",
    "is_announcement",
    "discussion_subentry_count",
    "unread_count",
    "read_state",
    "published",
    "locked",
    "locked_for_user",
    "lock_explanation",
    "attachments",
    "entries",
    "view",
    "participants",
    "unread_entries",
    "forced_entries",
    "require_initial_post",
    "user_can_see_posts",
    "comments_disabled",
    "pinned",
    "subscribed",
    "group_topic_children",
    "topic_children",
}
ASSIGNMENT_FIELDS = PATH_FIELDS | {
    "id",
    "name",
    "description",
    "course_id",
    "due_at",
    "lock_at",
    "unlock_at",
    "points_possible",
    "grading_type",
    "submission_types",
    "allowed_extensions",
    "allowed_attempts",
    "published",
    "workflow_state",
    "quiz_id",
    "locked_for_user",
    "lock_explanation",
    "lock_info",
    "submission",
    "rubric",
    "rubric_settings",
    "assignment_group_id",
    "group_category_id",
    "peer_reviews",
    "omit_from_final_grade",
    "has_submitted_submissions",
}
SUBMISSION_FIELDS = PATH_FIELDS | {
    "id",
    "user_id",
    "assignment_id",
    "attempt",
    "submission_type",
    "submitted_at",
    "body",
    "url",
    "score",
    "grade",
    "excused",
    "workflow_state",
    "late",
    "missing",
    "seconds_late",
    "graded_at",
    "preview_url",
    "attachments",
    "submission_comments",
    "rubric_assessment",
    "cached_due_date",
    "points_deducted",
    "late_policy_status",
}
SUBMISSION_HISTORY_FIELDS = {
    "attempt",
    "submitted_at",
    "submission_type",
    "workflow_state",
    "late",
    "missing",
    "seconds_late",
    "cached_due_date",
    "score",
    "grade",
    "graded_at",
    "url",
    "preview_url",
    "attachments",
}
FILE_FIELDS = PATH_FIELDS | {
    "id",
    "folder_id",
    "display_name",
    "filename",
    "content-type",
    "size",
    "updated_at",
    "modified_at",
    "locked",
    "locked_for_user",
    "hidden_for_user",
    "lock_explanation",
    "downloaded",
    "download_error",
    "error",
    "available",
    "unavailable",
    "reason",
    "content_type",
    "bytes",
    "external_url",
    "inaccessible",
    "access_error",
    "sources",
    "source",
}
GROUP_FIELDS = PATH_FIELDS | {
    "id",
    "name",
    "description",
    "group_category_id",
    "course_id",
    "context_name",
    "members_count",
    "users",
    "leader",
    "role",
    "is_current_user_member",
    "group_category",
    "is_full",
    "join_level",
}
COURSE_LIST_FIELDS = PATH_FIELDS | {
    "id",
    "name",
    "course_code",
    "enrollment_state",
    "enrollment_roles",
    "term_name",
    "term_folder_name",
    "metadata_path",
    "available_sections",
    "content",
    "availability_status",
    "availability_checked_at",
    "archived_data",
    "retired_at",
}
EVENT_FIELDS = PATH_FIELDS | {
    "id",
    "title",
    "description",
    "type",
    "start_at",
    "end_at",
    "all_day",
    "all_day_date",
    "context_code",
    "context_name",
    "course_id",
    "location_name",
    "location_address",
    "workflow_state",
    "assignment",
    "child_events",
    "parent_event_id",
    "rrule",
    "series_uuid",
}
QUIZ_FIELDS = PATH_FIELDS | {
    "id",
    "title",
    "description",
    "quiz_type",
    "time_limit",
    "allowed_attempts",
    "scoring_policy",
    "question_count",
    "points_possible",
    "due_at",
    "lock_at",
    "unlock_at",
    "published",
    "locked_for_user",
    "lock_explanation",
    "hide_results",
    "show_correct_answers",
    "show_correct_answers_at",
    "hide_correct_answers_at",
    "assignment_id",
    "question_types",
    "one_question_at_a_time",
    "cant_go_back",
    "has_access_code",
    "all_dates",
}
COURSE_FIELDS = PATH_FIELDS | {
    "id",
    "name",
    "course_code",
    "enrollment_state",
    "enrollment_roles",
    "workflow_state",
    "default_view",
    "start_at",
    "end_at",
    "time_zone",
    "term",
    "enrolled_sections",
    "enrollments",
    "syllabus_body",
}


def compact(value: Any) -> Any:
    if isinstance(value, list):
        return [compact(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "course_code" in value and "metadata_path" in value:
        value = useful_fields(value, COURSE_LIST_FIELDS)
    elif "course_code" in value and "name" in value:
        value = useful_fields(value, COURSE_FIELDS)
    elif "enrollments" in value and "name" in value and "course_code" not in value:
        value = useful_fields(value, PERSON_FIELDS | PATH_FIELDS)
    elif "discussion_type" in value or "is_announcement" in value:
        value = useful_fields(value, TOPIC_FIELDS)
    elif "submission_types" in value and "description" in value:
        value = useful_fields(value, ASSIGNMENT_FIELDS)
    elif "submission_type" in value and "attempt" in value:
        submission = useful_fields(value, SUBMISSION_FIELDS)
        if isinstance(value.get("submission_history"), list):
            submission["submission_history"] = [
                {
                    **useful_fields(attempt, SUBMISSION_HISTORY_FIELDS),
                    **{key: attempt[key] for key in ("attempt", "submitted_at") if key in attempt},
                }
                for attempt in value["submission_history"]
                if isinstance(attempt, dict)
            ]
        value = submission
    elif "filename" in value:
        value = useful_fields(value, FILE_FIELDS)
    elif "members_count" in value and "group_category_id" in value:
        value = useful_fields(value, GROUP_FIELDS)
    elif "start_at" in value and "end_at" in value and "title" in value:
        value = useful_fields(value, EVENT_FIELDS)
    elif "assignment" in value and "ignore_permanently" in value:
        value = useful_fields(
            value, {"type", "assignment", "course_id", "context_type", "context_name", "html_url"}
        )
    elif "quiz_type" in value and "question_count" in value:
        value = useful_fields(value, QUIZ_FIELDS)

    result = {}
    for key, item in value.items():
        if str(key).startswith("_canvas") or key in {
            "fingerprint",
            "raw",
            "raw_session_item",
            "dashboard",
            "all_tabs",
            "secure_params",
            "lti_context_id",
        }:
            continue
        if key == "content" and "metadata_path" in value and isinstance(item, dict):
            item = {
                name: useful_fields(section, {"available", "status", "count", "error"})
                if isinstance(section, dict)
                else section
                for name, section in item.items()
            }
        elif key == "available_sections" and isinstance(item, list):
            item = [
                useful_fields(section, {"id", "label", "type", "html_url"})
                for section in item
                if isinstance(section, dict)
            ]
        elif key == "new_quiz_result" and value.get("questions"):
            # Normalized questions already include answers and grading results.
            item = _quiz_result(item) if isinstance(item, dict) else item
        elif key == "quiz_review" and value.get("questions") and isinstance(item, dict):
            item = {name: field for name, field in item.items() if name != "summary_text"}
        elif key in {"author", "last_edited_by", "leader"} and isinstance(item, dict):
            item = useful_fields(item, PERSON_FIELDS)
        elif key == "users" and isinstance(item, list):
            item = [useful_fields(user, PERSON_FIELDS) for user in item if isinstance(user, dict)]
        elif key == "enrollments" and isinstance(item, list):
            item = [
                useful_fields(
                    enrollment,
                    {
                        "type",
                        "role",
                        "enrollment_state",
                        "course_id",
                        "course_section_id",
                    },
                )
                for enrollment in item
                if isinstance(enrollment, dict)
            ]
        elif key == "group_category" and isinstance(item, dict):
            item = useful_fields(
                item, {"id", "name", "self_signup", "self_signup_end_at", "group_limit"}
            )
        elif key in {"message", "body", "description", "syllabus_body"} and isinstance(item, str):
            item = html_text(item)
        result[key] = compact(item)
    if "question_html" in result:
        result["question_text"] = html_text(result.pop("question_html") or "") or result.get(
            "question_text", ""
        )
        result.pop("classes", None)
        if result.get("score"):
            result.pop("header_text", None)
    if "html" in result and "selected" in result and "correct" in result:
        result["text"] = html_text(result.pop("html") or "") or result.get("text", "")
        result.pop("classes", None)
    return result


def _quiz_result(value: dict[str, Any]) -> dict[str, Any]:
    result = useful_fields(value, {"preview_url", "title", "participant_result", "quiz_session"})
    for key, fields in (
        (
            "participant_result",
            {
                "attempt_history",
                "average_score",
                "completed_attempt_count",
                "max_attempts",
                "participant_session",
            },
        ),
        ("quiz_session", {"status", "attempt", "authoritative_result"}),
    ):
        if not isinstance(result.get(key), dict):
            continue
        nested = useful_fields(result[key], fields)
        for name in ("participant_session", "authoritative_result"):
            if isinstance(nested.get(name), dict):
                nested[name] = useful_fields(
                    nested[name],
                    {
                        "id",
                        "status",
                        "submitted_at",
                        "score_to_keep_wrt_multiple_attempts",
                        "score",
                        "points_possible",
                        "percentage",
                        "fudge_points",
                        "grading_method",
                        "requires_additional_review",
                    },
                )
        result[key] = nested
    return result
