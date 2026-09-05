# Canvas low-level inspection

Use this path only after high-level `canvas` commands are insufficient.

## Decision order

1. Check `canvas auth status`.
2. Try the smallest read-only same-origin request with `canvas api`. For example:

   ```bash
   canvas api /api/v1/users/self/profile
   ```

3. Use a browser only for page-rendered, DOM, or network details absent from the API. Discover the installed browser CLI at runtime:

   ```bash
   playwright-cli --help
   playwright-cli --help <command>
   ```

Do not send Canvas cookies to another origin. Require explicit user approval before a non-read-only API method or browser action that changes remote state.

## Authenticated browser session

Choose a unique, short task ID and retain the same session name for all commands:

```bash
canvas playwright-cli --session canvas-<short-task-id>
playwright-cli -s=canvas-<short-task-id> snapshot
playwright-cli -s=canvas-<short-task-id> close
```

Use `--headed` on the bootstrap command only when visible interaction is necessary. Put the close command in cleanup logic so it runs after success or failure. Never open an unstarted quiz or assignment merely to inspect it.

For additional live commands, consult `playwright-cli --help`. If the skill for it is ALREADY installed you can refer to that as well.

## Troubleshooting

- **Missing `playwright-cli`:** suggest `npm install -g @playwright/cli@latest`, then verify `playwright-cli --help`.
- **Missing CLI browser:** suggest `playwright-cli install-browser`.
- **Stale or colliding session:** inspect `playwright-cli list --json`, close only the task's session, and retry with a new unique session name.
- **Expired NUS login:** rerun `canvas auth status`. Ask before opening `canvas auth login`.
- **Bootstrap failure:** report the failing command and stderr, then attempt to close only the named task session.
