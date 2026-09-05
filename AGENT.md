# Maintainer agent guide

This file is guidance for agents maintaining this repository. It is not the
operating prompt for an end-user-facing NUS assistant.

nus-canvas, nus-talent-connect and nusmods skills may have been installed to you, those are for end-user-facing NUS assistant. 

## Repository map

- `canvas/` provides authenticated, read-only Canvas queries, targeted cache
  refreshes, bulk synchronization, direct authenticated API
  access, and authenticated `@playwright/cli` sessions.
- `talent_connect/` provides public and authenticated Kinobi job/company
  access, local SQLite persistence, direct authenticated API access, and
  authenticated `@playwright/cli` sessions.
- `nusmods/` uses public NUSMods APIs and does not need an authentication flow.
- `tools/` contains shared request, saved-session, and browser-session helpers.
- `tests/` contains the unit suite. Keep application-specific behavior in its
  package and put genuinely shared behavior in `tools/`.

Use `uv` from the repository root. The normal local checks are:

```bash
uv run pytest -q
uvx ruff --config pyproject.toml check .
uvx pyright
```

## Authentication comes first

Before any Canvas or TalentConnect task that may contact the real service,
always check authentication status first:

```bash
uv run canvas auth status
uv run talent-connect auth status
```

If the relevant command reports unauthenticated and the task needs actual
access, ask the user for approval to start authentication. Once approved, run
the appropriate command below and ask the user to complete NUS SSO in the
browser window:

```bash
uv run canvas auth login
uv run talent-connect auth login
```

Do not require authentication for NUSMods: its APIs are public, so use direct
requests or available browser skills as appropriate.

Prefer the repository's direct authenticated `api` command for endpoint
discovery and validation. It reuses the durable saved login and has lower cost
and overhead than a built-in browser/app connector. When page-level inspection
is necessary, use the repository's `playwright-cli` command; it injects that
same saved authentication into a named `@playwright/cli` session that later CLI
commands can reuse. Avoid built-in app/browser connectors for Canvas and
TalentConnect maintenance because their authentication is not persisted for
subsequent repository commands and they add unnecessary overhead.

```bash
uv run canvas api /api/v1/users/self/profile
uv run canvas playwright-cli --session canvas

uv run talent-connect api /api/auth/
uv run talent-connect playwright-cli --session talent-connect
```

The browser-opening commands leave their sessions running. Continue with, for
example, `playwright-cli -s=canvas snapshot`, and close a session explicitly
when the task is finished.

## Updating a remote integration

Remote interfaces change. When adding support for a new or updated Canvas or
TalentConnect page, field, endpoint, filter, or workflow:

1. Check auth status before beginning remote investigation.
2. Inspect the smallest useful read-only API call first with the app's `api`
   command. Use the authenticated `playwright-cli` session for network, DOM, or
   interaction details that the direct API cannot reveal.
3. Record the observed request/response contract and preserve existing unknown
   fields unless there is a reason to normalize them.
4. Implement the narrowest package-local change, sharing only infrastructure
   that truly applies to multiple apps.
5. Add or update deterministic unit tests, then run the focused tests and the
   full unit suite.
6. Follow unit tests with safe real-service end-to-end checks when remote
   behavior is relevant. Read-only checks such as auth status, profile fetches,
   small list/detail requests, and page inspection are allowed and encouraged
   by default unless the user says otherwise.

Real read-only API calls and actual use of the repository's `playwright-cli`
commands are allowed by default and are required for many remote-integration
tasks; mocks alone are not proof that a changed remote interface still works.

Any operation that would exercise real write access on the user's behalf needs
the user's explicit approval first. This includes creating, editing, deleting,
submitting, applying, bookmarking, messaging, enrolling, or otherwise changing
remote state. Approval for read-only investigation does not imply approval for
writes. Prefer a read-only status/detail request as the end-to-end smoke test
whenever it can validate the implementation safely.
