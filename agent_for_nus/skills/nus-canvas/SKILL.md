---
name: nus-canvas
description: Safely inspect and cache NUS Canvas courses, calendar events, To-Do and upcoming items, home/default views, announcements, assignments, discussions, files, modules, pages, people, quizzes, and syllabi with the local Canvas CLI, and map dates to NUS academic weeks. Use for Canvas course discovery, content refreshes, local cache access, academic-calendar checks, direct Canvas endpoint requests, or authenticated Canvas page, DOM, and network inspection.
---

# NUS Canvas

Use the installed `canvas` command for deterministic, authenticated NUS Canvas reads.

## Workflow

1. Verify `canvas` and `agent-for-nus` exist on `PATH`. If either is missing, tell the user to install `agent-for-nus`; do not clone or install software silently.
2. Before constructing or running any `canvas` CLI command, read [references/commands.md](references/commands.md), including for seemingly familiar commands. Treat it as the authoritative command grammar; do not infer subcommands, options, argument placement, or defaults.
3. Run `canvas auth status` before any network-backed Canvas request. If authentication is required, ask before running `canvas auth login`, because login opens an interactive NUS SSO browser. `agent-for-nus calendar` uses public NUSMods data and does not require Canvas authentication.
4. Prefer the narrowest high-level command. Refresh one course or content area instead of running bulk `canvas sync` unless the user asks for a full sync.
5. Use human output for a simple check, `--format json` or `jsonl` for parsing, and `--format plain` for line-oriented shell filtering. JSON and JSONL omit internal `_canvas_sync*` metadata but can still be large. For a large result, redirect JSON to a file and inspect only the required fields with `jq` instead of loading the entire response into context.
6. Use `canvas api` only when a high-level command is insufficient. Require explicit approval immediately before any request that can change remote state.
7. Read [references/low-level.md](references/low-level.md) and use an authenticated `playwright-cli` session only when the API cannot provide required page, DOM, or network details. Always close the named session.
8. Cite every reported Canvas item as `[title](CANVAS_URL)`. For a downloaded file that exists locally, use `[title](CANVAS_URL) ([local](<ABSOLUTE_PATH>))`; do not add a local link when no downloaded file exists. Preserve and report inaccessible Canvas links with their access error.

### Current activity tasks

1. Use `canvas calendar-events` for Canvas calendar entries, `canvas todo` for the current user's To-Do items, and `canvas upcoming` for upcoming Canvas events before considering `canvas api`. Note that those events are managed by canvas and do not include all the accurate timings. Course schedules, exams are usually not included in the Canvas calendar, To-Do, or upcoming events. Deadlines embedded in course announcements or files are also not included. Therefore, always cross-check against the course announcements and files for accurate deadlines.
2. Use `agent-for-nus calendar --date YYYY-MM-DD` to map a date to its NUS semester, instructional week or non-instructional status, and public-holiday status. This command reuses the public NUSMods academic calendar and accepts `--no-refresh` for cached/offline lookup.
3. Cross-check structured dates against the course term and the shared academic calendar. Treat dates outside the active term, inconsistent week labels, and copied old-semester content as possible staff/content-entry errors; state the discrepancy and do not silently promote it to a current deadline.

### Course related tasks

1. Use `canvas list` to discover courses and their IDs.
2. Use `canvas course <course_id>` to inspect a course's name, ID, term, roles, default view, and available sections.
3. Use `canvas course <course_id> home` to inspect what the course presents as Home. It resolves Canvas's default view to modules, pages, assignments, syllabus, or the activity feed, including a default view whose navigation tab is hidden.
   A course can legitimately have no modules or no published Home content. Human output states that `no content is defined`; JSON/JSONL retain the normal Home object with `"items": []`. Do not treat an empty item list as a fetch failure unless the command also reports an error.
4. Depending on the task, use `canvas course <course_id> {path|home|announcements|assignments|discussions|files|modules|pages|people|quizzes|syllabus}` for detailed information. Apart from `home` and the default view it resolves, query only sections reported as available by `canvas course <course_id>`.
5. Treat `body`, `content`, `description`, `message`, and similar fields that contain a local path as pointers, not the body text. Read the referenced HTML or JSON artifact before summarizing the item.
6. When checking schedules or deadlines, inspect relevant downloaded attachments as well as structured Canvas dates. Dates may exist only inside PDF, image, document, or linked content; extract or render that content as appropriate and state any coverage gap.
7. Treat inaccessible file references returned by `canvas course <course_id> files` as coverage warnings and retain their Canvas links. Explicit file embeds belonging to another Canvas course are intentionally ignored.
8. External-tool navigation entries are currently discoverable as Canvas sections but their embedded third-party contents are not exposed by the high-level CLI. State this limitation explicitly. If the task requires those details, follow the low-level authenticated browser workflow, inspect only the required page, and close the session.

## Cache and safety rules

- Omit refresh flags normally. The default performs incremental remote checks and reuses unchanged cache artifacts; explicit `--refresh` forces the requested scope and is rarely necessary. Use `--no-refresh` only for an explicitly offline/cache-only request or a repetitive read whose required data is already cached.
- Preserve the default data location so incremental refreshes keep working. Do not clear caches or sessions, choose a temporary data path, log out, or delete artifacts unless the user asks.
- Do not open an unstarted quiz or assignment merely to inspect it.
- Do not use TA, staff, or elevated access beyond the user's stated task.
- Never create, edit, delete, submit, enroll, grade, message, or otherwise change Canvas state without explicit user approval.
