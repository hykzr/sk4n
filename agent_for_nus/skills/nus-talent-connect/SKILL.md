---
name: nus-talent-connect
description: Safely search NUS TalentConnect/Kinobi jobs and companies, inspect job details, local saved records, applications, offers, qualification, and workflow status, and investigate authenticated endpoints or pages with the talent-connect CLI. Use for public or personalized job searches, cached/offline job data, company research, and TalentConnect API, DOM, or network inspection.
---

# NUS TalentConnect

Use the installed `talent-connect` command for NUS TalentConnect/Kinobi reads and local persistence.

## Workflow

1. Verify `talent-connect` exists on `PATH`. If it is missing, tell the user to install `agent-for-nus`; do not clone or install software silently.
2. Before constructing or running any `talent-connect` CLI command, read [references/commands.md](references/commands.md), including for seemingly familiar commands. Treat it as the authoritative command grammar; do not infer subcommands, options, argument placement, filters, or defaults.
3. For personalized filters or authenticated data, run `talent-connect auth status` first. Ask before `talent-connect auth login`, because login opens an interactive NUS SSO browser. Use `--no-login` only for explicitly public searches.
4. Prefer high-level `fetch`, `search`, `job`, and `company` commands. Use `--cached` or `--no-refresh` when the user explicitly requests local/offline results, or in subsequent and repeated queries.
5. `--posted-after` is applied locally and may require scanning all remote matches before `--max-jobs` or `--max-results` can limit the result.
6. Use human output for a simple check, `--format json` or `jsonl` for parsing, and `--format plain` for line-oriented shell filtering. Expect JSON formats to expose complete records and potentially be large.
7. Use `talent-connect api` only when high-level commands are insufficient. Require explicit approval immediately before any request that can change remote state.
8. Read [references/low-level.md](references/low-level.md) and use an authenticated `playwright-cli` session only when the API cannot provide required page, DOM, or network details. Always close the named session.
9. Return the source command and relevant local artifact paths when they help the user verify the result.

## Persistence and safety rules

- Preserve the default database so updates remain incremental. Do not choose a temporary data path, delete local records, clear sessions, or log out unless the user asks.
- Do not apply, bookmark, message, upload, withdraw, accept or decline an offer, or otherwise change remote state without explicit user approval.
- Treat application, qualification, and workflow status as personal data. Return only what the task requires and never expose saved cookies or session material.
