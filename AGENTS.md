# Maintainer agent guide

This file is guidance for agents maintaining this repository. It is not the operating prompt for an
end-user-facing NUS assistant.

nus-canvas, nus-talent-connect, nus-outlook and nusmods skills may have been installed to you, those
are for end-user-facing NUS assistant.

## Repository map

- `src/sk4n/canvas/` provides authenticated, read-only Canvas queries, targeted cache refreshes,
  bulk synchronization, direct authenticated API access, and authenticated `@playwright/cli`
  sessions.
- `src/sk4n/talent_connect/` provides public and authenticated Kinobi job/company access, local
  SQLite persistence, direct authenticated API access, and authenticated `@playwright/cli` sessions.
- `src/sk4n/nusmods/` uses public NUSMods APIs and does not need an authentication flow.
- `src/sk4n/outlook/` provides NUS SSO/MFA login, durable browser state with silent token renewal,
  Outlook mail reads and authorized item actions, NUS directory (people) search, and authenticated
  `@playwright/cli` inspection. Its web endpoints are private and may change.
- `src/sk4n/tools/` contains shared request, saved-session, and browser-session helpers.
- `tests/` contains the unit suite. Keep application-specific behavior in its package and put
  genuinely shared behavior in `src/sk4n/tools/`.

For check / build / test, refer to the justfile under repo root. Prefer running the just commands
like `just check` as is.

## Authentication comes first

Before any Canvas, TalentConnect, or Outlook task that may contact the real service, always check
authentication status first:

```bash
uv run canvas auth status
uv run talent-connect auth status
uv run nus-outlook auth status
```

If the relevant command reports unauthenticated and the task needs actual access, ask the user for
approval to start authentication. Once approved, run the appropriate command below and ask the user
to complete NUS SSO in the browser window:

```bash
uv run canvas auth login
uv run talent-connect auth login
uv run nus-outlook auth login
```

Do not require authentication for NUSMods: its APIs are public, so use direct requests or available
browser skills as appropriate.

Prefer the repository's direct authenticated `api` command for endpoint discovery and validation. It
reuses the durable saved login and has lower cost and overhead than a built-in browser/app
connector. When page-level inspection is necessary, use the repository's `playwright-cli` command;
it injects that same saved authentication into a named `@playwright/cli` session that later CLI
commands can reuse. Avoid built-in app/browser connectors for Canvas and TalentConnect maintenance
because their authentication is not persisted for subsequent repository commands and they add
unnecessary overhead.

```bash
uv run canvas api /api/v1/users/self/profile
uv run canvas playwright-cli --session canvas

uv run talent-connect api /api/auth/
uv run talent-connect playwright-cli --session talent-connect

uv run nus-outlook api '/owa/service.svc?action=GetOwaUserConfiguration' -X POST
uv run nus-outlook playwright-cli --session nus-outlook
```

The browser-opening commands leave their sessions running. Continue with, for example,
`playwright-cli -s=canvas snapshot`, and close a session explicitly when the task is finished.

## TASK: Updating a remote integration

Remote interfaces change. When adding support for a new or updated page, field, endpoint, filter, or
workflow:

1. Check auth status before beginning remote investigation.
2. Prefer existing `api` command for exploration over raw curl
3. Use the authenticated `playwright-cli` session for network, DOM, or interaction details that the
   direct API cannot reveal.

Real read-only API calls and actual use of the repository's `playwright-cli` commands are allowed by
default and are required for many remote-integration tasks; mocks alone are not proof that a changed
remote interface still works.

Any operation that would exercise real write access on the user's behalf needs the user's explicit
approval first.

## TASK: Actual check for tool and skill

When asked to test a tool and its skill in real life situation: You'll be given a task, which you
need to complete first by referring to `src/sk4n/skills/<feature>` only, and not any other code. Do
not refer to any other user scope or global installations of the tool or the skill. Then apply the
SKILL.md to do the task, (replace the direct call with uv run, such as `canvas auth status` should
become `uv run canvas auth status`, and the references `SKILL.md` mentioned is also in that folder).

Note down

1. the direct response for actual's task
2. any inconsistency or incompleteness of documentations that leads you to run the wrong command and
   retry, or any undocumented aspects that differ from your assumptions and cause a error and retry
3. named high level commands failure that you have to use the low-level alternatives to complete the
   job
4. other issues for the skill and tool

follow the SKILL.md for authentication and approval rules and stop the inspection right away if not
logged in.

after the task is done, note down your findings in `temp/actual-test/<feature>.md`
