## Canvas Sync

Sync course metadata and opened course content:

```bash
python -m canvas_sync.cli
```

The command validates the saved `nus_canvas` session first. If the session is
missing or expired, it opens a visible browser and waits for you to log in, then
saves the refreshed session and continues automatically.

By default this writes to `data/canvas/{term}/{course}`. Academic terms such as
`2025/2026 Semester 2` are normalized to `2526S2`; irregular terms such as
`Non-Academic` are kept as-is except for filesystem-unsafe characters. Each
course folder contains `course.json` with basic course info, enrolled Canvas
sections, available course navigation tabs such as Files/Modules/Assignments,
and the cover image URL when Canvas provides one. Published, accessible past
courses from Canvas's `/courses` page are included as well. Cover images are
downloaded beside `course.json` as `cover_image.*`.

When a course exposes the relevant Canvas navigation tab, the sync also writes
content files beside `course.json`:

- `announcements/announcements.json`
- `discussions/discussions.json`
- `people.json`
- `pages/pages.json`
- `syllabus.json` and `syllabus.html`
- `modules.json`
- `assignments/assignments.json`, with one subfolder per assignment or quiz
- `files/files.json` plus downloaded Canvas files

Announcement, discussion, page, and syllabus HTML bodies are written to `.html`
files. The old Canvas `message`, `body`, or `content` field is replaced with a
relative path to that HTML file. Paths stored inside course JSON files are
relative to the JSON file that contains them. Files are downloaded under the
course's `files/` folder, using Canvas's folder structure when the Files tab is
available. Files linked from modules or HTML bodies are also downloaded, even
when the Files tab itself is closed. `files/files.json` stores metadata for each
downloaded file, with the local `path` relative to `files.json` and a SHA-256
hash for the downloaded bytes.

Assignments and quizzes are stored under `assignments/`. `Roll Call Attendance`
is ignored. Each assignment or standalone quiz gets a normalized-name subfolder
containing `assignment.json`, optional `content.html`, optional downloaded
description images under `images/`, optional `quiz.json`, and submitted files
under `submitted_files/` when Canvas exposes a self-submission with upload
attachments. TA-only courses record self-submission lookup errors without
fetching other students' submissions.

Existing courses are incremental by default. `course.json` and `people.json` are
assumed stable and are not refreshed unless forced. Pages are checked through
Canvas page summaries and page bodies are fetched only for new or changed pages.
Discussion reply views are fetched only for new or changed discussion topics.
Announcements and discussion topic lists on NUS Canvas already include the
message body, so the sync compares cached signatures and avoids rewriting
unchanged local files. Syllabus bodies do not have a cheap update signal, so
existing `syllabus.html` is skipped unless forced. Files are checked by Canvas
metadata (`updated_at`, `modified_at`, `size`, filename, folder, and lock/hidden
fields). A file is not downloaded again when that metadata signature matches the
cache and the local file with its SHA-256 hash is still present. Assignments are
checked through Canvas assignment and quiz list metadata plus durable
self-submission fields; volatile values such as late-second counters are ignored.

Useful options:

```bash
python -m canvas_sync.cli --course CG2023 --course 85096
python -m canvas_sync.cli --refresh-course
python -m canvas_sync.cli --refresh-people
python -m canvas_sync.cli --refresh-pages --refresh-discussions
python -m canvas_sync.cli --refresh-assignments
python -m canvas_sync.cli --refresh-files
python -m canvas_sync.cli --refresh-content
```

`--course` accepts one or more course IDs or exact course codes and can be
repeated. The CLI prints Rich progress and a summary table showing which tabs
were created, updated, unchanged, skipped, or failed.
