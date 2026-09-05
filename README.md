# SkillKit for NUS

SkillKit for NUS (`sk4n`) is a small toolkit for everyday NUS student tasks.
It can help you look through Canvas, search NUSMods, browse TalentConnect jobs, 
and give supported AI assistants a reliable way to use the same tools.

## What can it do?

- **Canvas:** sign in with NUS SSO; ckeck courses, read assignments, upcoming
  items, files, announcements, modules, pages, people, and groups.
- **NUSMods:** search courses, view course details and comments, and keep a
  local timetable using NUSMods share links. No login is needed.
- **TalentConnect:** search jobs and companies, including information available
  after signing in with NUS SSO.
- **Agent skills:** install ready-made skills for ChatGPT Work / Codex, GitHub
  Copilot, Claude Code, and Google Antigravity.

## Before you use it

This is an unofficial, independent project. It is not affiliated with,
endorsed by, or supported by the National University of Singapore (NUS),
Instructure/Canvas, NUSMods, Kinobi/TalentConnect, or any supported AI
platform.

It is intended as a personal study and workflow aid. Follow NUS policies and
the terms, rules, and acceptable-use requirements of every platform you access.
AI and CLI output can be incomplete or wrong, so check important information
against the original source. The software comes with no warranty of accuracy,
reliability, or fitness for a particular purpose; see the
[MIT License](https://github.com/hykzr/sk4n/blob/main/LICENSE).

## Install

**Not computing major or don't want to struggle with installation? You can try to ask your agent `help me install https://github.com/hykzr/sk4n/` and let it do everything in this step for you**

You need to install Python 3.11 or newer with pip first.

```bash
pip install sk4n
```

On MacOS or Linux, if you get `command not found: pip`, try

```bash
pip3 install sk4n
```

The one `sk4n` installation gives you four commands:

```text
sk4n
canvas
nusmods
talent-connect
```

### One extra setup step for Canvas and TalentConnect

Canvas and TalentConnect use a small automated browser (chromium) to handle NUS SSO. Set
it up once after installing:

```bash
sk4n browser install chromium
```

sk4n also comes with a `doctor` command to help you check if everything is correctly installed:

```bash
sk4n doctor
```

NUSMods does not need this browser or an NUS login.

## Use it with an AI assistant

The package includes optional agent skills for Canvas, NUSMods, and
TalentConnect. To install them for all supported assistants on your computer:

```bash
sk4n skills install --agents all --scope user
sk4n skills status --agents all --scope all
```

You can choose one assistant instead, for example `--agents codex`. Installing
a skill helps the assistant call the CLI consistently, but it does not make AI
answers automatically correct. Check important dates, requirements, course
information, and job details against the original platform.

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

Canvas and TalentConnect save browser session information so you do not need to
sign in for every command. Treat those session files like login credentials:
do not share them, upload them.

To forget a saved login without signing your other browsers out of NUS SSO:

```bash
canvas auth logout
talent-connect auth logout
```

## If the command is not found

First, close and reopen your terminal after installing. If the command is still
missing, Python's Scripts folder may not be on your `PATH`. 

## Contribute

This section is for contributors working from a repository clone. Ordinary
users do not need any of these tools.

### `uv` workflow

This repo uses `uv` to manage venv. see [uv install](https://docs.astral.sh/uv/getting-started/installation/)

```bash
uv sync
uv run pytest -q
uvx ruff --config pyproject.toml check .
uvx pyright
```

### Optional `just` shortcuts

If [`just`](https://github.com/casey/just#installation) is installed, these
shortcuts are available:

```bash
just test
just lint
just format
```

Neither `just` nor `uv` is needed by people who install `sk4n` from PyPI.
