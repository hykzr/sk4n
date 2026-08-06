---
name: nus-canvas
description: Safely inspect and cache NUS Canvas courses, announcements, assignments, discussions, files, modules, pages, people, quizzes, and syllabi with the local canvas CLI. Use for Canvas course discovery, content refreshes, local cache access, direct Canvas endpoint requests, or authenticated Canvas page, DOM, and network inspection.
---

# NUS Canvas

Use the installed `canvas` command for deterministic, authenticated NUS Canvas reads.

## Workflow

1. Verify `canvas` exists on `PATH`. If it is missing, tell the user to install `agent-for-nus`; do not clone or install software silently.
2. Read [references/commands.md](references/commands.md) before choosing unfamiliar arguments or subcommands.
3. Run `canvas auth status` before any network-backed request. If authentication is required, ask before running `canvas auth login`, because login opens an interactive NUS SSO browser.
4. Prefer the narrowest high-level command. Refresh one course or content area instead of running bulk `canvas sync` unless the user asks for a full sync.
5. Use human output for a simple check, `--format json` or `jsonl` for parsing, and `--format plain` for line-oriented shell filtering. Expect JSON formats to expose the complete response and potentially be large.
6. Use `canvas api` only when a high-level command is insufficient. Require explicit approval immediately before any request that can change remote state.
7. Read [references/low-level.md](references/low-level.md) and use an authenticated `playwright-cli` session only when the API cannot provide required page, DOM, or network details. Always close the named session.
8. Return the source command and relevant local artifact paths when they help the user verify the result.

## Cache and safety rules

- Use `--no-refresh` only for an explicitly offline/cache-only request or a repetitive read whose required data is already cached. Do not use it on the first call by default.
- Preserve the default data location so incremental refreshes keep working. Do not clear caches or sessions, choose a temporary data path, log out, or delete artifacts unless the user asks.
- Do not open an unstarted quiz or assignment merely to inspect it.
- Do not use TA, staff, or elevated access beyond the user's stated task.
- Never create, edit, delete, submit, enroll, grade, message, or otherwise change Canvas state without explicit user approval.
