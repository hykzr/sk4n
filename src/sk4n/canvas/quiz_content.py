from __future__ import annotations

import asyncio
import copy
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from playwright.async_api import async_playwright

from sk4n.tools.shared import load_session

from .client import CanvasAPIError, CanvasClient

QUIZ_TAKE_PATH = re.compile(r"/quizzes/\d+/(?:take|take_questions)(?:/|$)")
QUESTION_ID = re.compile(r"question_(\d+)")
ANSWER_ID = re.compile(r"answer[_-](.+)")
SCORE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*/\s*([+-]?\d+(?:\.\d+)?)")


@dataclass
class QuizContentResult:
    questions: list[dict[str, Any]]
    source: str | None
    error: str | None = None
    review: dict[str, Any] | None = None
    api_questions: dict[str, Any] | None = None
    new_quiz_result: dict[str, Any] | None = None

    @property
    def available(self) -> bool:
        return self.error is None and bool(self.questions)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def class_names(node: Tag) -> list[str]:
    value = node.get("class")
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return value.split()
    return []


def inner_html(node: Tag | None) -> str | None:
    if not node:
        return None
    return "".join(str(child) for child in node.contents).strip()


def absolutize_html_fragment(fragment: str | None, base_url: str) -> str | None:
    if not fragment:
        return fragment
    soup = BeautifulSoup(fragment, "html.parser")
    for tag in soup.select("[href], [src]"):
        for attr in ("href", "src"):
            value = tag.get(attr)
            if isinstance(value, str) and value:
                tag[attr] = urljoin(base_url, value)
    return str(soup)


def input_records(node: Tag) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for element in node.select("input, textarea, select"):
        record = {
            key: element.get(key)
            for key in ("id", "name", "type", "value", "aria-label")
            if element.get(key) is not None
        }
        if element.has_attr("checked"):
            record["checked"] = True  # type: ignore
        if element.has_attr("selected"):
            record["selected"] = True  # type: ignore
        records.append(record)
    return records


def answer_record(node: Tag, base_url: str) -> dict[str, Any]:
    classes = class_names(node)
    answer_text = node.select_one(".answer_text")
    raw_id = str(node.get("id") or "")
    match = ANSWER_ID.search(raw_id)
    html = inner_html(answer_text) or inner_html(node)
    return {
        "id": match.group(1) if match else raw_id or None,
        "classes": classes,
        "selected": "selected_answer" in classes,
        "correct": "correct_answer" in classes,
        "incorrect": "wrong_answer" in classes,
        "title": node.get("title"),
        "text": clean_text(
            answer_text.get_text(" ", strip=True) if answer_text else node.get_text(" ", strip=True)
        ),
        "html": absolutize_html_fragment(html, base_url),
        "inputs": input_records(node),
    }


def parse_question_score(text: str) -> dict[str, float] | None:
    match = SCORE.search(text)
    if not match:
        return None
    return {
        "score": float(match.group(1)),
        "points_possible": float(match.group(2)),
    }


def question_record(node: Tag, base_url: str, position: int) -> dict[str, Any]:
    classes = class_names(node)
    raw_id = str(node.get("id") or "")
    question_id = QUESTION_ID.search(raw_id)
    header = node.select_one(".header")
    question_text = node.select_one(".question_text")
    comments = [
        clean_text(comment.get_text(" ", strip=True))
        for comment in node.select(".quiz_comment")
        if clean_text(comment.get_text(" ", strip=True))
    ]
    header_text = clean_text(header.get_text(" ", strip=True)) if header else ""
    return {
        "id": question_id.group(1) if question_id else raw_id or None,
        "position": position,
        "name": clean_text(node.select_one(".question_name").get_text(" ", strip=True))  # type: ignore
        if node.select_one(".question_name")
        else None,
        "type": next((item for item in classes if item.endswith("_question")), None),
        "classes": classes,
        "status": {
            "correct": "correct" in classes,
            "incorrect": "incorrect" in classes or "wrong" in classes,
        },
        "score": parse_question_score(header_text),
        "header_text": header_text,
        "question_text": clean_text(question_text.get_text(" ", strip=True))
        if question_text
        else "",
        "question_html": absolutize_html_fragment(inner_html(question_text), base_url),
        "answers": [
            answer_record(answer, base_url) for answer in node.select(".answers .answer, .answer")
        ],
        "inputs": input_records(node),
        "comments": comments,
    }


def parse_classic_quiz_review(html: str, base_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    container = (
        soup.select_one(".quiz-submission")
        or soup.select_one("#quiz_show")
        or soup.select_one("body")
    )
    questions = []
    seen: set[int] = set()
    if container:
        for node in container.select(".display_question"):
            marker = id(node)
            if marker in seen:
                continue
            seen.add(marker)
            questions.append(question_record(node, base_url, len(questions) + 1))

    review_html = inner_html(container) if container else None
    return {
        "title": clean_text(soup.title.get_text(" ", strip=True)) if soup.title else None,
        "questions": questions,
        "html": absolutize_html_fragment(review_html, base_url),
        "summary_text": clean_text(container.get_text(" ", strip=True)) if container else "",
    }


def safe_get_quiz_html(
    client: CanvasClient,
    url: str,
    *,
    max_redirects: int = 5,
) -> tuple[str, str]:
    current_url = url
    for _ in range(max_redirects + 1):
        response = client.get_response(current_url, allow_redirects=False)
        if 300 <= response.status_code < 400:
            location = response.headers.get("location")
            if not location:
                raise CanvasAPIError(f"Canvas redirected {current_url} without a Location header.")
            next_url = urljoin(current_url, location)
            path = urlparse(next_url).path
            if QUIZ_TAKE_PATH.search(path):
                raise CanvasAPIError(
                    "Canvas redirected this quiz to a quiz-taking URL; skipped to avoid opening an unstarted attempt."
                )
            current_url = next_url
            continue
        content_type = response.headers.get("content-type", "")
        if "html" not in content_type.lower():
            raise CanvasAPIError(f"Canvas returned non-HTML quiz review content for {current_url}.")
        return response.text, response.url
    raise CanvasAPIError(f"Canvas redirected too many times while fetching {url}.")


def has_submitted_attempt(submission: Any) -> bool:
    if not isinstance(submission, dict):
        return False
    if submission.get("workflow_state") == "unsubmitted":
        return False
    return any(
        bool(submission.get(key))
        for key in ("submitted_at", "finished_at", "result_url", "preview_url")
    )


def submitted_quiz_attempt(response: Any) -> dict[str, Any] | None:
    submissions = (
        response.get("quiz_submissions")
        if isinstance(response, dict) and isinstance(response.get("quiz_submissions"), list)
        else None
    )
    if submissions is None:
        submissions = [response] if isinstance(response, dict) else []
    attempts = [
        item for item in submissions if isinstance(item, dict) and has_submitted_attempt(item)
    ]
    if not attempts:
        return None
    attempts.sort(
        key=lambda item: (
            str(item.get("finished_at") or item.get("submitted_at") or ""),
            int(item.get("attempt") or 0),
        )
    )
    attempt = copy.deepcopy(attempts[-1])
    attempt["_canvas_source"] = "canvas_quiz_submissions_self"
    return attempt


def quiz_attempt_review_url(submission: Any) -> str | None:
    if not isinstance(submission, dict):
        return None
    for key in ("result_url", "html_url"):
        value = submission.get(key)
        if isinstance(value, str) and value:
            path = urlparse(value).path
            if not QUIZ_TAKE_PATH.search(path):
                return value
    return None


def is_new_quiz_submission(submission: Any) -> bool:
    if not has_submitted_attempt(submission):
        return False
    if submission.get("submission_type") != "basic_lti_launch":
        return False
    for key in ("url", "external_tool_url", "preview_url"):
        value = submission.get(key)
        if isinstance(value, str) and "quiz-lti" in value:
            return True
    return False


def submitted_attempt_error() -> str:
    return (
        "Quiz content was not fetched because no submitted student attempt was found; "
        "skipped to avoid opening an unstarted quiz."
    )


def ta_skip_error() -> str:
    return (
        "Quiz content was not fetched for this course because the enrollment role "
        "appears to be TA/staff; skipped to avoid using elevated quiz access."
    )


def safe_error(parts: list[str | None]) -> str:
    return " ".join(part for part in parts if part).strip()


def fetch_classic_quiz_content(
    *,
    client: CanvasClient,
    course_id: str,
    quiz_id: str,
    quiz_detail: dict[str, Any] | None,
    submission: dict[str, Any] | None,
    api_questions: list[dict[str, Any]],
    api_error: str | None,
) -> QuizContentResult:
    html_url = quiz_attempt_review_url(submission) or (
        quiz_detail.get("html_url")
        if isinstance(quiz_detail, dict) and isinstance(quiz_detail.get("html_url"), str)
        else client.api_url(f"/courses/{course_id}/quizzes/{quiz_id}")
    )
    assert isinstance(html_url, str) and html_url, "No quiz review URL was available."
    review: dict[str, Any] = {"url": html_url}
    try:
        html, final_url = safe_get_quiz_html(client, html_url)
    except CanvasAPIError as exc:
        if api_questions:
            return QuizContentResult(
                questions=api_questions,
                source="canvas_quiz_questions_api",
                api_questions={
                    "available": True,
                    "question_count": len(api_questions),
                    "error": api_error,
                },
                review={"url": html_url, "error": str(exc)},
            )
        return QuizContentResult(
            questions=[],
            source=None,
            error=safe_error(
                [
                    api_error,
                    str(exc),
                    "No readable submitted quiz review page was available.",
                ]
            ),
            api_questions={
                "available": False,
                "question_count": 0,
                "error": api_error,
            },
            review={"url": html_url, "error": str(exc)},
        )

    parsed = parse_classic_quiz_review(html, final_url)
    questions = parsed["questions"]
    review.update(
        {
            "final_url": final_url,
            "title": parsed.get("title"),
            "question_count": len(questions),
            "summary_text": parsed.get("summary_text"),
            "html": parsed.get("html"),
        }
    )
    if questions:
        return QuizContentResult(
            questions=questions,
            source="canvas_quiz_review_html",
            api_questions={
                "available": bool(api_questions),
                "question_count": len(api_questions),
                "error": api_error,
            },
            review=review,
        )
    if api_questions:
        return QuizContentResult(
            questions=api_questions,
            source="canvas_quiz_questions_api",
            api_questions={
                "available": True,
                "question_count": len(api_questions),
                "error": api_error,
            },
            review=review,
        )
    return QuizContentResult(
        questions=[],
        source=None,
        error=safe_error(
            [
                api_error,
                "Canvas did not expose any questions in the submitted quiz review page; results may be hidden or unavailable.",
            ]
        ),
        api_questions={
            "available": False,
            "question_count": 0,
            "error": api_error,
        },
        review=review,
    )


def compact_participant_result(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in {"token", "access_token", "launch_token", "refresh_token"}:
                continue
            result[key] = compact_participant_result(item)
        return result
    if isinstance(value, list):
        return [compact_participant_result(item) for item in value]
    return value


def merge_new_quiz_questions(
    session_items: list[dict[str, Any]],
    item_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results_by_item_id = {
        str(item.get("item_id")): item
        for item in item_results
        if isinstance(item, dict) and item.get("item_id") is not None
    }
    questions: list[dict[str, Any]] = []
    for index, session_item in enumerate(session_items, start=1):
        if not isinstance(session_item, dict):
            continue
        item = session_item.get("item")
        item = item if isinstance(item, dict) else {}
        item_id = str(item.get("id") or session_item.get("quiz_entry_id") or index)
        interaction = item.get("interaction_type")
        interaction_name = None
        if isinstance(interaction, dict):
            interaction_name = interaction.get("name") or interaction.get("slug")
        interaction_data = item.get("interaction_data")
        choices = interaction_data.get("choices") if isinstance(interaction_data, dict) else None
        result = copy.deepcopy(results_by_item_id.get(item_id))
        questions.append(
            {
                "id": item_id,
                "position": session_item.get("position") or index,
                "question_number": session_item.get("question_number"),
                "quiz_entry_id": session_item.get("quiz_entry_id"),
                "title": item.get("title") or item.get("label"),
                "type": interaction_name,
                "points_possible": session_item.get("points_possible"),
                "question_html": item.get("item_body"),
                "choices": choices if isinstance(choices, list) else [],
                "result": result,
                "raw_session_item": session_item,
            }
        )
    return questions


async def _fetch_new_quiz_result_browser(
    *,
    preview_url: str,
    site_name: str,
    timeout_ms: int,
) -> dict[str, Any]:

    if "/submissions/" not in urlparse(preview_url).path:
        raise CanvasAPIError("New Quiz preview URL was not a Canvas submission review URL.")
    if "/take" in urlparse(preview_url).path:
        raise CanvasAPIError("New Quiz preview URL pointed to a quiz-taking path.")

    captured: dict[str, Any] = {}
    event = asyncio.Event()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context_kwargs: dict[str, Any] = {"viewport": {"width": 1440, "height": 950}}
        session = load_session(site_name)
        if session and "storage_state" in session:
            context_kwargs["storage_state"] = session["storage_state"]
        context = await browser.new_context(**context_kwargs)

        async def route_handler(route):
            request = route.request
            lowered = request.url.lower()
            if "sentry.insops.net" in lowered or request.resource_type in {
                "image",
                "font",
                "media",
            }:
                await route.abort()
                return
            await route.continue_()

        await context.route("**/*", route_handler)
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)

        async def on_response(response):
            url = response.url
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type.lower():
                return
            try:
                data = await response.json()
            except Exception:
                return
            if "/api/native/launch" in url and isinstance(data, dict):
                captured["launch"] = {
                    key: data.get(key)
                    for key in (
                        "assignment_type",
                        "canvas_assignment_id",
                        "canvas_local_context_id",
                        "entry_path",
                        "return_to",
                        "user_canvas_id",
                    )
                    if key in data
                }
            elif "/api/participant_sessions/" in url and url.endswith("/results"):
                captured["participant_result"] = compact_participant_result(data)
            elif "/session_item_results" in url and isinstance(data, list):
                captured["session_item_results"] = data
            elif "/session_items" in url and isinstance(data, list):
                captured["session_items"] = data
            elif "/api/quiz_sessions/" in url and isinstance(data, dict):
                captured["quiz_session"] = data
            if captured.get("session_items") and captured.get("session_item_results"):
                event.set()

        page.on("response", on_response)
        try:
            await page.goto(preview_url, wait_until="domcontentloaded")
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout_ms / 1000)
            except TimeoutError:
                await page.wait_for_timeout(1500)
            captured["final_url"] = page.url
            captured["title"] = await page.title()
            try:
                captured["page_text"] = clean_text(
                    await page.locator("body").inner_text(timeout=3000)
                )
            except Exception:
                captured["page_text"] = None
        finally:
            await browser.close()

    return captured


def fetch_new_quiz_result(
    *,
    preview_url: str,
    site_name: str,
    timeout_ms: int = 20000,
) -> QuizContentResult:
    try:
        data = asyncio.run(
            _fetch_new_quiz_result_browser(
                preview_url=preview_url,
                site_name=site_name,
                timeout_ms=timeout_ms,
            )
        )
    except Exception as exc:
        return QuizContentResult(
            questions=[],
            source=None,
            error=f"Canvas New Quizzes result page was not readable: {exc}",
            new_quiz_result={"preview_url": preview_url},
        )

    session_items = data.get("session_items")
    item_results = data.get("session_item_results")
    questions = merge_new_quiz_questions(
        session_items if isinstance(session_items, list) else [],
        item_results if isinstance(item_results, list) else [],
    )
    new_quiz_result = {
        "preview_url": preview_url,
        "final_url": data.get("final_url"),
        "title": data.get("title"),
        "page_text": data.get("page_text"),
        "launch": data.get("launch"),
        "participant_result": data.get("participant_result"),
        "quiz_session": data.get("quiz_session"),
        "session_items": session_items if isinstance(session_items, list) else [],
        "session_item_results": item_results if isinstance(item_results, list) else [],
    }
    if questions:
        return QuizContentResult(
            questions=questions,
            source="canvas_new_quizzes_result",
            new_quiz_result=new_quiz_result,
        )
    return QuizContentResult(
        questions=[],
        source=None,
        error=(
            "Canvas New Quizzes did not expose readable result items for this "
            "submitted attempt; results may be hidden or unavailable."
        ),
        new_quiz_result=new_quiz_result,
    )


def fetch_quiz_content(
    *,
    client: CanvasClient,
    course_id: str,
    quiz_id: str,
    quiz_detail: dict[str, Any] | None,
    submission: dict[str, Any] | None,
    skip_for_ta: bool,
) -> QuizContentResult:
    if skip_for_ta:
        return QuizContentResult(questions=[], source=None, error=ta_skip_error())
    if not has_submitted_attempt(submission):
        return QuizContentResult(
            questions=[],
            source=None,
            error=submitted_attempt_error(),
        )
    if submission and is_new_quiz_submission(submission):
        preview_url = submission.get("preview_url")
        if isinstance(preview_url, str) and preview_url:
            return fetch_new_quiz_result(
                preview_url=preview_url,
                site_name=client.site_name,
            )
        return QuizContentResult(
            questions=[],
            source=None,
            error="Submitted New Quiz attempt did not include a preview URL.",
        )

    api_questions: list[dict[str, Any]] = []
    api_error = None
    try:
        api_questions = client.course_quiz_questions(course_id, quiz_id)
    except CanvasAPIError as exc:
        api_error = str(exc)

    return fetch_classic_quiz_content(
        client=client,
        course_id=course_id,
        quiz_id=quiz_id,
        quiz_detail=quiz_detail,
        submission=submission,
        api_questions=api_questions,
        api_error=api_error,
    )
