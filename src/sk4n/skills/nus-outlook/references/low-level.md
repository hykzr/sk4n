# Outlook API and browser fallback

Start with a minimal Outlook read, for example:

```bash
nus-outlook api '/owa/service.svc?action=GetOwaUserConfiguration' -X POST
```

OWA read actions use a JSON `Header`/`Body` envelope whose request types match the action. POST can
be a read. Inspect only the response/payload needed; do not print tokens, headers, or browser
storage.

For DOM or network details, discover commands with `playwright-cli --help` and
`playwright-cli --help <command>`. Choose a unique task session:

```bash
nus-outlook playwright-cli --session outlook-task
playwright-cli -s=outlook-task snapshot
playwright-cli -s=outlook-task requests
playwright-cli -s=outlook-task close
```

Ordinary Outlook page views can mark mail read; use the guarded `preview` command when passive
visual inspection is needed. Browser traces may store request bodies in
`.playwright-cli/traces/resources/`; inspect individual required payloads, never whole logs.

Put the named close command in cleanup so it runs after failures too. For collisions, inspect
`playwright-cli list --json` and choose a new name; never close unrelated sessions. If bootstrap
fails, report stderr and attempt to close only the task session. If missing, suggest
`npm install -g @playwright/cli@latest` or `playwright-cli install-browser` as appropriate.
