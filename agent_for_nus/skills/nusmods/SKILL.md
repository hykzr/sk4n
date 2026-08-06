---
name: nusmods
description: Search public NUSMods course data, inspect course details, import or export share URLs, view schedule status and today's lessons, and edit a locally stored timetable with the nusmods CLI. Use for NUS course discovery, timetable planning, schedule checks, and local timetable mutations; NUSMods access is public and requires no login.
---

# NUSMods

Use the installed `nusmods` command for public course reads and local timetable management. Do not request authentication; NUSMods data is public.

## Workflow

1. Verify `nusmods` exists on `PATH`. If it is missing, tell the user to install `agent-for-nus`; do not clone or install software silently.
2. Read [references/commands.md](references/commands.md) before choosing unfamiliar arguments or subcommands.
3. Distinguish public network reads from local timetable mutations. High-level `nusmods` commands are the preferred interface; use `curl` or `playwright-cli` yourself only when those commands cannot answer a public-data question.
4. Use human output for a simple check, `--format json` or `jsonl` for parsing, and `--format plain` for line-oriented shell filtering. Expect JSON formats to contain complete records and potentially be large.
5. Inspect the current schedule before `schedule import`, `add`, `edit`, or `delete` when the request does not already identify the affected semester, course, or slots.
6. Treat `schedule import` as replacement of one semester. Surface that effect before executing an ambiguous import request.
7. Prefer a NUSMods share URL for portable schedule exports.
8. Return the source command and relevant local artifact paths when they help the user verify the result.

## Cache and mutation rules

- Use `--no-refresh` only for an explicitly offline/cache-only request or a repetitive read whose required data is already cached. Do not use it on the first call by default.
- Preserve the default data location so cached API data and the local timetable remain incremental. Do not choose a temporary data path or clear the cache unless the user asks.
- A schedule mutation changes only the local timetable, not NUSMods or NUS registration records. Execute a clear user-requested local edit, but clarify ambiguous replacements or deletions first.
- Do not claim that a timetable edit registers for, drops, or changes an official NUS course.
