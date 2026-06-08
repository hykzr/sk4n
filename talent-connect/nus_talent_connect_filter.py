"""
NUS Talent Connect — CEG Internship Filter
===========================================
Filters internship postings on NUS Talent Connect for CEG UG students.

Criteria:
  1. Role relevant for CEG UG (SWE, DevOps, frontend, backend,
     computer engineering, data, AI, cybersecurity, fpga, IC design, embedded system, etc.)
  2. No Singapore Citizen / PR requirement
  3. No requirement to graduate in 2026/2027
  4. Internship period: late May - late Dec 2026, minimum 5 months
  5. Located in Singapore (not overseas)
  6. Application not expired (deadline >= today)

Usage:
    python nus_talent_connect_filter.py

Output:
    nus_internships_filtered.xlsx — all posts with evaluation details.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from datetime import date, datetime
from html import unescape
from pathlib import Path
from bs4 import BeautifulSoup
from rich.console import Console
from rich.table import Table
from openpyxl import Workbook

import pyrootutils

root = pyrootutils.setup_root(__file__, dotenv=True, pythonpath=True, cwd=True)

from tools import (
    BrowserTools,
    LLMModel,
    async_request_user_interaction,
    get_local_models,
    get_models,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://nus-csm.symplicity.com"
SEARCH_URL = f"{BASE_URL}/students/app/jobs/search"
SEARCH_PARAMS = "sort=!postdate&ocr=g&job_type=19,23"
JOBS_API_PATTERN = "/api/v2/jobs?"

TODAY = date.today()

SAVE_DIR = root / "temp"
SAVE_DIR.mkdir(exist_ok=True)
EXCEL_OUTPUT_FILE = SAVE_DIR / "nus_internships_filtered.xlsx"
CSV_OUTPUT_FILE = SAVE_DIR / "nus_internships_filtered.csv"
RAW_JOBS_FILE = SAVE_DIR / "nus_jobs_all.json"
PROCESSED_JOBS_FILE = SAVE_DIR / "nus_jobs_processed.json"
CITIZENSHIP_CACHE_FILE = SAVE_DIR / "nus_jobs_citizenship.json"
V3_DETAIL_CACHE_FILE = SAVE_DIR / "nus_jobs_v3_details.json"

# ---------------------------------------------------------------------------
# HTML → plain text
# ---------------------------------------------------------------------------


def html_to_text(html: str) -> str:
    """Convert HTML job description to plain text."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    # Decode HTML entities
    text = unescape(text)
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Filtering functions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# LLM filtering
# ---------------------------------------------------------------------------


DEFAULT_PREFERRED_MODELS = ["llama3.1"]


# ---------------------------------------------------------------------------
# Graduation requirement pre-filter (regex-based)
# ---------------------------------------------------------------------------

_GRADUATION_SIGNAL_PATTERNS: list[re.Pattern[str]] = [
    # "Graduating in Dec 2026 or May 2027", "graduate by July 2027", etc.
    re.compile(r"\bgraduat", re.IGNORECASE),
    # "final[- ]year student"
    re.compile(r"final[\s-]year", re.IGNORECASE),
    # "penultimate year"
    re.compile(r"penultimate", re.IGNORECASE),
    # "must be in your Nth year" (e.g. "3rd or 4th year")
    re.compile(
        r"(?:3rd|4th|third|fourth|final)\s+year",
        re.IGNORECASE,
    ),
    # "completing .* degree .* 2027"
    re.compile(
        r"complet\w*\s.{0,40}(?:degree|study|programme)",
        re.IGNORECASE,
    ),
]


def _has_graduation_signal(desc_text: str) -> bool:
    """Return True if the description contains explicit graduation-related text.

    Used as a pre-filter: when the LLM reports a graduation requirement but the
    description has no textual signal, the LLM result is overridden to ``"none"``
    to avoid false positives (e.g. the LLM hallucinating a requirement from
    generic phrases like 'SOC ATAP 2026' or 'currently pursuing a degree').
    """
    for pattern in _GRADUATION_SIGNAL_PATTERNS:
        if pattern.search(desc_text):
            return True
    return False


def _select_llm_models(
    preferred_models: list[str] | None = None,
) -> list[LLMModel]:
    prefs = preferred_models or DEFAULT_PREFERRED_MODELS
    models = get_local_models(preferred_models=prefs)
    if not models:
        return []
    return models


def _llm_filter_job(
    *,
    desc_text: str,
    models: list[LLMModel],
) -> dict:
    prompt = f"""
You are filtering NUS Talent Connect internships for CEG undergraduates.
Evaluate the job using ONLY the provided data. Output strict JSON with keys. ONLY OUTPUT JSON, no explanations.:

relevant_role: boolean
graduation_requirement: string ("2026" | "2026/2027" | "2026/2027/2028" | "none" | "unknown")
ug_eligible: boolean
notes: string (short)

Filtering criteria:
1. Role relevant to CEG (or more broadly, from CS to EEE, but not pure finance, consulting, marketing, etc.). 
(SWE, DevOps, frontend, backend, computer engineering, AI, cybersecurity, FPGA, IC / Chip Design, embedded system, etc.).
1. ug_eligible reflects if undergraduates can apply (not just postgraduates). if the role doesn't specify or states all students, assume true.

Graduation requirement rules:
- only consider explicit statements about graduation year, working period is not relevant, 
    academic year is 2026/2027 refers to working period, not graduation year.
- If the role requires a final-year student, set graduation_requirement="2026".
- If the role states "Requires 3rd or 4th year student", set graduation_requirement="2026/2027/2028".
- If the role states "Graduating in 2026 or 2027", "Graduating in Dec 2026 or May 2027", or similar, set graduation_requirement="2026/2027".
- If the role states "Graduating in 2026, 2027, or 2028", "Graduating in 2026, 2027, or 2028", or similar, set graduation_requirement="2026/2027/2028".
- All other cases, including no mention of graduation timing, vague statements like "recent graduates", 
or references to academic year without clear graduation timing, set graduation_requirement="none".
- ATAP / IA is the name of the program and has nothing to do with graduation requirement.
- A single mention of "2026" may be working period or graduation year, but if there is no explicit mention of graduation timing, assume "none".
- Preferred internship period (e.g. "May/Jun 26 - Dec 26") is not a reliable indicator of graduation requirement and should not be used to infer graduation_requirement.
- If no graduation timing is stated, use "none".

description: {desc_text}
""".strip()

    last_error = None
    for model in models:
        print(f"  [LLM] Using model: {model.name}")
        try:
            data = model.call_json(prompt, temperature=0.0, max_tokens=1200)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise RuntimeError(
            f"LLM call failed for all models: {last_error}"
        ) from last_error

    if not isinstance(data, dict):  # type: ignore
        raise ValueError("LLM output is not a JSON object.")

    required_keys = {
        "relevant_role",
        "graduation_requirement",
        "ug_eligible",
        "notes",
    }
    missing = required_keys - set(data.keys())
    if missing:
        raise ValueError(f"LLM output missing keys: {sorted(missing)}")

    return data


def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_json(path: Path, data: dict | list) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _is_llm_cache_valid(data: dict) -> bool:
    required_keys = {
        "relevant_role",
        "graduation_requirement",
        "ug_eligible",
        "notes",
    }
    return required_keys.issubset(data.keys())


def _parse_iso_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _parse_posted_date(posted: str) -> date | None:
    if not posted:
        return None
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(posted, fmt).date()
        except ValueError:
            continue
    return None


def _get_expire_date(job: dict) -> date | None:
    job_end = (job.get("jobs_iso_times") or {}).get("job_end")
    expire_date = _parse_iso_date(job_end) if job_end else None
    if expire_date:
        return expire_date
    return _parse_posted_date(job.get("deadline", ""))


def _is_expired(job: dict) -> bool:
    expire_date = _get_expire_date(job)
    return bool(expire_date and expire_date < TODAY)


def _parse_money(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"[^0-9]", "", value)
    return int(digits) if digits else None


def _salary_from_job(job: dict) -> dict:
    salary_min = _parse_money(job.get("compensation_from"))
    salary_max = _parse_money(job.get("compensation_to"))
    freq = (job.get("compensation_frequency") or "").lower()
    if "month" in freq:
        salary_type = "per_month"
    elif "hour" in freq:
        salary_type = "per_hour"
    else:
        salary_type = "unknown"
    return {"min": salary_min, "max": salary_max, "type": salary_type}


def _write_excel(rows: list[dict]) -> Path:
    headers = [
        "role",
        "company",
        "start_date",
        "end_date",
        "salary_min",
        "salary_max",
        "posted_time",
        "expire_date",
        "location",
        "link",
        "relevant",
        "graduation_requirement",
        "citizenship_requirement",
        "timing_ok",
        "eligible",
    ]

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Jobs"
    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(k, "") for k in headers])
    workbook.save(EXCEL_OUTPUT_FILE)
    return EXCEL_OUTPUT_FILE


def _format_salary(llm_result: dict) -> str:
    salary_min = llm_result.get("salary_min")
    salary_max = llm_result.get("salary_max")
    salary_type = llm_result.get("salary_type")
    if salary_min is None and salary_max is None:
        return "Not specified"

    def _fmt(val: int | None) -> str:
        return f"${val}" if isinstance(val, int) else "?"

    salary_range = _fmt(salary_min)
    if salary_max is not None and salary_max != salary_min:
        salary_range = f"{salary_range} - {_fmt(salary_max)}"

    suffix = ""
    if salary_type == "per_month":
        suffix = " / month"
    elif salary_type == "per_hour":
        suffix = " / hour"
    return f"{salary_range}{suffix}"


def _pretty_print_job(details: dict) -> None:
    console = Console()
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Status", style=details["status_style"], no_wrap=True)
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("Posted")
    table.add_column("Period")
    table.add_column("Salary")
    table.add_column("Reason", overflow="fold")
    table.add_row(
        details["status"],
        details["title"],
        details["company"],
        details["posted"],
        details["period"],
        details["salary"],
        details["reason"],
    )
    console.print(table)
    console.print(f"Link: {details['link']}")


# ---------------------------------------------------------------------------
# Data collection via Playwright
# ---------------------------------------------------------------------------


async def collect_all_jobs(bt: BrowserTools) -> list[dict]:
    """Navigate through all pages and collect job data via API interception.

    Uses page.route() to request a large perPage (500) to minimize navigation.
    Falls back to pagination if needed.
    """
    all_jobs: list[dict] = []
    api_data: dict = {}
    response_event = asyncio.Event()

    async def on_response(response):
        nonlocal api_data
        url = response.url
        if "/api/v2/jobs?" in url and "filters" not in url:
            try:
                body = await response.text()
                data = json.loads(body)
                api_data = data
                all_jobs.extend(data.get("models", []))
                response_event.set()
            except Exception as e:
                print(f"  [warn] Error reading API response: {e}")

    # Route handler: add perPage=500 to get more results at once
    async def modify_request(route):
        url = route.request.url
        if "/api/v2/jobs?" in url and "filters" not in url:
            if "perPage" not in url:
                url += "&perPage=500"
            print(f"  [route] Modified API request: perPage=500")
        await route.continue_(url=url)

    bt.page.on("response", on_response)
    await bt.page.route("**/api/v2/jobs?*", modify_request)

    # Navigate to the search page
    print("Navigating to job search page...")
    full_url = f"{SEARCH_URL}?{SEARCH_PARAMS}"
    await bt.navigate(full_url)

    # Wait for the API response
    try:
        await asyncio.wait_for(response_event.wait(), timeout=30)
    except asyncio.TimeoutError:
        print("[ERROR] Timed out waiting for API response.")
        return []

    total = api_data.get("total", 0)
    per_page = api_data.get("perPage", 20)
    collected = len(all_jobs)
    print(f"  Collected {collected}/{total} jobs (perPage={per_page})")

    # If we didn't get all jobs, paginate
    if collected < total:
        total_pages = (total + per_page - 1) // per_page
        print(f"  Need to fetch {total_pages - 1} more pages...")

        for page_num in range(2, total_pages + 1):
            response_event.clear()
            api_data = {}
            page_url = (
                f"{SEARCH_URL}?perPage={per_page}&page={page_num}&{SEARCH_PARAMS}"
            )
            print(f"  Fetching page {page_num}/{total_pages}...")
            await bt.navigate(page_url)
            try:
                await asyncio.wait_for(response_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                print(f"  [warn] Timed out on page {page_num}, skipping.")
                continue
            print(f"  Collected {len(all_jobs)}/{total} jobs so far")

    # Clean up route handler
    await bt.page.unroute("**/api/v2/jobs?*")

    print(f"\nTotal jobs collected: {len(all_jobs)}")
    return all_jobs


# ---------------------------------------------------------------------------
# v3 API detail fetching (citizenship, timing, location)
# ---------------------------------------------------------------------------

# The v3 job detail API provides structured fields:
#   this_project_is_open_to:  _id="1" → All students, "2" → SC Only, "3" → SC/PR
#   el_work_term:             Academic term with label like "AY 2026/2027 Sem 1 (Jul '26 - Nov/Dec '26)"
#   estimated_start_date_of_intern:  e.g. "2026-07-01"
#   estimated_end_date_of_internsh:  e.g. "2026-12-31"
#   job_location_custom:      e.g. {"_id": "204", "_label": "Singapore"}
#   internship_geography:     e.g. {"_id": "1", "_label": "Local"}

CITIZENSHIP_OPEN_TO_ALL = {"1"}  # _id values that mean no restriction
CITIZENSHIP_RESTRICTED = {"2", "3"}  # _id values that mean restricted

# Timing constants
INTERN_EARLIEST_START = date(2026, 5, 1)  # earliest acceptable start
INTERN_LATEST_START = date(2026, 8, 31)  # latest acceptable start
INTERN_EARLIEST_END = date(2026, 10, 31)  # earliest acceptable end (≥5 months from May)
INTERN_MIN_DAYS = 140  # ~5 months minimum duration


def _extract_citizenship_from_v3(v3_data: dict) -> str:
    """Extract citizenship status from v3 API response data.

    Returns one of:
      'all'           — open to all students
      'sc_only'       — Singapore Citizens only
      'sc_pr_only'    — Singapore Citizens and PRs only
      'unknown'       — field not present or unrecognized
    """
    open_to = v3_data.get("open_to") or v3_data.get("this_project_is_open_to")
    if isinstance(open_to, dict):
        oid = str(open_to.get("_id", ""))
    elif isinstance(open_to, str):
        oid = open_to
    else:
        oid = ""

    if oid == "1":
        return "all"
    elif oid == "2":
        return "sc_only"
    elif oid == "3":
        return "sc_pr_only"
    return "unknown"


def _extract_timing_from_v3(v3_data: dict) -> dict:
    """Extract structured timing info from v3 API response data.

    Returns dict with:
      'work_term'    — label string (e.g. "Academic Year 2026/2027 Semester 1 (Jul '26 - Nov/Dec '26)")
      'start_date'   — date or None
      'end_date'     — date or None
      'period'       — human-readable period string (e.g. "Jul 2026 – Dec 2026")
      'duration_days' — int or None
      'timing_ok'    — bool: meets late-May–Dec 2026 with ≥5 months
      'timing_reason' — explanation if timing_ok is False
    """
    # Extract work term label
    el_term = v3_data.get("el_work_term")
    work_term = ""
    if isinstance(el_term, dict):
        work_term = el_term.get("_label") or el_term.get("title") or ""
    elif isinstance(el_term, str):
        work_term = el_term

    # Extract dates
    start_str = (
        v3_data.get("estimated_start")
        or v3_data.get("estimated_start_date_of_intern")
        or ""
    )
    end_str = (
        v3_data.get("estimated_end")
        or v3_data.get("estimated_end_date_of_internsh")
        or ""
    )
    start_date = _parse_iso_date(start_str) if start_str else None
    end_date = _parse_iso_date(end_str) if end_str else None

    # Build human-readable period
    if start_date and end_date:
        period = f"{start_date.strftime('%b %Y')} – {end_date.strftime('%b %Y')}"
        duration_days = (end_date - start_date).days
    elif work_term:
        period = work_term
        duration_days = None
    else:
        period = "Unknown"
        duration_days = None

    # Evaluate timing criteria
    timing_ok = True
    timing_reason = ""

    # Check work term first — if it matches the accepted semester, accept it
    # regardless of exact duration (standard NUS semester is ~4-5 months)
    is_accepted_term = False
    if work_term:
        term_lower = work_term.lower()
        if "2026/2027" in term_lower and (
            "semester 1" in term_lower or "sem 1" in term_lower
        ):
            is_accepted_term = True

    if is_accepted_term:
        # Accepted academic term — only reject if dates are clearly wrong
        if start_date and end_date:
            if end_date <= start_date:
                timing_ok = False
                timing_reason = f"invalid dates ({start_date.isoformat()} to {end_date.isoformat()})"
            elif start_date > INTERN_LATEST_START:
                timing_ok = False
                timing_reason = f"starts too late ({start_date.isoformat()})"
            else:
                timing_ok = True
        else:
            timing_ok = True  # accepted term, no conflicting dates
    elif start_date and end_date:
        if start_date > INTERN_LATEST_START:
            timing_ok = False
            timing_reason = f"starts too late ({start_date.isoformat()})"
        elif start_date < INTERN_EARLIEST_START:
            timing_ok = False
            timing_reason = f"starts too early ({start_date.isoformat()})"
        elif end_date < INTERN_EARLIEST_END:
            timing_ok = False
            timing_reason = f"ends too early ({end_date.isoformat()})"
        elif duration_days is not None and duration_days < INTERN_MIN_DAYS:
            timing_ok = False
            timing_reason = f"too short ({duration_days} days < {INTERN_MIN_DAYS})"
    elif work_term:
        timing_ok = False
        timing_reason = f"unrecognized work term: {work_term}"
    else:
        timing_ok = False
        timing_reason = "no timing data available"

    return {
        "work_term": work_term,
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "period": period,
        "duration_days": duration_days,
        "timing_ok": timing_ok,
        "timing_reason": timing_reason,
    }


def _extract_location_from_v3(v3_data: dict) -> dict:
    """Extract structured location info from v3 API response data.

    Returns dict with:
      'location'      — location label (e.g. "Singapore")
      'geography'     — geography label (e.g. "Local" or None)
      'in_singapore'  — bool
    """
    loc_custom = v3_data.get("job_location_custom")
    location = ""
    if isinstance(loc_custom, dict):
        location = loc_custom.get("_label") or ""
    elif isinstance(loc_custom, str):
        location = loc_custom

    geo = v3_data.get("internship_geography")
    geography = ""
    if isinstance(geo, dict) and geo.get("_id"):
        geography = geo.get("_label") or ""

    # Determine if in Singapore
    loc_lower = location.lower()
    in_singapore = (
        "singapore" in loc_lower or loc_lower == "" or geography.lower() == "local"
    )

    return {
        "location": location or "Unknown",
        "geography": geography or None,
        "in_singapore": in_singapore,
    }


async def fetch_v3_detail_data(
    bt: BrowserTools,
    job_ids: list[str],
    cache: dict[str, dict],
    batch_size: int = 20,
) -> dict[str, dict]:
    """Fetch structured detail data for all jobs via v3 API.

    Uses browser-context JS fetch() in parallel batches.
    Fetches: citizenship, timing (el_work_term, estimated dates), location.
    Results are cached to V3_DETAIL_CACHE_FILE.

    Returns dict mapping job_id → {citizenship fields, timing fields, location fields}.
    """
    # Filter out jobs that already have complete v3 data
    uncached_ids = [
        jid for jid in job_ids if jid not in cache or "timing" not in cache.get(jid, {})
    ]
    if not uncached_ids:
        print(f"v3 detail data: all {len(job_ids)} jobs already cached.")
        return cache

    print(
        f"Fetching v3 detail data for {len(uncached_ids)} jobs "
        f"({len(job_ids) - len(uncached_ids)} cached)..."
    )

    for batch_start in range(0, len(uncached_ids), batch_size):
        batch = uncached_ids[batch_start : batch_start + batch_size]
        batch_num = batch_start // batch_size + 1
        total_batches = (len(uncached_ids) + batch_size - 1) // batch_size
        print(f"  Batch {batch_num}/{total_batches} ({len(batch)} jobs)...")

        js_code = (
            """
            (async () => {
                const ids = """
            + json.dumps(batch)
            + """;
                const results = {};
                const promises = ids.map(async (id) => {
                    try {
                        const resp = await fetch('/api/v3/jobs/' + id);
                        if (!resp.ok) {
                            results[id] = {error: 'HTTP ' + resp.status};
                            return;
                        }
                        const data = await resp.json();
                        results[id] = {
                            open_to: data.this_project_is_open_to || null,
                            sg_only: data.singaporean_only || null,
                            any_nationality: data.any_nationality || null,
                            el_work_term: data.el_work_term || null,
                            estimated_start: data.estimated_start_date_of_intern || null,
                            estimated_end: data.estimated_end_date_of_internsh || null,
                            job_location_custom: data.job_location_custom || null,
                            internship_geography: data.internship_geography || null,
                        };
                    } catch (e) {
                        results[id] = {error: e.message};
                    }
                });
                await Promise.all(promises);
                return results;
            })()
        """
        )
        try:
            batch_result = await bt.evaluate(js_code)
        except Exception as exc:
            print(f"    [warn] Batch {batch_num} failed: {exc}")
            continue

        for jid, info in batch_result.items():
            if "error" in info:
                print(f"    [warn] {jid[:12]}: {info['error']}")
                continue
            citizenship = _extract_citizenship_from_v3(info)
            timing = _extract_timing_from_v3(info)
            location = _extract_location_from_v3(info)
            cache[jid] = {
                "open_to": info.get("open_to"),
                "citizenship": citizenship,
                "timing": timing,
                "location": location,
            }

        # Save after each batch
        _save_json(V3_DETAIL_CACHE_FILE, cache)

    cached_count = sum(1 for jid in job_ids if jid in cache)
    print(f"v3 detail data: {cached_count}/{len(job_ids)} jobs fetched.")
    return cache


# ---------------------------------------------------------------------------
# Main filter pipeline
# ---------------------------------------------------------------------------


def filter_jobs(
    jobs: list[dict],
    processed_cache: dict[str, dict],
    v3_cache: dict[str, dict],
    graduation_year: int,
    citizenship_status: str,
    preferred_models: list[str] | None = None,
) -> list[dict]:
    """Apply filtering criteria and return enriched job rows.

    Uses v3 API data for citizenship, timing, and location (deterministic).
    Uses LLM only for role relevance, graduation requirement, and UG eligibility.
    """
    results = []
    stats = {
        "total": len(jobs),
        "expired": 0,
        "not_relevant_role": 0,
        "citizenship_required": 0,
        "graduation_required": 0,
        "bad_timing": 0,
        "overseas": 0,
        "llm_errors": 0,
        "passed": 0,
    }

    def _is_graduation_requirement_acceptable(req: str) -> bool:
        if req in {"unknown", "none"}:
            return True
        if req == "2026/2027/2028":
            return True
        if graduation_year == 2028:
            return req == "2026/2027/2028"
        if graduation_year == 2027:
            return req in {"2026", "2026/2027"}
        if graduation_year == 2026:
            return req == "2026"
        return False

    def _is_citizenship_acceptable(req: str) -> bool:
        if req in {"unknown", "all"}:
            return True
        if citizenship_status == "sc":
            return True
        if citizenship_status == "pr":
            return req != "sc_only"
        return False

    models = _select_llm_models(preferred_models)
    if not models:
        raise RuntimeError(
            "No LLM models found. Install an Ollama model or set API keys."
        )

    total_jobs = len(jobs)
    job_ids = {job.get("job_id", "") for job in jobs}
    cached_total = len(
        [
            jid
            for jid, val in processed_cache.items()
            if jid in job_ids and isinstance(val, dict) and _is_llm_cache_valid(val)
        ]
    )
    print(f"Already processed (cached): {cached_total}/{total_jobs}")

    processed_count = 0

    for job in jobs:
        job_id = job.get("job_id", "")
        title = job.get("job_title", "")
        company = job.get("name", "")
        desc_html = job.get("job_desc", "")
        desc_text = html_to_text(desc_html)
        deadline = job.get("deadline", "")
        location = job.get("job_location", "")

        llm_result = processed_cache.get(job_id)
        was_cached = bool(llm_result and _is_llm_cache_valid(llm_result))
        if not was_cached:
            try:
                llm_result = _llm_filter_job(
                    desc_text=desc_text,
                    models=models,
                )
                processed_cache[job_id] = llm_result
                _save_json(PROCESSED_JOBS_FILE, processed_cache)
            except Exception as exc:
                stats["llm_errors"] += 1
                print(f"  [warn] LLM filter failed for {job_id}: {exc}")
                continue

        # --- Filter decisions ---
        failed_reasons = []
        assert llm_result is not None

        # --- v3 API data (authoritative for citizenship, timing, location) ---
        v3_data = v3_cache.get(job_id, {})

        # Expiry check
        expired = _is_expired(job)
        if expired:
            stats["expired"] += 1
            failed_reasons.append("expired")

        # Location: v3 API (deterministic)
        loc_info = v3_data.get("location", {})
        in_singapore = loc_info.get("in_singapore", True)  # default True if no data
        if not in_singapore:
            stats["overseas"] += 1
            failed_reasons.append(f"overseas ({loc_info.get('location', 'Unknown')})")

        # Role relevance: LLM
        if not llm_result.get("relevant_role", False):
            stats["not_relevant_role"] += 1
            failed_reasons.append("not relevant role")

        # Citizenship: v3 API (authoritative)
        citizenship_req = v3_data.get("citizenship", "unknown")
        if not _is_citizenship_acceptable(citizenship_req):
            stats["citizenship_required"] += 1
            failed_reasons.append(f"citizenship required ({citizenship_req})")

        # Graduation: LLM (with regex pre-filter override)
        graduation_req = llm_result.get("graduation_requirement", "unknown")
        if graduation_req not in {"none", "unknown"} and not _has_graduation_signal(
            desc_text
        ):
            print(
                f"  [info] Overriding graduation requirement for {job_id} "
                f"from '{graduation_req}' to 'none' due to lack of textual signal."
            )
            graduation_req = "none"  # override LLM hallucination
        if not _is_graduation_requirement_acceptable(graduation_req):
            stats["graduation_required"] += 1
            failed_reasons.append("graduation requirement")

        # UG eligibility: LLM
        if not llm_result.get("ug_eligible", False):
            failed_reasons.append("not UG eligible")

        # Timing: v3 API (deterministic)
        timing_info = v3_data.get("timing", {})
        period = timing_info.get("period", "Unknown")
        timing_ok = timing_info.get("timing_ok", False)
        if not timing_ok:
            stats["bad_timing"] += 1
            reason = timing_info.get("timing_reason", "timing mismatch")
            failed_reasons.append(f"timing: {reason}")

        status = "PASS" if not failed_reasons else "FAIL"
        salary_info = _salary_from_job(job)
        salary_display = _format_salary(
            {
                "salary_min": salary_info.get("min"),
                "salary_max": salary_info.get("max"),
                "salary_type": salary_info.get("type"),
            }
        )
        reason_text = (
            ", ".join(failed_reasons) if failed_reasons else "meets all criteria"
        )
        processed_count += 1
        if not was_cached:
            _pretty_print_job(
                {
                    "status": status,
                    "status_style": "green" if status == "PASS" else "red",
                    "title": title,
                    "company": company,
                    "posted": job.get("postdate", ""),
                    "period": period,
                    "salary": salary_display,
                    "link": f"{BASE_URL}/students/app/jobs/detail/{job_id}",
                    "reason": reason_text,
                    "text": (
                        f"[{status}] {title} | {company} | Posted: {job.get('postdate', '')} | "
                        f"Period: {period} | Salary: {salary_display} | {reason_text}"
                    ),
                }
            )
            print(f"Processed {processed_count}/{total_jobs}")

        eligible = not failed_reasons
        if eligible:
            stats["passed"] += 1

        loc_label = loc_info.get("location", "Unknown")
        expire_date = _get_expire_date(job)
        results.append(
            {
                "role": title,
                "company": company,
                "timing": period,
                "start_date": timing_info.get("start_date", ""),
                "end_date": timing_info.get("end_date", ""),
                "salary_min": salary_info.get("min"),
                "salary_max": salary_info.get("max"),
                "salary_type": salary_info.get("type"),
                "posted_time": job.get("postdate", ""),
                "expire_date": expire_date.isoformat() if expire_date else "",
                "location": loc_label,
                "relevant": llm_result.get("relevant_role", False),
                "graduation_requirement": graduation_req,
                "citizenship_requirement": citizenship_req,
                "timing_ok": timing_ok,
                "in_singapore": in_singapore,
                "ug_eligible": llm_result.get("ug_eligible", False),
                "eligible": eligible,
                "link": f"{BASE_URL}/students/app/jobs/detail/{job_id}",
            }
        )

    # Print stats
    print("\n" + "=" * 60)
    print("FILTERING RESULTS")
    print("=" * 60)
    print(f"  Total jobs scanned:        {stats['total']}")
    print(f"  Expired:                   {stats['expired']}")
    print(f"  Overseas:                  {stats['overseas']}")
    print(f"  Not relevant role:         {stats['not_relevant_role']}")
    print(f"  Citizenship required:      {stats['citizenship_required']}")
    print(f"  Graduation required:       {stats['graduation_required']}")
    print(f"  Bad timing:                {stats['bad_timing']}")
    print(f"  LLM errors:                {stats['llm_errors']}")
    print(f"  ✓ Passed all filters:      {stats['passed']}")
    print("=" * 60)

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    print("=" * 60)
    print("NUS Talent Connect — CEG Internship Filter")
    print("=" * 60)

    parser = argparse.ArgumentParser(
        description="NUS Talent Connect — CEG Internship Filter"
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh job postings from the portal (processed cache kept).",
        default=False,
    )
    parser.add_argument(
        "--preferred-models",
        nargs="*",
        default=DEFAULT_PREFERRED_MODELS,
        help=(
            "Preferred LLM model name substrings, used to prioritize models. "
            f"Default: {DEFAULT_PREFERRED_MODELS}"
        ),
    )
    parser.add_argument(
        "--graduation-year",
        type=int,
        choices=[2026, 2027, 2028],
        default=2028,
        help=("Your graduation year (affects eligibility). " "Default: 2028"),
    )
    parser.add_argument(
        "--citizenship",
        choices=["neither", "sc", "pr"],
        default="neither",
        help=("Your citizenship status. " "neither = not SC/PR. Default: neither"),
    )
    args = parser.parse_args()

    bt = None

    # --- 1. Collect jobs (skip browser if cache is valid and refresh is false) ---
    cached_jobs = None if args.refresh else _load_json(RAW_JOBS_FILE)
    if isinstance(cached_jobs, list) and cached_jobs:
        print(f"\nLoaded {len(cached_jobs)} cached jobs.")
        all_jobs = cached_jobs
    else:
        # --- 1a. Start browser with session ---
        bt = BrowserTools(headless=False)
        await bt.start(site_name="nus_talent_connect")

        # --- 1b. Navigate to the search page (may redirect to login) ---
        full_url = f"{SEARCH_URL}?{SEARCH_PARAMS}"
        await bt.navigate(full_url)
        await bt.wait_for_timeout(2000)

        cur_url = await bt.current_url()
        if "symplicity.com/students" not in cur_url:
            # Need to log in
            print("\nLogin required. Please log in via the browser window.")
            await async_request_user_interaction(
                "Please log in to NUS Talent Connect in the browser. "
                "Wait until you see the job listings, then press Enter here."
            )
            await bt.save_session_data("nus_talent_connect")
            print("Session saved for future runs.")

        # Ensure we're on the right page
        cur_url = await bt.current_url()
        if "jobs" not in cur_url:
            await bt.navigate(full_url)
            await bt.wait_for_timeout(3000)

        # --- 1c. Collect all jobs via API interception ---
        print("\nCollecting all internship postings...")
        all_jobs = await collect_all_jobs(bt)
        if all_jobs:
            _save_json(RAW_JOBS_FILE, all_jobs)

    if not all_jobs:
        print("\n[ERROR] No jobs collected. Check your login session.")
        if bt:
            await bt.stop()
        return

    # --- 4. Filter jobs ---
    print(f"\nFiltering {len(all_jobs)} jobs...")
    processed_cache = _load_json(PROCESSED_JOBS_FILE)
    if not isinstance(processed_cache, dict):
        processed_cache = {}
    # --- 3b. Fetch v3 detail data (citizenship + timing + location) ---
    v3_cache = _load_json(V3_DETAIL_CACHE_FILE)
    if not isinstance(v3_cache, dict):
        v3_cache = {}
    # Also load old citizenship-only cache and migrate data
    old_cit_cache = _load_json(CITIZENSHIP_CACHE_FILE)
    if isinstance(old_cit_cache, dict):
        for jid, cit_data in old_cit_cache.items():
            if jid not in v3_cache:
                v3_cache[jid] = cit_data  # will be re-fetched for timing
    job_ids = [j.get("job_id", "") for j in all_jobs if j.get("job_id")]
    uncached_v3 = [
        jid
        for jid in job_ids
        if jid not in v3_cache or "timing" not in v3_cache.get(jid, {})
    ]
    if uncached_v3:
        if bt is None:
            bt = BrowserTools(headless=False)
            await bt.start(site_name="nus_talent_connect")
            # Navigate to establish session
            full_url = f"{SEARCH_URL}?{SEARCH_PARAMS}"
            await bt.navigate(full_url)
            await bt.wait_for_timeout(2000)
            cur_url = await bt.current_url()
            if "symplicity.com/students" not in cur_url:
                print("\nLogin required for v3 data fetching.")
                await async_request_user_interaction(
                    "Please log in to NUS Talent Connect, then press Enter."
                )
                await bt.save_session_data("nus_talent_connect")
        v3_cache = await fetch_v3_detail_data(bt, job_ids, v3_cache)

    rows = filter_jobs(
        all_jobs,
        processed_cache,
        v3_cache,
        args.graduation_year,
        args.citizenship,
        args.preferred_models,
    )

    # --- 5. Output results ---
    def _sort_date(value: str) -> date:
        parsed = _parse_posted_date(value)
        return parsed if parsed else date.min

    def _sort_expire(value: str) -> date:
        return _parse_posted_date(value) or date.min

    def _salary_sort(row: dict) -> int:
        return row.get("salary_max") or row.get("salary_min") or 0

    rows.sort(
        key=lambda r: (
            not r.get("eligible", False),
            -_salary_sort(r),
            -_sort_date(r.get("posted_time", "")).toordinal(),
            -_sort_expire(r.get("expire_date", "")).toordinal(),
            r.get("role", ""),
        )
    )

    output_path = _write_excel(rows)
    eligible_count = sum(1 for row in rows if row.get("eligible"))
    print(f"\nResults saved to {output_path}")
    print(f"Eligible roles: {eligible_count}/{len(rows)}")

    # --- 6. Clean up ---
    if bt is not None:
        await bt.stop()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
