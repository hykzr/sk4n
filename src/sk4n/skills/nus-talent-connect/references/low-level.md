# TalentConnect API and browser fallback

Start with a minimal Kinobi read, for example:

```bash
talent-connect api /api/auth/
```

For DOM or network details unavailable through API reads, discover browser commands with
`playwright-cli --help` and `playwright-cli --help <command>`. Choose a unique task session and
retain its name:

```bash
talent-connect playwright-cli --session talent-connect-task
playwright-cli -s=talent-connect-task snapshot
playwright-cli -s=talent-connect-task close
```

Put the named close command in cleanup so it runs after failures too. For collisions, inspect
`playwright-cli list --json` and choose a new name; never close unrelated sessions. If bootstrap
fails, report stderr and attempt to close only the task session.

If missing, suggest `npm install -g @playwright/cli@latest` or `playwright-cli install-browser` as
appropriate; do not silently install them.
