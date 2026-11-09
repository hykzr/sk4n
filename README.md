# SkillKit for NUS

SkillKit for NUS (`sk4n`) helps you check Canvas, explore courses on NUSMods, search TalentConnect
jobs, and read or manage NUS Outlook mail. You can use it yourself or let a supported AI assistant
handle these tasks for you.

## Get started

If installation feels unfamiliar, ask your assistant:

> Help me install <https://github.com/hykzr/sk4n/> and set it up for my AI assistant.

To install it yourself, you need Python 3.11 or newer. Open a terminal and run:

```bash
python -m pip install sk4n
```

If your computer cannot find `python`, try `py` on Windows or `python3` on macOS/Linux.

Canvas, TalentConnect, and Outlook also need a browser for NUS sign-in. Set it up once:

```bash
sk4n browser install chromium
```

You complete NUS sign-in and MFA yourself in the browser window. NUSMods needs no login.

## Use an AI assistant

Skills tell your assistant how to use these tools. Install them for ChatGPT Work / Codex, GitHub
Copilot, Claude Code, and Google Antigravity:

```bash
sk4n skills install --agents all --scope user
```

Choose one assistant with, for example, `--agents codex` instead of `--agents all`. You can then ask
things like “Check my Canvas deadlines”, “Find internship opportunities”, or “Find my tutor's NUS
email address”.

For pages the regular tools cannot read, your assistant may also need a separate browser tool. After
[installing Node.js 18 or newer](https://nodejs.org/en/download), run:

```bash
npm install -g @playwright/cli@latest
playwright-cli install-browser
```

## Try it yourself

Paste commands into your terminal. Each tool's guide has more examples:

| Task                    | Example                                                          | Guide                                                                                      |
| ----------------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Find a course           | `nusmods search "machine learning"`                              | [NUSMods](https://github.com/hykzr/sk4n/blob/main/src/sk4n/nusmods/README.md)              |
| Check Canvas            | `canvas auth login`, then `canvas upcoming`                      | [Canvas](https://github.com/hykzr/sk4n/blob/main/src/sk4n/canvas/README.md)                |
| Browse jobs             | `talent-connect fetch --no-login --query engineer --max-jobs 20` | [TalentConnect](https://github.com/hykzr/sk4n/blob/main/src/sk4n/talent_connect/README.md) |
| Read email              | `nus-outlook auth login`, then `nus-outlook messages --limit 10` | [Outlook](https://github.com/hykzr/sk4n/blob/main/src/sk4n/outlook/README.md)              |
| Check the academic week | `sk4n calendar`                                                  | No login needed                                                                            |

## Output for shell users

If you already use Unix pipes, `grep`, or `jq`, most data commands offer `--format`:

- `json`: one JSON document with useful fields.
- `jsonl`: the same fields, one record per line for streaming or line-by-line processing.
- `json-full`: full records, including metadata omitted by the compact formats.
- `plain`: `key: value` lines for shell filtering; nested values remain JSON.

For example, print matching course codes:

```bash
nusmods search programming --format json | jq '.[].moduleCode'
```

AI assistants use these formats to filter large results and read the relevant details. Omit
`--format` for the usual readable output; check each command's `--help` for supported formats.
Direct `api` commands return unfiltered response data instead.

## Your saved data

The tools keep local copies so repeated checks are faster. Canvas, TalentConnect, and Outlook also
remember your sign-in. Run `sk4n paths` to find these folders; treat saved sign-in files like
passwords and do not share them.

To forget a sign-in, run `canvas auth logout`, `talent-connect auth logout`, or
`nus-outlook auth logout`. This leaves your other browsers signed in.

## Need help?

Run `sk4n doctor` to check your setup. If a command is missing after installation, close and reopen
your terminal. If it is still missing, ask your assistant to help with Python's command search path
(`PATH`).

## About this project

This is an unofficial, independent project, unaffiliated with NUS or the platforms it supports.
Follow NUS policies and each platform's rules. Check important dates, requirements, and AI answers
against the original source. The software comes without warranty; see the
[MIT License](https://github.com/hykzr/sk4n/blob/main/LICENSE).

For development setup and repository checks, see the
[maintainer guide](https://github.com/hykzr/sk4n/blob/main/AGENTS.md).
