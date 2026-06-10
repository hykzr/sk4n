from __future__ import annotations

import copy
import html as html_lib
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

QUIZ_IMAGE_DIR = "quiz_images"
QUIZ_IMAGE_METADATA_FILE = "quiz_images.json"

try:
    from .client import CanvasAPIError, CanvasClient, image_extension
    from .quiz_content import (
        fetch_quiz_content,
        is_new_quiz_submission,
        submitted_quiz_attempt,
    )
    from .utils import (
        COURSE_METADATA_FILE,
        content_file_path,
        download_with_fallbacks,
        existing_items_by_key,
        extract_file_ids_from_text,
        file_signature,
        fingerprint,
        normalize_existing_path,
        open_tab_ids,
        read_json,
        rel_path,
        resolve_relative_path,
        safe_path_segment,
        topic_status,
        unique_download_path,
        write_html,
        write_json,
    )
except ImportError:
    from client import CanvasAPIError, CanvasClient, image_extension
    from quiz_content import (
        fetch_quiz_content,
        is_new_quiz_submission,
        submitted_quiz_attempt,
    )
    from utils import (
        COURSE_METADATA_FILE,
        content_file_path,
        download_with_fallbacks,
        existing_items_by_key,
        extract_file_ids_from_text,
        file_signature,
        fingerprint,
        normalize_existing_path,
        open_tab_ids,
        read_json,
        rel_path,
        resolve_relative_path,
        safe_path_segment,
        topic_status,
        unique_download_path,
        write_html,
        write_json,
    )


def assignment_item_key(kind: str, item_id: Any) -> str:
    return f"{kind}:{item_id}"


def safe_folder_name(value: Any, fallback: str) -> str:
    name = safe_path_segment(value, fallback)
    if len(name) > 100:
        name = name[:100].strip(" ._")
    return name or fallback


def assignment_folder_for_item(
    *,
    assignments_root: Path,
    title: Any,
    item_id: str,
    used_dirs: set[Path],
) -> Path:
    base = safe_folder_name(title, f"assignment-{item_id}")
    candidate = assignments_root / base
    if candidate.resolve() not in used_dirs:
        used_dirs.add(candidate.resolve())
        return candidate
    candidate = assignments_root / f"{base} - {item_id}"
    counter = 2
    while candidate.resolve() in used_dirs:
        candidate = assignments_root / f"{base} - {item_id} ({counter})"
        counter += 1
    used_dirs.add(candidate.resolve())
    return candidate


def submission_signature_value(submission: Any) -> Any:
    if not isinstance(submission, dict):
        return submission
    selected = {
        key: submission.get(key)
        for key in (
            "id",
            "score",
            "grade",
            "attempt",
            "submission_type",
            "submitted_at",
            "body",
            "assignment_id",
            "workflow_state",
            "cached_due_date",
            "grade_matches_current_submission",
            "missing",
            "late",
            "attachments",
        )
        if key in submission
    }
    if isinstance(selected.get("attachments"), list):
        selected["attachments"] = [
            submitted_file_record_base(item)
            for item in selected["attachments"]  # type: ignore
            if isinstance(item, dict)
        ]
    return selected


def assignment_summary_signature(
    assignment: dict[str, Any],
    quiz_summary: dict[str, Any] | None = None,
) -> str:
    keys = (
        "id",
        "name",
        "description",
        "points_possible",
        "grading_type",
        "created_at",
        "updated_at",
        "due_at",
        "lock_at",
        "unlock_at",
        "assignment_group_id",
        "submission_types",
        "workflow_state",
        "published",
        "quiz_id",
        "is_quiz_assignment",
        "is_quiz_lti_assignment",
        "external_tool_tag_attributes",
        "has_submitted_submissions",
        "all_dates",
        "overrides",
        "score_statistics",
    )
    selected = {key: assignment.get(key) for key in keys if key in assignment}
    selected["submission"] = submission_signature_value(assignment.get("submission"))
    if quiz_summary:
        selected["quiz_summary"] = {
            key: quiz_summary.get(key)
            for key in (
                "id",
                "title",
                "description",
                "question_count",
                "points_possible",
                "due_at",
                "lock_at",
                "unlock_at",
                "published",
                "locked_for_user",
                "assignment_id",
            )
            if key in quiz_summary
        }
    return fingerprint(selected)


def quiz_summary_signature(quiz: dict[str, Any]) -> str:
    return fingerprint(
        {
            key: quiz.get(key)
            for key in (
                "id",
                "title",
                "description",
                "question_count",
                "points_possible",
                "due_at",
                "lock_at",
                "unlock_at",
                "published",
                "locked_for_user",
                "assignment_id",
                "assignment_group_id",
            )
            if key in quiz
        }
    )


def quiz_image_metadata_ok(quiz_path: Path, quiz_payload: dict[str, Any]) -> bool:
    quiz_images = quiz_payload.get("quiz_images")
    if not isinstance(quiz_images, dict):
        return False
    metadata_path = resolve_relative_path(quiz_path, quiz_images.get("path"))
    if not metadata_path or not metadata_path.exists():
        return False
    metadata = read_json(metadata_path)
    if not metadata or not isinstance(metadata.get("images"), dict):
        return False
    for record in metadata["images"].values():
        if not isinstance(record, dict) or not record.get("downloaded"):
            continue
        file_value = record.get("file") or record.get("path")
        image_path = resolve_relative_path(quiz_path, file_value)
        if not (image_path and image_path.exists() and record.get("sha256")):
            return False
    return True


def quiz_payload_needs_attempt_recheck(quiz_payload: dict[str, Any]) -> bool:
    error = quiz_payload.get("questions_error")
    return (
        quiz_payload.get("questions_available") is False
        and isinstance(error, str)
        and "no submitted student attempt was found" in error
    )


def existing_assignment_payload_ok(
    existing_item: dict[str, Any] | None,
    assignments_json_path: Path,
) -> bool:
    if not existing_item:
        return False
    assignment_json_path = resolve_relative_path(
        assignments_json_path, existing_item.get("path")
    )
    if not assignment_json_path or not assignment_json_path.exists():
        return False
    payload = read_json(assignment_json_path)
    if not payload:
        return False
    content_path = resolve_relative_path(assignment_json_path, payload.get("content"))
    if payload.get("content_present") and not (content_path and content_path.exists()):
        return False
    for image in payload.get("images") or []:
        if not isinstance(image, dict) or not image.get("downloaded"):
            continue
        image_path = resolve_relative_path(assignment_json_path, image.get("path"))
        if not (image_path and image_path.exists() and image.get("sha256")):
            return False
    for submitted_file in payload.get("submitted_files") or []:
        if not isinstance(submitted_file, dict) or not submitted_file.get("downloaded"):
            continue
        file_path = resolve_relative_path(
            assignment_json_path, submitted_file.get("path")
        )
        if not (file_path and file_path.exists() and submitted_file.get("sha256")):
            return False
    quiz = payload.get("quiz")
    if isinstance(quiz, dict) and quiz.get("path"):
        quiz_path = resolve_relative_path(assignment_json_path, quiz.get("path"))
        if not (quiz_path and quiz_path.exists()):
            return False
        quiz_payload = read_json(quiz_path)
        if not quiz_payload or not quiz_image_metadata_ok(quiz_path, quiz_payload):
            return False
        if quiz_payload_needs_attempt_recheck(quiz_payload):
            return False
    return True


def normalize_existing_assignment_item(
    existing_item: dict[str, Any],
    assignments_json_path: Path,
) -> dict[str, Any]:
    item = copy.deepcopy(existing_item)
    item["path"] = normalize_existing_path(
        json_path=assignments_json_path,
        target_path=item.get("path"),
        course_dir=assignments_json_path.parent,
    )
    if item.get("content"):
        item["content"] = normalize_existing_path(
            json_path=assignments_json_path,
            target_path=item.get("content"),
            course_dir=assignments_json_path.parent,
        )
    item["_canvas_sync"] = item.get("_canvas_sync", {})
    item["_canvas_sync"]["status"] = "unchanged"
    return item


def existing_assignment_dir(
    existing_item: dict[str, Any] | None,
    assignments_json_path: Path,
) -> Path | None:
    if not existing_item:
        return None
    assignment_json_path = resolve_relative_path(
        assignments_json_path, existing_item.get("path")
    )
    if not assignment_json_path:
        return None
    return assignment_json_path.parent


def course_metadata_has_staff_quiz_role(metadata: dict[str, Any] | None) -> bool:
    course = metadata.get("course") if isinstance(metadata, dict) else None
    sections = course.get("enrolled_sections") if isinstance(course, dict) else None
    if not isinstance(sections, list):
        return False
    roles = [
        str(section.get("enrollment_role") or section.get("role") or "")
        for section in sections
        if isinstance(section, dict)
    ]
    staff_markers = ("taenrollment", "teacherenrollment", "designerenrollment")
    return any(
        any(marker in role.casefold().replace("_", "") for marker in staff_markers)
        for role in roles
    )


def attach_quiz_assignment_summaries(
    *,
    client: CanvasClient,
    course_id: str,
    assignments: list[dict[str, Any]],
    quizzes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    assignment_ids = {
        str(item.get("id")) for item in assignments if item.get("id") is not None
    }
    errors: list[dict[str, Any]] = []
    for quiz in quizzes:
        assignment_id = quiz.get("assignment_id")
        if assignment_id is None or str(assignment_id) in assignment_ids:
            continue
        try:
            assignment = client.course_assignment_detail(course_id, assignment_id)
        except CanvasAPIError as exc:
            errors.append(
                {
                    "quiz_id": quiz.get("id"),
                    "assignment_id": assignment_id,
                    "error": str(exc),
                }
            )
            continue
        if assignment.get("name") == "Roll Call Attendance":
            continue
        assignment.setdefault("quiz_id", quiz.get("id"))
        assignments.append(assignment)
        assignment_ids.add(str(assignment_id))
    return errors


def absolute_canvas_url(client: CanvasClient, url: str) -> str:
    return urljoin(client.base_url.rstrip("/") + "/", url)


def image_output_path(
    *,
    assignment_dir: Path,
    src: str,
    index: int,
    used_paths: set[Path],
) -> Path:
    parsed = urlparse(src)
    original_name = Path(unquote(parsed.path)).name
    if not original_name or "." not in original_name:
        original_name = f"image-{index}.bin"
    base_name = safe_path_segment(original_name, f"image-{index}.bin")
    candidate = assignment_dir / "images" / base_name
    if candidate.resolve() not in used_paths:
        used_paths.add(candidate.resolve())
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        deduped = candidate.with_name(f"{stem}-{counter}{suffix}")
        if deduped.resolve() not in used_paths:
            used_paths.add(deduped.resolve())
            return deduped
        counter += 1


def download_assignment_images(
    *,
    client: CanvasClient,
    body: str,
    assignment_json_path: Path,
    assignment_dir: Path,
    existing_images: list[dict[str, Any]],
    force: bool,
) -> tuple[str, list[dict[str, Any]]]:
    if not body:
        return body, []
    soup = BeautifulSoup(body, "html.parser")
    existing_by_src = {
        str(image.get("original_src")): image
        for image in existing_images
        if isinstance(image, dict) and image.get("original_src")
    }
    used_paths: set[Path] = set()
    images: list[dict[str, Any]] = []
    for index, image in enumerate(soup.select("img[src]"), start=1):
        src = str(image.get("src") or "")
        if not src:
            continue
        existing = existing_by_src.get(src)
        existing_path = (
            resolve_relative_path(assignment_json_path, existing.get("path"))
            if existing and isinstance(existing.get("path"), str)
            else None
        )
        if (
            existing
            and not force
            and existing_path
            and existing_path.exists()
            and existing.get("sha256")
        ):
            record = copy.deepcopy(existing)
            record["_canvas_sync"] = (
                record.get("_canvas_sync", {})
                if isinstance(record.get("_canvas_sync"), dict)
                else {}
            )
            record["_canvas_sync"]["status"] = "unchanged"
            image["src"] = record["path"]
            used_paths.add(existing_path.resolve())
            images.append(record)
            continue

        output_path = image_output_path(
            assignment_dir=assignment_dir,
            src=src,
            index=index,
            used_paths=used_paths,
        )
        record: dict[str, Any] = {
            "original_src": src,
            "path": rel_path(assignment_json_path, output_path),
        }
        try:
            result = client.download_file(absolute_canvas_url(client, src), output_path)
        except CanvasAPIError as exc:
            record.update({"downloaded": False, "error": str(exc)})
        else:
            record.update(
                {
                    "downloaded": True,
                    "sha256": result["sha256"],
                    "bytes_downloaded": result["bytes"],
                    "download_content_type": result.get("content_type"),
                }
            )
            image["src"] = record["path"]
        record["_canvas_sync"] = {
            "signature": fingerprint(src),
            "status": topic_status(existing),
        }
        images.append(record)
    return str(soup), images


def reserve_quiz_image_path(candidate: Path, used_paths: set[Path]) -> Path:
    if candidate.resolve() not in used_paths:
        used_paths.add(candidate.resolve())
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        deduped = candidate.with_name(f"{stem}-{counter}{suffix}")
        if deduped.resolve() not in used_paths:
            used_paths.add(deduped.resolve())
            return deduped
        counter += 1


def quiz_image_extension(content_type: str | None, url: str) -> str | None:
    media_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if media_type == "image/svg+xml":
        return ".svg"
    extension = image_extension(media_type, url)
    if extension:
        return extension
    path = url.split("?", 1)[0].lower()
    if path.endswith(".svg"):
        return ".svg"
    return None


def quiz_image_content_type_is_image(content_type: str | None) -> bool:
    media_type = str(content_type or "").split(";", 1)[0].strip().lower()
    return (
        not media_type
        or media_type.startswith("image/")
        or media_type == "application/octet-stream"
    )


def quiz_image_output_path(
    *,
    quiz_path: Path,
    src: str,
    index: int,
    used_paths: set[Path],
) -> Path:
    parsed = urlparse(src)
    original_name = Path(unquote(parsed.path)).name
    extension = quiz_image_extension(None, src)
    if not original_name or "." not in original_name:
        original_name = f"image-{index}{extension or '.bin'}"
    base_name = safe_path_segment(original_name, f"image-{index}.bin")
    if len(base_name) > 120:
        suffix = Path(base_name).suffix
        stem = Path(base_name).stem[: max(1, 120 - len(suffix))].strip(" ._")
        base_name = f"{stem or f'image-{index}'}{suffix}"
    return reserve_quiz_image_path(
        quiz_path.parent / QUIZ_IMAGE_DIR / base_name,
        used_paths,
    )


def existing_quiz_image_path(
    quiz_path: Path,
    record: dict[str, Any] | None,
) -> Path | None:
    if not isinstance(record, dict):
        return None
    file_value = record.get("file") or record.get("path")
    if not isinstance(file_value, str):
        return None
    return resolve_relative_path(quiz_path, file_value)


def rename_quiz_image_for_content_type(
    *,
    output_path: Path,
    content_type: str | None,
    url: str,
    used_paths: set[Path],
) -> Path:
    extension = quiz_image_extension(content_type, url)
    if not extension or output_path.suffix.lower() == extension:
        return output_path
    used_paths.discard(output_path.resolve())
    candidate = reserve_quiz_image_path(output_path.with_suffix(extension), used_paths)
    output_path.replace(candidate)
    return candidate


def is_local_quiz_image_src(src: str) -> bool:
    stripped = src.strip()
    if not stripped or stripped.startswith("#"):
        return True
    parsed = urlparse(stripped)
    if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
        return True
    return stripped.startswith(
        (
            f"{QUIZ_IMAGE_DIR}/",
            f"./{QUIZ_IMAGE_DIR}/",
            f"../{QUIZ_IMAGE_DIR}/",
        )
    )


def quiz_image_metadata_payload(images_by_url: dict[str, dict[str, Any]]) -> dict[str, Any]:
    images = {
        url: images_by_url[url]
        for url in sorted(images_by_url)
        if isinstance(images_by_url[url], dict)
    }
    downloaded_count = sum(1 for record in images.values() if record.get("downloaded"))
    return {
        "schema_version": 1,
        "content_type": "quiz_images",
        "image_count": len(images),
        "downloaded_count": downloaded_count,
        "failed_count": len(images) - downloaded_count,
        "images": images,
    }


def quiz_image_metadata_summary(
    quiz_path: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    metadata_path = quiz_path.parent / QUIZ_IMAGE_METADATA_FILE
    return {
        "path": rel_path(quiz_path, metadata_path),
        "image_count": metadata.get("image_count", 0),
        "downloaded_count": metadata.get("downloaded_count", 0),
        "failed_count": metadata.get("failed_count", 0),
    }


def rewrite_quiz_artifact_images(
    *,
    client: CanvasClient,
    quiz_path: Path,
    artifact: Any,
    force: bool,
    base_url: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    metadata_path = quiz_path.parent / QUIZ_IMAGE_METADATA_FILE
    existing_metadata = read_json(metadata_path)
    existing_images = (
        existing_metadata.get("images")
        if isinstance(existing_metadata, dict)
        and isinstance(existing_metadata.get("images"), dict)
        else {}
    )
    existing_by_url: dict[str, dict[str, Any]] = {
        str(url): copy.deepcopy(record)
        for url, record in existing_images.items()
        if isinstance(record, dict)
    }
    images_by_url: dict[str, dict[str, Any]] = {}
    image_dir = quiz_path.parent / QUIZ_IMAGE_DIR
    used_paths: set[Path] = set()
    if image_dir.exists():
        used_paths.update(
            path.resolve() for path in image_dir.iterdir() if path.is_file()
        )
    for record in existing_by_url.values():
        path = existing_quiz_image_path(quiz_path, record)
        if path:
            used_paths.add(path.resolve())

    absolute_base_url = (base_url or client.base_url).rstrip("/") + "/"
    counter = len(existing_by_url)

    def local_image_path_for_src(src: str) -> str | None:
        nonlocal counter
        if is_local_quiz_image_src(src):
            return None
        absolute_src = urljoin(absolute_base_url, src)
        parsed = urlparse(absolute_src)
        if parsed.scheme.lower() not in {"http", "https"}:
            return None

        current = images_by_url.get(absolute_src)
        current_path = existing_quiz_image_path(quiz_path, current)
        if current and current_path and current_path.exists() and current.get("sha256"):
            return str(current.get("file") or current.get("path"))
        if current and current.get("downloaded") is False:
            return None

        existing = existing_by_url.get(absolute_src)
        existing_path = existing_quiz_image_path(quiz_path, existing)
        if (
            existing
            and not force
            and existing_path
            and existing_path.exists()
            and existing.get("sha256")
        ):
            record = copy.deepcopy(existing)
            record["_canvas_sync"] = (
                record.get("_canvas_sync", {})
                if isinstance(record.get("_canvas_sync"), dict)
                else {}
            )
            record["_canvas_sync"]["status"] = "unchanged"
            images_by_url[absolute_src] = record
            return str(record.get("file") or record.get("path"))

        if existing_path:
            output_path = existing_path
            used_paths.add(output_path.resolve())
        else:
            counter += 1
            output_path = quiz_image_output_path(
                quiz_path=quiz_path,
                src=absolute_src,
                index=counter,
                used_paths=used_paths,
            )

        try:
            result = client.download_file(absolute_src, output_path)
            content_type = result.get("content_type")
            if not quiz_image_content_type_is_image(content_type):
                try:
                    output_path.unlink()
                except OSError:
                    pass
                raise CanvasAPIError(
                    f"Quiz image URL returned non-image content: {content_type}"
                )
            output_path = rename_quiz_image_for_content_type(
                output_path=output_path,
                content_type=content_type,
                url=absolute_src,
                used_paths=used_paths,
            )
        except CanvasAPIError as exc:
            if (
                existing
                and existing_path
                and existing_path.exists()
                and existing.get("sha256")
            ):
                record = copy.deepcopy(existing)
                record["refresh_error"] = str(exc)
                record["_canvas_sync"] = (
                    record.get("_canvas_sync", {})
                    if isinstance(record.get("_canvas_sync"), dict)
                    else {}
                )
                record["_canvas_sync"]["status"] = "unchanged"
                images_by_url[absolute_src] = record
                return str(record.get("file") or record.get("path"))
            images_by_url[absolute_src] = {
                "file": None,
                "original_src": src,
                "download_url": absolute_src,
                "downloaded": False,
                "error": str(exc),
                "_canvas_sync": {
                    "signature": fingerprint(absolute_src),
                    "status": "error",
                },
            }
            return None

        record = {
            "file": rel_path(quiz_path, output_path),
            "original_src": src,
            "download_url": absolute_src,
            "downloaded": True,
            "sha256": result["sha256"],
            "bytes_downloaded": result["bytes"],
            "download_content_type": result.get("content_type"),
            "_canvas_sync": {
                "signature": fingerprint(absolute_src),
                "status": topic_status(existing),
            },
        }
        images_by_url[absolute_src] = record
        return record["file"]

    def rewrite_html(value: str) -> str:
        soup = BeautifulSoup(value, "html.parser")
        changed = False
        for image in soup.select("img[src]"):
            src = str(image.get("src") or "")
            replacement = local_image_path_for_src(src)
            if replacement:
                image["src"] = replacement
                changed = True
        return str(soup) if changed else value

    def rewrite_value(value: Any, seen: dict[int, Any]) -> Any:
        if isinstance(value, dict):
            marker = id(value)
            if marker in seen:
                return seen[marker]
            result: dict[str, Any] = {}
            seen[marker] = result
            for key, item in value.items():
                result[key] = rewrite_value(item, seen)
            return result
        if isinstance(value, list):
            marker = id(value)
            if marker in seen:
                return seen[marker]
            result_list: list[Any] = []
            seen[marker] = result_list
            result_list.extend(rewrite_value(item, seen) for item in value)
            return result_list
        if (
            isinstance(value, str)
            and "<img" in value.lower()
            and "src" in value.lower()
        ):
            return rewrite_html(value)
        return value

    rewritten = rewrite_value(artifact, {})
    metadata = quiz_image_metadata_payload(images_by_url)
    write_json(metadata_path, metadata)
    return rewritten, metadata


def quiz_title_for_html(
    *,
    quiz_detail: dict[str, Any] | None,
    new_quiz_result: Any,
) -> str:
    if isinstance(quiz_detail, dict):
        for key in ("title", "name"):
            value = quiz_detail.get(key)
            if value:
                return str(value)
    if isinstance(new_quiz_result, dict) and new_quiz_result.get("title"):
        return str(new_quiz_result["title"])
    return "Canvas quiz review"


def quiz_html_fragment(value: Any, fallback: str = "") -> str:
    if isinstance(value, str) and value.strip():
        return value
    return html_lib.escape(fallback)


def question_result_score(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    score = result.get("score")
    possible = result.get("points_possible")
    if score is None and possible is None:
        return None
    if possible is None:
        return f"Score: {score}"
    if score is None:
        return f"Points possible: {possible}"
    return f"Score: {score} / {possible}"


def selected_new_quiz_choice_ids(question: dict[str, Any]) -> set[str]:
    result = question.get("result")
    scored_data = result.get("scored_data") if isinstance(result, dict) else None
    values = scored_data.get("value") if isinstance(scored_data, dict) else None
    if not isinstance(values, dict):
        return set()
    return {
        str(choice_id)
        for choice_id, value in values.items()
        if isinstance(value, dict) and value.get("user_responded")
    }


def render_new_quiz_response_values(question: dict[str, Any]) -> str:
    result = question.get("result")
    scored_data = result.get("scored_data") if isinstance(result, dict) else None
    values = scored_data.get("value") if isinstance(scored_data, dict) else None
    if not isinstance(values, dict):
        return ""
    rows = []
    for key, value in values.items():
        if not isinstance(value, dict):
            continue
        user_response = value.get("user_response")
        correct_answer = value.get("correct_answer")
        if user_response is None and correct_answer is None:
            continue
        rows.append(
            "<tr>"
            f"<th>{html_lib.escape(str(key))}</th>"
            f"<td>{html_lib.escape('' if user_response is None else str(user_response))}</td>"
            f"<td>{html_lib.escape('' if correct_answer is None else str(correct_answer))}</td>"
            "</tr>"
        )
    if not rows:
        return ""
    return (
        '<table class="quiz-response-values">'
        "<thead><tr><th>Blank</th><th>Your answer</th><th>Correct answer</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def render_quiz_answers(question: dict[str, Any]) -> str:
    answers = question.get("answers")
    if isinstance(answers, list) and answers:
        items = []
        for answer in answers:
            if not isinstance(answer, dict):
                continue
            classes = ["quiz-answer"]
            labels = []
            if answer.get("selected"):
                classes.append("selected")
                labels.append("selected")
            if answer.get("correct"):
                classes.append("correct")
                labels.append("correct")
            if answer.get("incorrect"):
                classes.append("incorrect")
                labels.append("incorrect")
            body = quiz_html_fragment(answer.get("html"), str(answer.get("text") or ""))
            label_html = (
                f'<span class="quiz-answer-label">{html_lib.escape(", ".join(labels))}</span>'
                if labels
                else ""
            )
            items.append(
                f'<li class="{" ".join(classes)}">{body}{label_html}</li>'
            )
        return f'<ol class="quiz-answers">{"".join(items)}</ol>' if items else ""

    choices = question.get("choices")
    if not isinstance(choices, list) or not choices:
        return render_new_quiz_response_values(question)
    selected_ids = selected_new_quiz_choice_ids(question)
    items = []
    for choice in sorted(
        (item for item in choices if isinstance(item, dict)),
        key=lambda item: item.get("position") or 0,
    ):
        choice_id = str(choice.get("id") or "")
        classes = ["quiz-answer"]
        labels = []
        if choice_id and choice_id in selected_ids:
            classes.append("selected")
            labels.append("selected")
        body = quiz_html_fragment(choice.get("item_body"), choice_id)
        label_html = (
            f'<span class="quiz-answer-label">{html_lib.escape(", ".join(labels))}</span>'
            if labels
            else ""
        )
        items.append(f'<li class="{" ".join(classes)}">{body}{label_html}</li>')
    return f'<ol class="quiz-answers">{"".join(items)}</ol>'


def render_quiz_preview_html(
    *,
    questions: list[dict[str, Any]],
    source: str | None,
) -> str:
    style = """
<style>
.quiz-preview { color: #1f2933; font-family: Arial, sans-serif; line-height: 1.45; max-width: 960px; }
.quiz-preview-note { color: #52606d; margin-bottom: 1.5rem; }
.quiz-question { border-top: 1px solid #d9e2ec; padding: 1.25rem 0; }
.quiz-question h2 { font-size: 1.1rem; margin: 0 0 0.5rem; }
.quiz-meta { color: #52606d; font-size: 0.92rem; margin-bottom: 0.75rem; }
.quiz-question-body img { height: auto; max-width: 100%; }
.quiz-answers { list-style-position: outside; margin: 0.75rem 0 0 1.5rem; padding: 0; }
.quiz-answer { border-left: 3px solid transparent; margin: 0.35rem 0; padding: 0.35rem 0.5rem; }
.quiz-answer.selected { border-left-color: #2563eb; background: #eff6ff; }
.quiz-answer.correct { border-left-color: #16a34a; background: #f0fdf4; }
.quiz-answer.incorrect { border-left-color: #dc2626; background: #fef2f2; }
.quiz-answer-label { color: #334e68; display: inline-block; font-size: 0.85rem; margin-left: 0.5rem; }
.quiz-response-values { border-collapse: collapse; margin-top: 0.75rem; width: 100%; }
.quiz-response-values th, .quiz-response-values td { border: 1px solid #d9e2ec; padding: 0.4rem 0.5rem; text-align: left; }
</style>
"""
    sections = []
    for index, question in enumerate(questions, start=1):
        if not isinstance(question, dict):
            continue
        number = question.get("question_number") or question.get("position") or index
        title = html_lib.escape(str(question.get("title") or f"Question {number}"))
        type_value = question.get("type")
        score = question_result_score(question.get("result")) or (
            f"Points possible: {html_lib.escape(str(question['points_possible']))}"
            if question.get("points_possible") is not None
            else None
        )
        meta_parts = [
            html_lib.escape(str(item))
            for item in (type_value, score)
            if item is not None and str(item)
        ]
        meta_html = (
            f'<div class="quiz-meta">{" | ".join(meta_parts)}</div>'
            if meta_parts
            else ""
        )
        body = quiz_html_fragment(
            question.get("question_html"),
            str(question.get("question_text") or ""),
        )
        answers = render_quiz_answers(question)
        sections.append(
            '<section class="quiz-question">'
            f"<h2>Question {html_lib.escape(str(number))}: {title}</h2>"
            f"{meta_html}"
            f'<div class="quiz-question-body">{body}</div>'
            f"{answers}"
            "</section>"
        )
    source_text = html_lib.escape(source or "Canvas quiz result data")
    return (
        f"{style}"
        '<div class="quiz-preview">'
        f'<p class="quiz-preview-note">Generated from {source_text}; Canvas did not provide a static review HTML page for this attempt.</p>'
        f"{''.join(sections)}"
        "</div>"
    )


def submitted_attachment_key(attachment: dict[str, Any], index: int) -> str:
    if attachment.get("id") is not None:
        return str(attachment["id"])
    if attachment.get("url"):
        return str(attachment["url"])
    return f"attachment-{index}"


def submitted_file_record_base(attachment: dict[str, Any]) -> dict[str, Any]:
    return {
        key: attachment.get(key)
        for key in (
            "id",
            "folder_id",
            "display_name",
            "filename",
            "content-type",
            "size",
            "created_at",
            "updated_at",
            "modified_at",
            "unlock_at",
            "lock_at",
            "locked",
            "hidden",
            "locked_for_user",
            "hidden_for_user",
        )
        if key in attachment
    }


def collect_submission_attachments(value: Any) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    if isinstance(value, dict):
        raw_attachments = value.get("attachments")
        if isinstance(raw_attachments, list):
            attachments.extend(
                item for item in raw_attachments if isinstance(item, dict)
            )
        for item in value.values():
            attachments.extend(collect_submission_attachments(item))
    elif isinstance(value, list):
        for item in value:
            attachments.extend(collect_submission_attachments(item))
    return attachments


def annotate_submission_attachments(
    value: Any, records_by_key: dict[str, dict[str, Any]]
) -> Any:
    if isinstance(value, list):
        return [annotate_submission_attachments(item, records_by_key) for item in value]
    if not isinstance(value, dict):
        return value

    result: dict[str, Any] = {}
    for key, item in value.items():
        if key == "attachments" and isinstance(item, list):
            annotated = []
            for index, attachment in enumerate(item, start=1):
                if not isinstance(attachment, dict):
                    annotated.append(attachment)
                    continue
                attachment_key = submitted_attachment_key(attachment, index)
                local_record = records_by_key.get(attachment_key)
                if local_record:
                    annotated.append(copy.deepcopy(local_record))
                else:
                    fallback = submitted_file_record_base(attachment)
                    fallback["downloaded"] = False
                    annotated.append(fallback)
            result[key] = annotated
        else:
            result[key] = annotate_submission_attachments(item, records_by_key)
    return result


def download_submitted_files(
    *,
    client: CanvasClient,
    submission: dict[str, Any] | None,
    assignment_json_path: Path,
    assignment_dir: Path,
    existing_files: list[dict[str, Any]],
    force: bool,
) -> tuple[Any, list[dict[str, Any]]]:
    if not isinstance(submission, dict):
        return submission, []
    existing_by_key = {
        str(item.get("id") or item.get("_canvas_sync", {}).get("attachment_key")): item
        for item in existing_files
        if isinstance(item, dict)
    }
    used_paths: set[Path] = set()
    records: list[dict[str, Any]] = []
    records_by_key: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for index, attachment in enumerate(
        collect_submission_attachments(submission), start=1
    ):
        attachment_key = submitted_attachment_key(attachment, index)
        if attachment_key in seen:
            continue
        seen.add(attachment_key)
        signature = file_signature(attachment)
        existing = existing_by_key.get(attachment_key)
        existing_signature = (
            existing.get("_canvas_sync", {}).get("signature")
            if isinstance(existing, dict)
            else None
        )
        existing_path = (
            resolve_relative_path(assignment_json_path, existing.get("path"))
            if existing and isinstance(existing.get("path"), str)
            else None
        )
        if (
            existing
            and not force
            and existing_signature == signature
            and existing_path
            and existing_path.exists()
            and existing.get("sha256")
        ):
            record = copy.deepcopy(existing)
            record["_canvas_sync"]["status"] = "unchanged"
            used_paths.add(existing_path.resolve())
            records.append(record)
            records_by_key[attachment_key] = record
            continue

        output_path = unique_download_path(
            files_root=assignment_dir / "submitted_files",
            folder_path=Path(),
            file_item=attachment,
            used_paths=used_paths,
        )
        record = submitted_file_record_base(attachment)
        record["path"] = rel_path(assignment_json_path, output_path)
        try:
            result = download_with_fallbacks(client, attachment, output_path)
        except CanvasAPIError as exc:
            record.update(
                {
                    "downloaded": False,
                    "download_error": str(exc),
                    "sha256": existing.get("sha256") if existing else None,
                    "bytes_downloaded": 0,
                }
            )
            status = "error"
        else:
            record.update(
                {
                    "downloaded": True,
                    "sha256": result["sha256"],
                    "bytes_downloaded": result["bytes"],
                    "download_content_type": result.get("content_type"),
                }
            )
            status = topic_status(existing)
        record["_canvas_sync"] = {
            "attachment_key": attachment_key,
            "signature": signature,
            "status": status,
        }
        records.append(record)
        records_by_key[attachment_key] = record
    return annotate_submission_attachments(submission, records_by_key), records


def quiz_payload_for_assignment(
    *,
    client: CanvasClient,
    course_id: str,
    quiz_id: str | None,
    assignment_json_path: Path,
    force: bool,
    existing_quiz_path: str | None,
    submission: dict[str, Any] | None,
    skip_quiz_content: bool,
    quiz_detail_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quiz_path = assignment_json_path.parent / "quiz.json"
    quiz_error = None
    quiz_detail: dict[str, Any] | None = copy.deepcopy(quiz_detail_hint)
    if quiz_id is not None and quiz_detail is None:
        try:
            quiz_detail = client.course_quiz_detail(course_id, quiz_id)
        except CanvasAPIError as exc:
            quiz_error = str(exc)

    content_result = fetch_quiz_content(
        client=client,
        course_id=course_id,
        quiz_id=str(quiz_id or ""),
        quiz_detail=quiz_detail,
        submission=submission,
        skip_for_ta=skip_quiz_content,
    )
    questions_error = content_result.error
    artifact, image_metadata = rewrite_quiz_artifact_images(
        client=client,
        quiz_path=quiz_path,
        artifact={
            "questions": content_result.questions,
            "api_questions": content_result.api_questions,
            "review": copy.deepcopy(content_result.review),
            "new_quiz_result": content_result.new_quiz_result,
        },
        force=force,
        base_url=client.base_url,
    )
    questions = (
        artifact.get("questions")
        if isinstance(artifact, dict) and isinstance(artifact.get("questions"), list)
        else []
    )
    api_questions = (
        artifact.get("api_questions")
        if isinstance(artifact, dict)
        else content_result.api_questions
    )
    review = (
        artifact.get("review")
        if isinstance(artifact, dict) and isinstance(artifact.get("review"), dict)
        else None
    )
    new_quiz_result = (
        artifact.get("new_quiz_result")
        if isinstance(artifact, dict)
        else content_result.new_quiz_result
    )
    quiz_submission = (
        copy.deepcopy(submission)
        if isinstance(submission, dict)
        and submission.get("_canvas_sync_source") == "canvas_quiz_submissions_self"
        else None
    )
    quiz_images = quiz_image_metadata_summary(quiz_path, image_metadata)
    review_path = quiz_path.parent / "quiz.html"
    if review and review.get("html"):
        review_html = str(review.pop("html"))
        write_html(
            review_path,
            quiz_title_for_html(
                quiz_detail=quiz_detail,
                new_quiz_result=new_quiz_result,
            ),
            review_html,
        )
        review["content"] = rel_path(quiz_path, review_path)
        review["generated"] = False
    elif questions:
        preview_html = render_quiz_preview_html(
            questions=questions,
            source=content_result.source,
        )
        write_html(
            review_path,
            quiz_title_for_html(
                quiz_detail=quiz_detail,
                new_quiz_result=new_quiz_result,
            ),
            preview_html,
        )
        if not isinstance(review, dict):
            review = {}
        review.update(
            {
                "content": rel_path(quiz_path, review_path),
                "generated": True,
                "source": content_result.source,
            }
        )

    payload = {
        "schema_version": 4,
        "content_type": "quiz",
        "quiz_id": quiz_id,
        "quiz": quiz_detail,
        "quiz_error": quiz_error,
        "questions_available": content_result.available,
        "question_source": content_result.source,
        "question_count": len(questions),
        "questions_error": questions_error,
        "questions": questions,
        "api_questions": api_questions,
        "quiz_review": review,
        "new_quiz_result": new_quiz_result,
        "quiz_submission": quiz_submission,
        "quiz_images": quiz_images,
        "fingerprint": fingerprint(
            {
                "quiz": quiz_detail,
                "quiz_error": quiz_error,
                "questions": questions,
                "questions_error": questions_error,
                "question_source": content_result.source,
                "quiz_review": review,
                "api_questions": api_questions,
                "new_quiz_result": new_quiz_result,
                "quiz_submission": quiz_submission,
                "quiz_images": quiz_images,
            }
        ),
    }
    write_json(quiz_path, payload)
    return {
        "path": rel_path(assignment_json_path, quiz_path),
        "status": "updated" if existing_quiz_path else "created",
        "question_count": len(questions),
        "questions_available": content_result.available,
        "question_source": content_result.source,
        "questions_error": questions_error,
        "quiz_image_count": quiz_images.get("downloaded_count", 0),
    }


def build_assignment_record(
    *,
    client: CanvasClient,
    course_id: str,
    assignments_json_path: Path,
    assignment_dir: Path,
    summary: dict[str, Any],
    existing_item: dict[str, Any] | None,
    synced_at: str,
    force: bool,
    quiz_summary: dict[str, Any] | None = None,
    skip_quiz_content: bool = False,
) -> tuple[dict[str, Any], bool]:
    assignment_id = str(summary["id"])
    assignment_json_path = assignment_dir / "assignment.json"
    existing_payload = read_json(assignment_json_path)
    detail = copy.deepcopy(summary)
    detail_error = None
    try:
        detail = client.course_assignment_detail(course_id, assignment_id)
    except CanvasAPIError as exc:
        detail_error = str(exc)

    quiz_id = detail.get("quiz_id") or summary.get("quiz_id")
    quiz_detail_for_body = None
    if quiz_id is not None:
        try:
            quiz_detail_for_body = client.course_quiz_detail(course_id, quiz_id)
        except CanvasAPIError:
            quiz_detail_for_body = None

    body_parts = []
    assignment_body = detail.get("description")
    if isinstance(assignment_body, str) and assignment_body:
        body_parts.append(assignment_body)
    quiz_body = (
        quiz_detail_for_body.get("description")
        if isinstance(quiz_detail_for_body, dict)
        else None
    )
    if isinstance(quiz_body, str) and quiz_body and quiz_body not in body_parts:
        body_parts.append(quiz_body)
    body = "\n".join(body_parts)

    existing_images = (
        existing_payload.get("images", [])
        if isinstance(existing_payload, dict)
        and isinstance(existing_payload.get("images"), list)
        else []
    )
    body, images = download_assignment_images(
        client=client,
        body=body,
        assignment_json_path=assignment_json_path,
        assignment_dir=assignment_dir,
        existing_images=existing_images,
        force=force,
    )
    content_path = assignment_dir / "content.html"
    content_rel = rel_path(assignment_json_path, content_path)
    if body:
        write_html(content_path, str(detail.get("name") or summary.get("name")), body)
        detail["description"] = content_rel
        if isinstance(quiz_detail_for_body, dict) and quiz_detail_for_body.get(
            "description"
        ):
            quiz_detail_for_body["description"] = content_rel
    else:
        content_rel = None
        detail["description"] = None

    self_submission = None
    self_submission_error = None
    try:
        self_submission = client.assignment_self_submission(course_id, assignment_id)
    except CanvasAPIError as exc:
        self_submission_error = str(exc)

    submission_source = self_submission
    if not isinstance(submission_source, dict) and isinstance(
        detail.get("submission"), dict
    ):
        submission_source = detail["submission"]
    existing_submitted = (
        existing_payload.get("submitted_files", [])
        if isinstance(existing_payload, dict)
        and isinstance(existing_payload.get("submitted_files"), list)
        else []
    )
    sanitized_submission, submitted_files = download_submitted_files(
        client=client,
        submission=submission_source,
        assignment_json_path=assignment_json_path,
        assignment_dir=assignment_dir,
        existing_files=existing_submitted,
        force=force,
    )
    if isinstance(self_submission, dict):
        self_submission = sanitized_submission
    if isinstance(detail.get("submission"), dict):
        detail["submission"] = annotate_submission_attachments(
            detail["submission"],
            {
                str(
                    item.get("id") or item.get("_canvas_sync", {}).get("attachment_key")
                ): item
                for item in submitted_files
            },
        )

    quiz_info = None
    if quiz_id is not None:
        existing_quiz_path = (
            existing_payload.get("quiz", {}).get("path")
            if isinstance(existing_payload, dict)
            and isinstance(existing_payload.get("quiz"), dict)
            else None
        )
        quiz_info = quiz_payload_for_assignment(
            client=client,
            course_id=course_id,
            quiz_id=str(quiz_id),
            assignment_json_path=assignment_json_path,
            force=force,
            existing_quiz_path=existing_quiz_path,
            submission=submission_source if isinstance(submission_source, dict) else None,
            skip_quiz_content=skip_quiz_content,
            quiz_detail_hint=quiz_detail_for_body,
        )
    elif isinstance(submission_source, dict) and is_new_quiz_submission(
        submission_source
    ):
        existing_quiz_path = (
            existing_payload.get("quiz", {}).get("path")
            if isinstance(existing_payload, dict)
            and isinstance(existing_payload.get("quiz"), dict)
            else None
        )
        quiz_info = quiz_payload_for_assignment(
            client=client,
            course_id=course_id,
            quiz_id=None,
            assignment_json_path=assignment_json_path,
            force=force,
            existing_quiz_path=existing_quiz_path,
            submission=submission_source,
            skip_quiz_content=skip_quiz_content,
            quiz_detail_hint=None,
        )

    signature = assignment_summary_signature(summary, quiz_summary)
    file_ids = sorted(extract_file_ids_from_text(body))
    payload = {
        "schema_version": 2,
        "synced_at": synced_at,
        "course_id": course_id,
        "content_type": "assignment",
        "kind": "assignment",
        "id": int(assignment_id) if assignment_id.isdigit() else assignment_id,
        "name": detail.get("name") or summary.get("name"),
        "content": content_rel,
        "content_present": bool(body),
        "images": images,
        "referenced_file_ids": file_ids,
        "submitted_files": submitted_files,
        "self_submission": self_submission,
        "self_submission_error": self_submission_error,
        "assignment": detail,
        "assignment_detail_error": detail_error,
        "quiz": quiz_info,
        "change_detection": {
            "canvas_fields": "assignment list metadata, included submission summary, and quiz summary when available",
            "strategy": "skip detail fetch when signature matches and local files exist",
        },
        "fingerprint": fingerprint(
            {
                "signature": signature,
                "file_ids": file_ids,
                "submitted_files": [
                    item.get("sha256")
                    for item in submitted_files
                    if item.get("downloaded")
                ],
                "images": [
                    item.get("sha256") for item in images if item.get("downloaded")
                ],
                "quiz": quiz_info,
            }
        ),
        "_canvas_sync": {
            "signature": signature,
            "status": topic_status(existing_item),
        },
    }
    write_json(assignment_json_path, payload)
    item = {
        "key": assignment_item_key("assignment", assignment_id),
        "kind": "assignment",
        "id": payload["id"],
        "name": payload["name"],
        "path": rel_path(assignments_json_path, assignment_json_path),
        "content": rel_path(assignments_json_path, content_path) if body else None,
        "submission_types": detail.get("submission_types")
        or summary.get("submission_types"),
        "due_at": detail.get("due_at") or summary.get("due_at"),
        "points_possible": detail.get("points_possible")
        or summary.get("points_possible"),
        "quiz_id": quiz_id,
        "submitted_file_count": len(submitted_files),
        "referenced_file_ids": file_ids,
        "_canvas_sync": {
            "signature": signature,
            "status": topic_status(existing_item),
        },
    }
    return item, True


def build_standalone_quiz_record(
    *,
    client: CanvasClient,
    course_id: str,
    assignments_json_path: Path,
    assignment_dir: Path,
    quiz: dict[str, Any],
    existing_item: dict[str, Any] | None,
    synced_at: str,
    force: bool,
    skip_quiz_content: bool = False,
) -> tuple[dict[str, Any], bool]:
    quiz_id = str(quiz["id"])
    assignment_json_path = assignment_dir / "assignment.json"
    existing_payload = read_json(assignment_json_path)
    detail = copy.deepcopy(quiz)
    detail_error = None
    try:
        detail = client.course_quiz_detail(course_id, quiz_id)
    except CanvasAPIError as exc:
        detail_error = str(exc)

    body = (
        detail.get("description", "")
        if isinstance(detail.get("description"), str)
        else ""
    )
    existing_images = (
        existing_payload.get("images", [])
        if isinstance(existing_payload, dict)
        and isinstance(existing_payload.get("images"), list)
        else []
    )
    body, images = download_assignment_images(
        client=client,
        body=body,
        assignment_json_path=assignment_json_path,
        assignment_dir=assignment_dir,
        existing_images=existing_images,
        force=force,
    )
    content_path = assignment_dir / "content.html"
    content_rel = rel_path(assignment_json_path, content_path)
    if body:
        write_html(content_path, str(detail.get("title") or quiz.get("title")), body)
        detail["description"] = content_rel
    else:
        content_rel = None
        detail["description"] = None

    quiz_submission = None
    quiz_submission_error = None
    if not skip_quiz_content:
        try:
            quiz_submission_response = client.course_quiz_self_submission(
                course_id, quiz_id
            )
            quiz_submission = submitted_quiz_attempt(quiz_submission_response)
        except CanvasAPIError as exc:
            quiz_submission_error = str(exc)

    existing_quiz_path = (
        existing_payload.get("quiz", {}).get("path")
        if isinstance(existing_payload, dict)
        and isinstance(existing_payload.get("quiz"), dict)
        else None
    )
    quiz_info = quiz_payload_for_assignment(
        client=client,
        course_id=course_id,
        quiz_id=quiz_id,
        assignment_json_path=assignment_json_path,
        force=force,
        existing_quiz_path=existing_quiz_path,
        submission=quiz_submission,
        skip_quiz_content=skip_quiz_content,
        quiz_detail_hint=detail,
    )
    signature = quiz_summary_signature(quiz)
    file_ids = sorted(extract_file_ids_from_text(body))
    payload = {
        "schema_version": 2,
        "synced_at": synced_at,
        "course_id": course_id,
        "content_type": "assignment",
        "kind": "quiz",
        "id": int(quiz_id) if quiz_id.isdigit() else quiz_id,
        "name": detail.get("title") or quiz.get("title"),
        "content": content_rel,
        "content_present": bool(body),
        "images": images,
        "referenced_file_ids": file_ids,
        "submitted_files": [],
        "quiz_submission": quiz_submission,
        "quiz_submission_error": quiz_submission_error,
        "quiz_detail_error": detail_error,
        "quiz_detail": detail,
        "quiz": quiz_info,
        "change_detection": {
            "canvas_fields": "quiz list metadata and quiz self-submission summary",
            "strategy": "skip detail fetch when signature matches and local files exist",
        },
        "fingerprint": fingerprint(
            {
                "signature": signature,
                "quiz_submission": quiz_submission,
                "quiz_submission_error": quiz_submission_error,
                "file_ids": file_ids,
                "images": [
                    item.get("sha256") for item in images if item.get("downloaded")
                ],
                "quiz": quiz_info,
            }
        ),
        "_canvas_sync": {
            "signature": signature,
            "status": topic_status(existing_item),
        },
    }
    write_json(assignment_json_path, payload)
    item = {
        "key": assignment_item_key("quiz", quiz_id),
        "kind": "quiz",
        "id": payload["id"],
        "name": payload["name"],
        "path": rel_path(assignments_json_path, assignment_json_path),
        "content": rel_path(assignments_json_path, content_path) if body else None,
        "due_at": detail.get("due_at") or quiz.get("due_at"),
        "points_possible": detail.get("points_possible") or quiz.get("points_possible"),
        "quiz_id": payload["id"],
        "submitted_file_count": 0,
        "referenced_file_ids": file_ids,
        "_canvas_sync": {
            "signature": signature,
            "status": topic_status(existing_item),
        },
    }
    return item, True


def sync_assignments(
    *,
    client: CanvasClient,
    course_id: str,
    course_dir: Path,
    tabs: list[dict[str, Any]],
    synced_at: str,
    force: bool,
    course_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    json_path = content_file_path(course_dir, "assignments")
    assignments_root = json_path.parent
    existing = read_json(json_path)
    existing_by_key = existing_items_by_key(existing, "key")
    available_tabs = open_tab_ids(tabs)
    has_assignments_tab = "assignments" in available_tabs
    has_quizzes_tab = "quizzes" in available_tabs
    if course_metadata is None:
        course_metadata = read_json(course_dir / COURSE_METADATA_FILE)
    skip_quiz_content = course_metadata_has_staff_quiz_role(course_metadata)

    assignments: list[dict[str, Any]] = []
    assignments_error = None
    if has_assignments_tab:
        try:
            assignments = [
                item
                for item in client.course_assignments(course_id)
                if item.get("name") != "Roll Call Attendance"
            ]
        except CanvasAPIError as exc:
            assignments_error = str(exc)

    quizzes: list[dict[str, Any]] = []
    quizzes_error = None
    if has_quizzes_tab:
        try:
            quizzes = client.course_quizzes(course_id)
        except CanvasAPIError as exc:
            quizzes_error = str(exc)

    quiz_assignment_errors: list[dict[str, Any]] = []
    if not skip_quiz_content:
        quiz_assignment_errors = attach_quiz_assignment_summaries(
            client=client,
            course_id=course_id,
            assignments=assignments,
            quizzes=quizzes,
        )

    if not assignments and not quizzes:
        return {
            "available": has_assignments_tab or has_quizzes_tab,
            "checked": True,
            "fetched": False,
            "status": (
                "closed"
                if not (has_assignments_tab or has_quizzes_tab)
                else "unchanged"
            ),
            "path": rel_path(course_dir / COURSE_METADATA_FILE, json_path),
            "count": 0,
            "assignments_error": assignments_error,
            "quizzes_error": quizzes_error,
            "quiz_assignment_errors": quiz_assignment_errors,
        }

    quiz_by_id = {
        str(quiz.get("id")): quiz for quiz in quizzes if quiz.get("id") is not None
    }
    assignment_ids = {
        str(item.get("id")) for item in assignments if item.get("id") is not None
    }
    linked_quiz_ids = {
        str(item.get("quiz_id"))
        for item in assignments
        if item.get("quiz_id") is not None
    }
    changed = force or existing is None
    changed_items = 0
    submitted_file_count = 0
    image_count = 0
    items: list[dict[str, Any]] = []
    used_dirs: set[Path] = set()

    for assignment in assignments:
        assignment_id = str(assignment.get("id"))
        key = assignment_item_key("assignment", assignment_id)
        quiz_summary = quiz_by_id.get(str(assignment.get("quiz_id")))
        signature = assignment_summary_signature(assignment, quiz_summary)
        existing_item = existing_by_key.get(key)
        existing_signature = (
            existing_item.get("_canvas_sync", {}).get("signature")
            if isinstance(existing_item, dict)
            else None
        )
        if (
            existing_item
            and not force
            and existing_signature == signature
            and existing_assignment_payload_ok(existing_item, json_path)
        ):
            item = normalize_existing_assignment_item(existing_item, json_path)
            existing_dir = existing_assignment_dir(existing_item, json_path)
            if existing_dir:
                used_dirs.add(existing_dir.resolve())
            items.append(item)
            submitted_file_count += int(item.get("submitted_file_count") or 0)
            continue

        assignment_dir = existing_assignment_dir(existing_item, json_path)
        if assignment_dir:
            used_dirs.add(assignment_dir.resolve())
        else:
            assignment_dir = assignment_folder_for_item(
                assignments_root=assignments_root,
                title=assignment.get("name"),
                item_id=assignment_id,
                used_dirs=used_dirs,
            )
        item, _ = build_assignment_record(
            client=client,
            course_id=course_id,
            assignments_json_path=json_path,
            assignment_dir=assignment_dir,
            summary=assignment,
            existing_item=existing_item,
            synced_at=synced_at,
            force=force,
            quiz_summary=quiz_summary,
            skip_quiz_content=skip_quiz_content,
        )
        items.append(item)
        submitted_file_count += int(item.get("submitted_file_count") or 0)
        changed = True
        changed_items += 1

    for quiz in quizzes:
        quiz_id = str(quiz.get("id"))
        assignment_id = str(quiz.get("assignment_id") or "")
        if quiz_id in linked_quiz_ids or assignment_id in assignment_ids:
            continue
        key = assignment_item_key("quiz", quiz_id)
        signature = quiz_summary_signature(quiz)
        existing_item = existing_by_key.get(key)
        existing_signature = (
            existing_item.get("_canvas_sync", {}).get("signature")
            if isinstance(existing_item, dict)
            else None
        )
        if (
            existing_item
            and not force
            and existing_signature == signature
            and existing_assignment_payload_ok(existing_item, json_path)
        ):
            existing_dir = existing_assignment_dir(existing_item, json_path)
            if existing_dir:
                used_dirs.add(existing_dir.resolve())
            items.append(normalize_existing_assignment_item(existing_item, json_path))
            continue

        assignment_dir = existing_assignment_dir(existing_item, json_path)
        if assignment_dir:
            used_dirs.add(assignment_dir.resolve())
        else:
            assignment_dir = assignment_folder_for_item(
                assignments_root=assignments_root,
                title=quiz.get("title"),
                item_id=quiz_id,
                used_dirs=used_dirs,
            )
        item, _ = build_standalone_quiz_record(
            client=client,
            course_id=course_id,
            assignments_json_path=json_path,
            assignment_dir=assignment_dir,
            quiz=quiz,
            existing_item=existing_item,
            synced_at=synced_at,
            force=force,
            skip_quiz_content=skip_quiz_content,
        )
        items.append(item)
        changed = True
        changed_items += 1

    for item in items:
        assignment_json_path = resolve_relative_path(json_path, item.get("path"))
        payload = read_json(assignment_json_path) if assignment_json_path else None
        if payload:
            image_count += sum(
                1 for image in payload.get("images") or [] if image.get("downloaded")
            )

    fingerprint_value = fingerprint(
        [item.get("_canvas_sync", {}).get("signature") for item in items]
    )
    if existing and existing.get("fingerprint") != fingerprint_value:
        changed = True
    payload = {
        "schema_version": 2,
        "synced_at": synced_at,
        "course_id": course_id,
        "content_type": "assignments",
        "count": len(items),
        "assignment_count": len(assignments),
        "standalone_quiz_count": sum(1 for item in items if item.get("kind") == "quiz"),
        "ignored_roll_call": True,
        "submitted_file_count": submitted_file_count,
        "image_count": image_count,
        "assignments_available": has_assignments_tab,
        "quizzes_available": has_quizzes_tab,
        "assignments_error": assignments_error,
        "quizzes_error": quizzes_error,
        "quiz_assignment_errors": quiz_assignment_errors,
        "fingerprint": fingerprint_value,
        "change_detection": {
            "canvas_fields": [
                "assignment updated_at/dates/points/submission_types/description",
                "included self-submission summary",
                "quiz list metadata when available",
            ],
            "strategy": "skip detail, quiz question, image, and submitted-file fetches when signatures match local files",
        },
        "items": items,
    }
    if changed:
        write_json(json_path, payload)
    return {
        "available": True,
        "checked": True,
        "fetched": changed,
        "status": (
            "created" if existing is None else ("updated" if changed else "unchanged")
        ),
        "path": rel_path(course_dir / COURSE_METADATA_FILE, json_path),
        "count": len(items),
        "changed_items": changed_items,
        "submitted_file_count": submitted_file_count,
    }
