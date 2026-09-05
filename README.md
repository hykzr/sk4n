# SkillKit for NUS

SkillKit for NUS bundles deterministic command-line tools and reusable agent
skills for accessing NUS services and keeping useful data available locally.

## Applications

| Application | Purpose | Documentation |
| --- | --- | --- |
| `canvas/` | Query NUS Canvas and incrementally cache courses and accessible content. | [Canvas CLI](canvas/README.md) |
| `nusmods/` | Search public NUSMods course data and manage a share-link-compatible timetable. | [NUSMods CLI](nusmods/README.md) |
| `talent_connect/` | Search, fetch, and persist NUS TalentConnect jobs and companies from Kinobi. | [TalentConnect CLI](talent_connect/README.md) |

Shared browser, request, and session helpers live under `tools/`. Shared NUS
academic-calendar logic lives under `sk4n/` and is exposed as:

```bash
sk4n calendar
```

## Install

Python 3.11 or newer is required.

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
sk4n --help
canvas --help
nusmods --help
talent-connect --help
```

`pipx` is the fallback isolated installer:

```bash
pipx install .
```

## Agent skills

The package bundles skills for Canvas, NUSMods, and TalentConnect. Install,
inspect, or remove the managed copies with:

```bash
sk4n skills install --agents all --scope user
sk4n skills status --agents all --scope all
sk4n skills uninstall --agents antigravity --scope user
```

Supported agents are Codex, GitHub Copilot, Claude, and Google Antigravity.
Antigravity skills use `~/.gemini/config/skills` at user scope and the shared
`.agents/skills` directory at project scope. Use `--agents antigravity` to
target only Antigravity, or combine agent names in a comma-separated list.

## Data locations

Mutable data is stored in the operating system's user-data directory under
`sk4n/`, never in the checkout or installed package:

```text
sk4n/
├── sessions/          # Canvas and TalentConnect browser state
├── canvas/            # Canvas cache and downloaded content
├── nusmods/           # timetable and API cache
└── talent-connect/    # SQLite database
```

Set `SK4N_HOME` to override the entire root. Set
`SK4N_SESSION_DIR` only when tests or account recovery need a separate
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
