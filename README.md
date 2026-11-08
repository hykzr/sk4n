# SkillKit for NUS

SkillKit for NUS (`sk4n`) is a small toolkit for everyday NUS student tasks. It can help you look
through Canvas, search NUSMods, browse TalentConnect jobs, read NUS Outlook mail, and give supported
AI assistants a reliable way to use the same tools.

## What can it do?

- **Canvas:** sign in with NUS SSO; ckeck courses, read assignments, upcoming items, files,
  announcements, modules, pages, people, and groups.
- **NUSMods:** search courses, view course details and comments, and keep a local timetable using
  NUSMods share links. No login is needed.
- **TalentConnect:** search jobs and companies, including information available after signing in
  with NUS SSO.
- **NUS Outlook:** sign in with NUS SSO and MFA, list folders, search mail, and read messages with
  incremental local caching without marking them as read; look up people's email addresses by name
  in the NUS directory; manage messages and create, edit, preview, and send drafts with attachments.
  Saved browser state renews tokens silently when Microsoft permits it.
- **Agent skills:** install ready-made skills for ChatGPT Work / Codex, GitHub Copilot, Claude Code,
  and Google Antigravity.

## Before you use it

This is an unofficial, independent project. It is not affiliated with, endorsed by, or supported by
the National University of Singapore (NUS), Instructure/Canvas, NUSMods, Kinobi/TalentConnect,
Microsoft/Outlook, or any supported AI platform.

It is intended as a personal study and workflow aid. Follow NUS policies and the terms, rules, and
acceptable-use requirements of every platform you access. AI and CLI output can be incomplete or
wrong, so check important information against the original source. The software comes with no
warranty of accuracy, reliability, or fitness for a particular purpose; see the
[MIT License](https://github.com/hykzr/sk4n/blob/main/LICENSE).

## Install

**Not computing major or don't want to struggle with installation? You can try to ask your agent
`help me install https://github.com/hykzr/sk4n/` and let it do everything in this step for you**

You need to install Python 3.11 or newer with pip first.

```bash
python -m pip install sk4n
```

If this command fail because the system cannot find `python` command, try replacing `python` with
`py` (for Windows), or `python3` (on MacOS or Linux)

The one `sk4n` installation gives you five commands:

```text
sk4n
canvas
nusmods
talent-connect
nus-outlook
```

### One extra setup step for Canvas, TalentConnect, and Outlook

Canvas, TalentConnect, and Outlook use a small automated browser (chromium) to handle NUS SSO. Set
it up once after installing:

```bash
sk4n browser install chromium
```

NUSMods does not need this browser or an NUS login.

### One extra setup step for AI assistants

Sometimes an AI assistant needs to look at a Canvas, TalentConnect, or Outlook page that the regular
commands cannot fully read. For those cases, sk4n uses `playwright-cli`, a browser tool made for
coding agents.

`playwright-cli` needs Node.js 18 or newer. After
[installing Node.js](https://nodejs.org/en/download), run:

```bash
npm install -g @playwright/cli@latest
playwright-cli install-browser
```

This is separate from the Chromium setup above: that browser handles your NUS login, while this one
lets your AI assistant inspect a signed-in page when needed.

sk4n also comes with a `doctor` command to help you check if everything is correctly installed:

```bash
sk4n doctor
```

## Use it with an AI assistant

The package includes optional agent skills for Canvas, NUSMods, TalentConnect, and NUS Outlook. To
install them for all supported assistants on your computer:

```bash
sk4n skills install --agents all --scope user
sk4n skills status --agents all --scope all
```

You can choose one assistant instead, for example `--agents codex`. Installing a skill helps the
assistant call the CLI consistently, but it does not make AI answers automatically correct. Check
important dates, requirements, course information, and job details against the original platform.

## CLI examples

### Find courses with NUSMods

```bash
nusmods search "machine learning"
nusmods course CS1010
```

You can also import an existing timetable share link:

```bash
nusmods schedule import 'https://nusmods.com/timetable/sem-1/share?...'
```

[More NUSMods examples](https://github.com/hykzr/sk4n/blob/main/src/sk4n/nusmods/README.md)

### Check Canvas

The first login opens a browser window for NUS SSO:

```bash
canvas auth login
canvas list
canvas todo
canvas upcoming
```

Once a course has been synced, you can inspect its saved content:

```bash
canvas course CS1010 assignments list
canvas course CS1010 announcements list
```

[More Canvas examples](https://github.com/hykzr/sk4n/blob/main/src/sk4n/canvas/README.md)

### Search TalentConnect

```bash
talent-connect auth login
talent-connect fetch --query engineer --max-jobs 20
talent-connect fetch --saved
```

For a public search that does not open the NUS login:

```bash
talent-connect fetch --no-login --query engineer --max-jobs 20
```

[More TalentConnect examples](https://github.com/hykzr/sk4n/blob/main/src/sk4n/talent_connect/README.md)

### Read and manage NUS Outlook mail

```bash
nus-outlook auth login
nus-outlook auth status --format json
nus-outlook folders
nus-outlook messages --unread --limit 10
nus-outlook search 'subject:internship' --format json
nus-outlook message 'MESSAGE_ID' --format json
nus-outlook message 'MESSAGE_ID' --attachments
nus-outlook messages --folder 'Sent Items' --limit 10
nus-outlook messages --folder-id 'FOLDER_ID' --limit 10
nus-outlook messages --limit 10 --no-refresh
nus-outlook people 'INSTRUCTOR NAME' --limit 10 --format json
nus-outlook draft create --to person@example.com --subject 'Draft subject' --body 'Unsent text'
nus-outlook preview 'DRAFT_ID' --output preview.png --format json
nus-outlook draft send 'DRAFT_ID'
```

If Microsoft account discovery needs help, use
`nus-outlook auth login --login-hint NUS-ID@u.nus.edu` for students, or use `NUS-ID@nus.edu.sg` for
staff. The saved login is reused across commands; a headless browser silently renews expired tokens
when possible. NUS/Microsoft can still require fresh interactive sign-in or MFA.

[More Outlook examples](https://github.com/hykzr/sk4n/blob/main/src/sk4n/outlook/README.md)

### Check the NUS academic week

```bash
sk4n calendar
sk4n calendar --date 2026-08-14
```

This uses public NUSMods calendar data and does not need an NUS login.

## Where is my data?

Run this to see the exact folders used on your computer:

```bash
sk4n paths
```

Canvas, TalentConnect, and Outlook save browser session information so you do not need to sign in
for every command. Treat those session files like login credentials: do not share or upload them.

To forget a saved login without signing your other browsers out of NUS SSO:

```bash
canvas auth logout
talent-connect auth logout
nus-outlook auth logout
```

## If the command is not found

First, close and reopen your terminal after installing. If the command is still missing, Python's
Scripts folder may not be on your `PATH`.

## Contribute

This section is for contributors working from a repository clone. Ordinary users do not need any of
these tools.

### `uv` workflow

This repo uses `uv` to manage venv. see
[uv install](https://docs.astral.sh/uv/getting-started/installation/)

```bash
uv sync
npm ci # requires Node.js 22+; also used by the reference generator
uv run pytest -q
uv run ruff --config pyproject.toml check .
uv run pyright
uv run python scripts/generate_skill_references.py --check
uv run pip-audit --strict --progress-spinner off .
```

### Markdown checks and VS Code

Markdown uses [markdownlint-cli2](https://github.com/DavidAnson/markdownlint-cli2) with the standard
markdownlint rules and [Prettier](https://prettier.io/) for formatting. Prose wraps to a 100-column
target; tables and code blocks can be wider. Embedded code formatting is disabled. Prettier checks
wrapping instead of markdownlint's `MD013` rule, which can flag unbreakable inline commands and
links. Install Node.js 22 or newer, then install the locked development tools:

```bash
npm ci
npm run lint:markdown
npm run format:markdown
```

The fix command corrects automatically-fixable issues; remaining lint errors need manual edits.
Generated CLI references are checked too; change `scripts/generate_skill_references.py` and
regenerate them instead of editing generated files directly. The generator uses the same local
Prettier version and configuration, so run `npm ci` before generating or checking references.

The npm overrides pin patched KaTeX and RE2JS dependencies while upstream markdownlint still pins
older versions.

In VS Code, install the recommended
[markdownlint extension](https://marketplace.visualstudio.com/items?itemName=DavidAnson.vscode-markdownlint)
(`DavidAnson.vscode-markdownlint`) and
[Prettier extension](https://marketplace.visualstudio.com/items?itemName=esbenp.prettier-vscode)
(`esbenp.prettier-vscode`). Markdownlint reads `.markdownlint-cli2.jsonc`; Prettier uses the locally
installed version, `.prettierrc.json`, and `.prettierignore`, just like the CLI. Workspace settings
enable Markdown formatting and automatic lint fixes on save. Keep rule changes in the shared configs
so the editor and CI use the same policy.

### Optional `just` shortcuts

If [`just`](https://github.com/casey/just#installation) is installed, these shortcuts are available:

```bash
just format
just lint-markdown
just format-markdown
just check # same as test + lint + check references
just build
```

Neither `just` nor `uv` is needed by people who install `sk4n` from PyPI.
