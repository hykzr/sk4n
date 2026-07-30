# Agent for NUS

A collection of small, deterministic tools for accessing NUS services and
keeping useful data available locally.

## Applications

| Application | Purpose | Documentation |
| --- | --- | --- |
| `canvas_sync/` | Incrementally sync NUS Canvas courses and accessible course content. | [Canvas Sync](canvas_sync/README.md) |
| `talent_connect/` | Search, fetch, and persist NUS TalentConnect jobs and companies from Kinobi. | [TalentConnect CLI](talent_connect/README.md) |

Shared browser, request, session, and optional model helpers live under
`tools/`. Application-specific code should remain inside its application
package.

The project uses `uv`:

```bash
uv sync
uv run talent-connect --help
uv run python -m canvas_sync.cli --help
```
