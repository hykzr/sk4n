# Canvas CLI

`canvas` is an authenticated, read-only NUS Canvas CLI with an incremental local
cache. Install the project with `uv sync`, then inspect the command surface:

```bash
uv run canvas --help
```

`uv run canvas-sync` and `uv run python -m canvas_sync` are equivalent entry
points.

## Authentication

```bash
uv run canvas auth status
uv run canvas auth login
uv run canvas auth login --refresh
uv run canvas auth logout
```

Login opens a browser only when the saved `nus_canvas` session is missing,
expired, or explicitly refreshed. Logout deletes only this CLI's saved session;
it does not sign other browsers out of NUS SSO. `auth status` also supports
`--format json`, `jsonl`, or `plain`.

## Student and courses

```bash
uv run canvas student
uv run canvas list
uv run canvas list --semester latest
uv run canvas list -s 2526S1
uv run canvas list -s ay2526s1
uv run canvas list -s Y3S1
uv run canvas list -s Non-Academic
uv run canvas course CG2028
uv run canvas course CS1010 -s 2425S1
uv run canvas course CG2028 path
```

Semester values are case-insensitive. With no semester, `list` returns every
accessible course. `latest` selects the newest regular academic semester;
`2526S1` and `AY2526S1` are equivalent. Available irregular terms such as
`Non-Academic` can also be selected case-insensitively. Study-year forms such as
`Y3S1` are resolved from the student's inferred enrollment academic year. Canvas
does not expose a dedicated NUS matriculation-year field, so the CLI labels this
value as an inference from the Canvas account creation date.

Course codes are also case-insensitive. A code that matches multiple enrollments
is rejected with the matching semester, Canvas ID, role, and name instead of
silently selecting one. Add `-s/--semester` to restrict the match; if that leaves
one course, the command proceeds. A numeric Canvas course ID is always an exact
selection. `course CODE` prints course metadata—including student, TA, or other
enrollment roles—available content areas, and cache paths, but not the course's
full cached content. `course CODE path` prints the absolute course cache folder.

## Course content

The content command shape is:

```text
canvas course COURSE RESOURCE {list|path|ITEM_ID}
```

Supported resources are `announcements`, `assignments`, `discussions`, `files`,
`modules`, `pages`, `people`, `quizzes`, and `syllabus`; singular aliases work
too. Omitting the last argument defaults to `list`.

```bash
uv run canvas course CG2028 announcements list
uv run canvas course CG2028 announcements 12345
uv run canvas course CG2028 assignments path
uv run canvas course 62224 assignments 118925
uv run canvas course CG2028 quizzes list
uv run canvas course CG2028 people list
```

`list` returns compact cached item records. An item ID returns its detailed
record when a dedicated JSON artifact exists. HTML bodies, assignment JSON,
quiz JSON, and downloaded-file fields include their absolute local paths.
`path` returns the absolute collection JSON path. Every local path emitted by
the CLI is absolute, although paths persisted inside cache JSON remain relative
and portable. When every listed item shares one cache file, as with `people`,
the human-readable table prints that file once above the table instead of
repeating it in every row.

All student, list, course, and course-content commands support:

```bash
--format json|jsonl|plain
--refresh
--no-refresh
```

The default contacts Canvas and performs the same incremental checks as the
fetcher: cheap list/signature data is checked, unchanged artifacts are reused,
and expensive bodies or downloads are fetched only when needed. `--refresh`
forces the requested scope. `--no-refresh` performs a strictly cache-only read.
A course-content command syncs only that course and content area; it does not
sync unrelated courses or tabs. These commands use only read-only Canvas HTTP
operations, though refreshed results are written to the local cache.

## Direct API requests

`api` sends a direct request with the saved Canvas cookies and prints JSON or
text responses:

```bash
uv run canvas api /api/v1/users/self/profile
uv run canvas api '/api/v1/courses?enrollment_state=active'
uv run canvas api /api/v1/courses/93662/tabs --param 'include[]=external'
uv run canvas api /api/example -X POST --data '{"key":"value"}'
uv run canvas api /api/example -H 'Accept:application/json'
```

The direct `requests` transport is used first; the command does not launch a
browser. If the cookies are invalid, run `canvas auth login`.

## Low-level authenticated browser sessions

Use `playwright-cli` when an API request is not enough to inspect or extend the
Canvas integration:

```bash
uv run canvas playwright-cli
uv run canvas playwright-cli --url 'https://canvas.nus.edu.sg/courses/12345'
uv run canvas playwright-cli --headed --session canvas-debug
```

The command checks the saved login, opens the normal interactive login flow if
needed, starts a headless `@playwright/cli` session by default, injects the saved
authenticated storage state, and leaves the session open. `--headed` shows the
browser; `--headless` is the explicit form of the default. The default session
ID is `canvas`, and the default URL is the Canvas root page.

Continue with commands such as `playwright-cli -s=canvas snapshot` and close it
when finished with `playwright-cli -s=canvas close`. The command fails without
the separately installed `@playwright/cli` executable, if the browser cannot
open, or if that session ID is already running.

## Bulk sync compatibility

The original fetch-all behavior is available under `sync`, with its existing
selection, refresh, skip, debug, and login-only options:

```bash
uv run canvas sync
uv run canvas sync --login-only
uv run canvas sync --course CG2028 --course 85096
uv run canvas sync --refresh-course
uv run canvas sync --refresh-people
uv run canvas sync --refresh-pages --refresh-discussions
uv run canvas sync --refresh-assignments
uv run canvas sync --refresh-files
uv run canvas sync --refresh-content
uv run canvas sync --skip-files
uv run canvas sync --skip-assignments --skip-files
```

## Cache layout

The default cache is `data/canvas/{term}/{course}`. Academic terms such as
`2025/2026 Semester 2` normalize to `2526S2`; irregular terms such as
`Non-Academic` retain a filesystem-safe name. The root contains `index.json`
and the privacy-trimmed `student.json`. Course folders can contain:

- `course.json` and `cover_image.*`
- `announcements/announcements.json` and announcement HTML
- `discussions/discussions.json` and discussion HTML
- `people.json`
- `pages/pages.json` and page HTML
- `syllabus.json` and `syllabus.html`
- `modules.json`
- `assignments/assignments.json`, with per-assignment or per-quiz folders
- `files/files.json` and downloaded Canvas files

Announcement, discussion, page, syllabus, assignment, and quiz HTML is saved as
local `.html` files. Files referenced by modules or HTML bodies are downloaded
even when the Files tab itself is closed. File metadata includes a SHA-256 hash.
Assignments can also include downloaded submitted files, description images,
Classic Quiz review content, and New Quizzes result data when Canvas exposes
them. Unstarted quizzes are not opened, and TA/staff enrollments skip quiz
content reads to avoid elevated-access behavior.

Incremental checks compare Canvas summaries and stable signatures. Pages and
discussion views fetch details only for changed items; files compare metadata
and retain verified downloads; assignments compare stable assignment, quiz, and
self-submission fields; people now compare the fetched roster fingerprint and
avoid rewriting an unchanged cache. Syllabus bodies have no cheap update signal,
so an existing syllabus remains lazy unless forced.

## Python API

Targeted fetching is also public as `CanvasFetcher`:

```python
from canvas_sync import CanvasFetcher

fetcher = CanvasFetcher()
student = fetcher.student()
courses = fetcher.courses(semester="latest")
course = fetcher.course("CS1010", semester="2425S1")
announcements = fetcher.content("CG2028", "announcements", "list")
```

Each method accepts cache/refresh controls appropriate to its scope. The legacy
`sync_canvas()` API remains exported for bulk synchronization.
