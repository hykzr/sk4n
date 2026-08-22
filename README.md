# Agent for NUS

A collection of small, deterministic tools for accessing NUS services and
keeping useful data available locally.

## Applications

| Application | Purpose | Documentation |
| --- | --- | --- |
| `canvas_sync/` | Query NUS Canvas and incrementally cache courses and accessible content. | [Canvas CLI](canvas_sync/README.md) |
| `nusmods/` | Search public NUSMods course data and manage a share-link-compatible timetable. | [NUSMods CLI](nusmods/README.md) |
| `talent_connect/` | Search, fetch, and persist NUS TalentConnect jobs and companies from Kinobi. | [TalentConnect CLI](talent_connect/README.md) |

Shared browser, request, and session helpers live under `tools/`. Shared NUS
academic-calendar logic lives under `agent_for_nus/` and is exposed as:

```bash
agent-for-nus calendar --date 2026-08-14
```

This public NUSMods-backed command maps dates to academic years, instructional
weeks, week ranges, and holidays without requiring Canvas authentication.
Application-specific code should remain inside its application package.

## Install

Install a stable snapshot from a checkout into an isolated tool environment:

```bash
uv tool install .
```

For development, an editable tool install reflects source changes without a
reinstall:

```bash
uv tool install --editable .
```

After installation, all commands work from any directory without the checkout:

```bash
agent-for-nus --help
canvas --help
nusmods --help
talent-connect --help
```

`pipx` is the fallback isolated installer:

```bash
pipx install .
```

## Data locations

Mutable data is stored in the operating system's user-data directory under
`agent-for-nus/`, never in the checkout or installed package:

```text
agent-for-nus/
├── sessions/          # Canvas and TalentConnect browser state
├── canvas/            # Canvas cache and downloaded content
├── nusmods/           # timetable and API cache
└── talent-connect/    # SQLite database
```

Set `AGENT_FOR_NUS_HOME` to override the entire root. Set
`AGENT_FOR_NUS_SESSION_DIR` only when tests or account recovery need a separate
session location. The session directory and files contain authentication
material and are restricted to the current user where the OS supports POSIX
permissions.

Each service's `--data-path PATH` option overrides its default directly. The
former `CANVAS_DATA_PATH`, `NUSMODS_DATA_PATH`, and
`TALENT_CONNECT_DATA_PATH` environment variables are no longer supported.

## Develop

The repository uses `uv`:

```bash
uv sync
uv run canvas --help
uv run nusmods --help
uv run talent-connect --help
```
