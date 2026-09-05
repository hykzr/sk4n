# TalentConnect low-level inspection

Use this path only after high-level `talent-connect` commands are insufficient.

## Decision order

1. Check `talent-connect auth status=`.
2. Try the smallest read-only Kinobi request with `talent-connect api`. For example:

   ```bash
   talent-connect api /api/auth/
   ```

3. Use a browser only for page-rendered, DOM, or network details absent from the API. Discover the installed browser CLI at runtime:

   ```bash
   playwright-cli --help
   playwright-cli --help <command>
   ```

Require explicit user approval before a non-read-only API method or browser action that applies, bookmarks, messages, uploads, withdraws, accepts or declines, or otherwise changes remote state.

## Authenticated browser session

Choose a unique, short task ID and retain the same session name for all commands:

```bash
talent-connect playwright-cli --session talent-connect-<short-task-id>
playwright-cli -s=talent-connect-<short-task-id> snapshot
playwright-cli -s=talent-connect-<short-task-id> close
```

Use `--headed` on the bootstrap command only when visible interaction is necessary. Put the close command in cleanup logic so it runs after success or failure.

For additional live commands, consult `playwright-cli --help`. If the skill for it is ALREADY installed you can refer to that as well.

## Troubleshooting

- **Missing `playwright-cli`:** suggest `npm install -g @playwright/cli@latest`, then verify `playwright-cli --help`.
- **Missing CLI browser:** suggest `playwright-cli install-browser`.
- **Stale or colliding session:** inspect `playwright-cli list --json`, close only the task's session, and retry with a new unique session name.
- **Expired NUS login:** rerun `talent-connect auth status`. Ask before opening `talent-connect auth login`.
- **Bootstrap failure:** report the failing command and stderr, then attempt to close only the named task session.
