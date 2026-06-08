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
separate JSON files beside `course.json`: `announcements.json`,
`discussions.json`, `people.json`, `pages.json`, `syllabus.json`, and
`modules.json`. Page records include each page's Canvas `body`; discussions
include the topic message and best-effort reply view data when Canvas allows it.
Closed tabs are skipped and any stale file for that content type is removed.
