# Maintainer guide

This guide is for repository maintainers. Bundled and globally installed NUS skills are for end-user
assistants; do not use them as maintainer instructions.

## Dependencies and initial setup

Install Git, [uv](https://docs.astral.sh/uv/getting-started/installation/), Node.js 22+ with npm,
and [just](https://github.com/casey/just#installation). Python 3.11+ is required; uv can provision
it. From the repository root:

```bash
uv sync       # Install Python runtime and development dependencies into .venv
npm ci        # Install locked Markdown tools, also required by the reference generator
just check    # Run tests, lint/type checks, and generated-reference checks
```

For live sign-in/browser investigation, also install the two browser tools:

```bash
uv run sk4n doctor # Run the commands below if the two browser is not installed
uv run sk4n browser install chromium  # Browser used by the integrations for NUS sign-in
npm install -g @playwright/cli@latest # Separate agent browser inspection tool
playwright-cli install-browser
```

Use `uv run` for repository commands; no manual virtual-environment activation is needed. See
`justfile` for check/build recipes and prefer them unchanged, including `just build`.

## Structure and formatting

- `src/sk4n/{canvas,talent_connect,outlook,nusmods}/`: platform integrations. Read the relevant
  platform `AGENTS.md` before changing its integration or bundled skill.
- `src/sk4n/tools/`: genuinely shared request, authentication, and browser helpers. Keep
  platform-specific behavior in its platform package.
- `src/sk4n/skills/`: packaged end-user skills and references; `tests/`: unit suite.
- Markdown uses shared markdownlint/Prettier configs and 100-column prose wrapping. Run
  `just format-markdown` after edits. Generate references with `just references`; never edit them
  directly. The generator needs the locked npm tools.

## Documentation boundaries

Keep each document concise and specific to its audience. Give each fact one home within an
audience's documentation; link to it elsewhere. Do not copy code inventories or CLI help into prose.

| Document                  | Audience and content                                                                                                                                                                                         |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Root `README.md`          | Students, including those without a computing background. Plain language; purpose, setup, everyday examples, and a brief optional output-format guide for shell users.                                       |
| Root `CHANGELOG.md`       | Students reading release notes. Brief user-visible features/fixes; modest technical detail only when useful.                                                                                                 |
| Root `AGENTS.md`          | Code maintainers. Universal requirements, repository structure, and shared workflow only.                                                                                                                    |
| Platform `AGENTS.md`      | Maintainers of that integration. Exploration guidance needed before reading code, restrictions, and non-obvious external facts or reasons for decisions. Omit implementation details discoverable from code. |
| Platform `README.md`      | Human users. Cover every named high-level command with its purpose and a concise example/comment. Include useful limitations; omit agent-only internals and exhaustive option tables.                        |
| Platform `SKILL.md`       | End-user agents. Minimal interface routing, workflow, and non-generated decision/approval rules. Omit implementation details and generated command grammar.                                                  |
| `references/commands.md`  | Generated interface reference. Syntax, options, defaults, and constraints; define shared information once. Change the parser/generator and regenerate.                                                       |
| `references/low-level.md` | End-user agents using direct API/browser commands. Only fallback procedures absent from `SKILL.md` and generated help; no repeated authentication workflow or option catalog.                                |

Documentation is not a work log: no completion reports, verification narratives, or “what I did”
sections. Report work and checks in the reply. Record new features/fixes concisely in
`CHANGELOG.md`; update other docs only when their purpose requires it. Delete stale or redundant
material instead of moving it wholesale into another file.

Preserve requirement strength when shortening skills: “explicitly requested/asked” must stay
explicit. Skills must gate reading `low-level.md` on relevant named high-level commands failing to
provide the required result; do not load fallback guidance in advance.

## Remote investigation

Before contacting Canvas, TalentConnect, or Outlook, run `uv run <cli> auth status` with `canvas`,
`talent-connect`, or `nus-outlook`. If unauthenticated and access is needed, ask approval before
`uv run <cli> auth login`; the user completes NUS SSO/MFA. NUSMods is public and needs no login.

Prefer the repository's `api` command over raw curl or app connectors. For page/DOM/network details,
use `<cli> playwright-cli --session <task-name>`; it restores the durable saved login. Continue with
`playwright-cli -s=<task-name> ...` and close that named session when finished. Avoid separate
Canvas/TalentConnect app or browser connectors whose login is not persisted for repository commands.

Read-only live API and repository browser checks are authorized and needed when changing remote
interfaces; mocks alone do not validate them. Real remote writes require explicit user approval.
Never expose saved credentials or whole authenticated network logs.

## Real-life tool/skill tests

When explicitly asked to test a tool and skill on a real task:

1. First complete the task using only `src/sk4n/skills/<feature>/` documentation, not implementation
   code or globally installed copies. Prefix documented CLI calls with `uv run`.
2. Follow that skill's authentication/approval rules; stop inspection immediately if not logged in.
3. Write `temp/actual-test/<feature>.md`: the task answer, documentation gaps or wrong assumptions
   causing retries, named high-level command failures requiring low-level fallback, and other
   issues.
