# Agent for NUS

A collection of small, deterministic tools for accessing NUS services and
keeping useful data available locally.

## Applications

| Application | Purpose | Documentation |
| --- | --- | --- |
| `canvas_sync/` | Query NUS Canvas and incrementally cache courses and accessible content. | [Canvas CLI](canvas_sync/README.md) |
| `nusmods/` | Search public NUSMods course data and manage a share-link-compatible timetable. | [NUSMods CLI](nusmods/README.md) |
| `talent_connect/` | Search, fetch, and persist NUS TalentConnect jobs and companies from Kinobi. | [TalentConnect CLI](talent_connect/README.md) |

Shared browser, request, session, and optional model helpers live under
`tools/`. Application-specific code should remain inside its application
package.

The project uses `uv`:

```bash
uv sync
uv run canvas --help
uv run nusmods --help
uv run talent-connect --help
```
