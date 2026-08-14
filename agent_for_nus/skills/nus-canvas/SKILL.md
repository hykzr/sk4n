---
name: nus-canvas
description: Safely inspect and cache NUS Canvas courses, home/default views, announcements, assignments, discussions, files, modules, pages, people, quizzes, and syllabi with the local canvas CLI, and map dates to NUS academic weeks. Use for Canvas course discovery, content refreshes, local cache access, academic-calendar checks, direct Canvas endpoint requests, or authenticated Canvas page, DOM, and network inspection.
---

# NUS Canvas

Use the installed `canvas` command for deterministic, authenticated NUS Canvas reads.

## Workflow

1. Verify `canvas` exists on `PATH`. If it is missing, tell the user to install `agent-for-nus`; do not clone or install software silently.
2. Before constructing or running any `canvas` CLI command, read [references/commands.md](references/commands.md), including for seemingly familiar commands. Treat it as the authoritative command grammar; do not infer subcommands, options, argument placement, or defaults.
3. Run `canvas auth status` before any network-backed Canvas request. If authentication is required, ask before running `canvas auth login`, because login opens an interactive NUS SSO browser. `canvas calendar` uses public NUSMods data and does not require Canvas authentication.
4. Prefer the narrowest high-level command. Refresh one course or content area instead of running bulk `canvas sync` unless the user asks for a full sync.
5. Use human output for a simple check, `--format json` or `jsonl` for parsing, and `--format plain` for line-oriented shell filtering. Expect JSON formats to expose the complete response and potentially be large.
6. Use `canvas api` only when a high-level command is insufficient. Require explicit approval immediately before any request that can change remote state.
7. Read [references/low-level.md](references/low-level.md) and use an authenticated `playwright-cli` session only when the API cannot provide required page, DOM, or network details. Always close the named session.
8. Return the source command and relevant local artifact paths when they help the user verify the result.

### Course related tasks

1. Use `canvas list` to discover courses and their IDs.
2. Use `canvas course <course_id>` to inspect a course's name, ID, term, roles, default view, and available sections.
3. Use `canvas course <course_id> home` to inspect what the course presents as Home. It resolves Canvas's default view to modules, pages, assignments, syllabus, or the activity feed, including a default view whose navigation tab is hidden.
4. Depending on the task, use `canvas course <course_id> {path|home|announcements|assignments|discussions|files|modules|pages|people|quizzes|syllabus}` for detailed information. Apart from `home` and the default view it resolves, query only sections reported as available by `canvas course <course_id>`.
5. Treat `body`, `content`, `description`, `message`, and similar fields that contain a local path as pointers, not the body text. Read the referenced HTML or JSON artifact before summarizing the item.
6. When checking schedules or deadlines, inspect relevant downloaded attachments as well as structured Canvas dates. Dates may exist only inside PDF, image, document, or linked content; extract or render that content as appropriate and state any coverage gap.
7. Use `canvas calendar --date YYYY-MM-DD` to map a date to its NUS semester, instructional week or non-instructional status, and public-holiday status. This reuses the public NUSMods academic calendar and accepts `--no-refresh` for cached/offline lookup.
8. External-tool navigation entries are currently discoverable as Canvas sections but their embedded third-party contents are not exposed by the high-level CLI. State this limitation explicitly. If the task requires those details, follow the low-level authenticated browser workflow, inspect only the required page, and close the session.

## Cache and safety rules

- Use `--no-refresh` only for an explicitly offline/cache-only request or a repetitive read whose required data is already cached. Do not use it on the first call by default.
- Preserve the default data location so incremental refreshes keep working. Do not clear caches or sessions, choose a temporary data path, log out, or delete artifacts unless the user asks.
- Do not open an unstarted quiz or assignment merely to inspect it.
- Do not use TA, staff, or elevated access beyond the user's stated task.
- Never create, edit, delete, submit, enroll, grade, message, or otherwise change Canvas state without explicit user approval.
