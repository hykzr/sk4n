#!/usr/bin/env python3
"""Generate exhaustive, deterministic Agent Skill references from argparse parsers."""

from __future__ import annotations

import argparse
import importlib
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILLS_ROOT = PROJECT_ROOT / "src" / "sk4n" / "skills"
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class Service:
    skill_name: str
    module_name: str
    environment: tuple[tuple[str, str], ...]
    default_labels: Mapping[str, str] = field(default_factory=dict)
    hidden_help: Mapping[str, str] = field(default_factory=dict)
    constraints: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


COMMON_HOME = (
    "SK4N_HOME",
    "Override the stable data root used to derive the service's default data path.",
)
COMMON_SESSIONS = (
    "SK4N_SESSION_DIR",
    "Override the private directory containing saved browser authentication state.",
)

SERVICES = (
    Service(
        skill_name="nus-outlook",
        module_name="sk4n.outlook.cli",
        environment=(
            COMMON_HOME,
            COMMON_SESSIONS,
            ("OUTLOOK_BASE_URL", "Set the Microsoft Outlook mailbox URL used by --base-url."),
            ("OUTLOOK_SITE_NAME", "Set the saved-session namespace used by --site-name."),
            ("OUTLOOK_TIMEOUT", "Set the request and silent login-renewal timeout in seconds."),
        ),
        hidden_help={
            "base_url": "Advanced HTTPS mailbox URL on a supported Microsoft Outlook host.",
            "site_name": "Advanced saved-session namespace.",
        },
        default_labels={"data_path": "<user-data>/sk4n/outlook"},
        constraints={
            "nus-outlook api": (
                "Only /owa/service.svc with a supported read action, POST /searchservice/api/v2/query (mail), or POST /search/api/v2/query (people) is accepted; state-changing actions are rejected.",
            ),
        },
    ),
    Service(
        skill_name="nus-canvas",
        module_name="sk4n.canvas.cli",
        environment=(
            COMMON_HOME,
            COMMON_SESSIONS,
            (
                "CANVAS_BASE_URL",
                "Set the Canvas origin used by the global --base-url option.",
            ),
            (
                "CANVAS_SITE_NAME",
                "Set the saved-session namespace used by --site-name.",
            ),
            ("CANVAS_TIMEOUT", "Set the default HTTP timeout in seconds."),
            (
                "CANVAS_LOGIN_WAIT_SECONDS",
                "Set the default maximum wait for interactive login detection.",
            ),
        ),
        default_labels={"data_path": "<user-data>/sk4n/canvas"},
        hidden_help={
            "site_name": "Advanced saved-session namespace.",
            "login_wait_seconds": "Advanced default login-detection timeout in seconds.",
        },
        constraints={
            "canvas course": (
                "The `path` resource does not accept an item selector.",
                "The `home` resource does not accept an item selector.",
            ),
        },
    ),
    Service(
        skill_name="nusmods",
        module_name="sk4n.nusmods.cli",
        environment=(
            COMMON_HOME,
            ("NUSMODS_ACADEMIC_YEAR", "Set the default course-data academic year."),
            ("NUSMODS_API_BASE_URL", "Set the public NUSMods API origin."),
            ("NUSMODS_TIMEOUT", "Set the default HTTP timeout in seconds."),
            ("NUSMODS_CACHE_TTL", "Set the default API cache lifetime in seconds."),
        ),
        default_labels={
            "data_path": "<user-data>/sk4n/nusmods",
            "academic_year": "current NUSMods academic year",
        },
        hidden_help={"api_base_url": "Advanced public NUSMods API origin."},
    ),
    Service(
        skill_name="nus-talent-connect",
        module_name="sk4n.talent_connect.cli",
        environment=(
            COMMON_HOME,
            COMMON_SESSIONS,
            (
                "TALENT_CONNECT_API_BASE_URL",
                "Set the public Kinobi API origin used by --api-base-url.",
            ),
            (
                "TALENT_CONNECT_APP_BASE_URL",
                "Set the authenticated TalentConnect application origin used by --app-base-url.",
            ),
            (
                "TALENT_CONNECT_SITE_NAME",
                "Set the saved-session namespace used by --site-name.",
            ),
            ("TALENT_CONNECT_TIMEOUT", "Set the default HTTP timeout in seconds."),
        ),
        default_labels={"data_path": "<user-data>/sk4n/talent-connect/talent_connect.sqlite3"},
        hidden_help={
            "api_base_url": "Advanced public Kinobi API origin.",
            "app_base_url": "Advanced authenticated TalentConnect application origin.",
            "site_name": "Advanced saved-session namespace.",
        },
        constraints={
            "talent-connect fetch": (
                "`--updated-only` cannot be combined with `--cached`.",
                "`--no-login` cannot be combined with applied, drafted, saved, `--recommended`, qualified, workflow-status, or `--include-expired-if-applied` filters; `--not-recommended` remains public.",
            ),
            "talent-connect search": (
                "`--no-login` cannot be combined with applied, drafted, saved, `--recommended`, qualified, workflow-status, or `--include-expired-if-applied` filters; `--not-recommended` remains public.",
            ),
        },
    ),
)


def _without_environment(names: Iterable[str]):
    """Temporarily remove CLI override variables while constructing parsers."""

    class EnvironmentGuard:
        def __enter__(self) -> None:
            self.saved = {name: os.environ.pop(name) for name in names if name in os.environ}

        def __exit__(self, *_args: object) -> None:
            os.environ.update(self.saved)

    return EnvironmentGuard()


def _disable_parser_colors(parser: argparse.ArgumentParser) -> None:
    """Make argparse rendering independent of terminal color settings."""
    if hasattr(parser, "color"):
        parser.color = False  # type: ignore
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, Mapping):
            for child in choices.values():
                if isinstance(child, argparse.ArgumentParser):
                    _disable_parser_colors(child)


def _build_parser(service: Service) -> argparse.ArgumentParser:
    environment_names = {name for spec in SERVICES for name, _description in spec.environment}
    with _without_environment(environment_names):
        module = importlib.import_module(service.module_name)
        parser = module.build_parser()
    _disable_parser_colors(parser)
    return parser


def _subparser_action(parser: argparse.ArgumentParser) -> argparse.Action | None:
    return next(
        (action for action in parser._actions if action.__class__.__name__ == "_SubParsersAction"),
        None,
    )


def _command_help(action: argparse.Action) -> dict[str, str]:
    return {
        choice.dest: choice.help
        for choice in getattr(action, "_choices_actions", ())
        if choice.help not in (None, argparse.SUPPRESS)
    }


def _commands(
    parser: argparse.ArgumentParser,
    prefix: tuple[str, ...],
) -> Iterable[tuple[tuple[str, ...], argparse.ArgumentParser, str]]:
    action = _subparser_action(parser)
    if action is None:
        return
    help_by_name = _command_help(action)
    choices = cast(Mapping[str, argparse.ArgumentParser], action.choices)
    for name, child in choices.items():
        path = (*prefix, name)
        yield path, child, help_by_name.get(name, child.description or "")
        yield from _commands(child, path)


def _markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _argument_name(action: argparse.Action) -> str:
    if action.option_strings:
        name = ", ".join(action.option_strings)
        if action.nargs != 0:
            metavar = action.metavar or action.dest.upper()
            if isinstance(metavar, tuple):
                metavar = " ".join(metavar)
            if action.nargs in ("*", "+"):
                metavar = f"{metavar} [{metavar} ...]"
            elif action.nargs == "?":
                metavar = f"[{metavar}]"
            name = f"{name} {metavar}"
        return name

    metavar = action.metavar or action.dest.upper()
    rendered = " ".join(metavar) if isinstance(metavar, tuple) else str(metavar)
    if action.nargs == "?":
        return f"[{rendered}]"
    if action.nargs in ("*", "+"):
        return f"{rendered} [{rendered} ...]"
    return rendered


def _required(action: argparse.Action) -> bool:
    if action.option_strings:
        return bool(action.required)
    return action.nargs not in ("?", "*")


def _repeatable(action: argparse.Action) -> bool:
    return action.__class__.__name__ in {
        "_AppendAction",
        "_AppendConstAction",
        "_CountAction",
    }


def _render_default(action: argparse.Action, service: Service) -> str:
    if action.dest == "help" or action.default is None:
        return "—"
    if action.default == argparse.SUPPRESS:
        return "inherited"
    if action.dest in service.default_labels:
        return service.default_labels[action.dest]
    if isinstance(action.default, bool):
        return str(action.default).lower()
    if isinstance(action.default, (list, tuple)):
        return "[" + ", ".join(map(str, action.default)) + "]"
    return str(action.default)


def _render_help(action: argparse.Action, service: Service) -> str:
    if action.help == argparse.SUPPRESS:
        return service.hidden_help.get(action.dest, "Advanced internal option.")
    help_text = action.help or "—"
    if action.dest == "format" and action.choices and "json-full" in action.choices:
        help_text = (
            "Omit for human output. json/jsonl are compact (JSONL: one record per line); "
            "json-full includes full records; plain emits key: value lines. Cached data is unchanged."
        )
    # Remove only redundant trailing labels, preserving any following instructions.
    if action.default is not None:
        defaults = {str(action.default)}
        if isinstance(action.default, float):
            defaults.add(f"{action.default:g}")
        for default in defaults:
            help_text = re.sub(rf"\s*Default:\s*{re.escape(default)}\.?$", "", help_text).strip()
    if action.choices is not None:
        choices = ", ".join(map(str, action.choices))
        help_text = re.sub(rf"\s*Values:\s*{re.escape(choices)}\.?$", "", help_text).strip()
    if action.dest in service.default_labels:
        help_text = help_text.replace(str(action.default), service.default_labels[action.dest])
    help_text = " ".join(help_text.split())

    # Render paths, URLs, and email examples as inline code so placeholders
    # such as <user-data> need no HTML escaping.
    def render_example(match: re.Match[str]) -> str:
        example = match[0].rstrip(".,;:!?")
        return f"`{example}`{match[0][len(example) :]}"

    return re.sub(
        r"https?://[^\s`]+|[\w.+-]+@[\w.-]+\.[A-Za-z]+|<[^<>]+>[^\s`]*",
        render_example,
        help_text,
    )


def _arguments(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    return [
        action
        for action in parser._actions
        if action.dest != "help" and action.__class__.__name__ != "_SubParsersAction"
    ]


def _argument_details(action: argparse.Action, service: Service) -> str:
    help_text = _render_help(action, service) if action.help else ""
    details = [help_text] if help_text else []
    if _required(action):
        details.append("Required.")
    if _repeatable(action) and "repeat" not in help_text.lower():
        details.append("Repeatable.")
    default = _render_default(action, service)
    if default != "—" and action.nargs != 0 and "Default:" not in help_text:
        details.append(f"Default: `{default}`.")
    if action.choices is not None:
        details.append("Choices: " + ", ".join(f"`{choice}`" for choice in action.choices) + ".")
    return " ".join(details) or "Optional."


def _argument_key(action: argparse.Action, service: Service) -> tuple[str, ...]:
    # Keep variants distinct when a flag's grammar, behavior, or default differs by command.
    return (
        _argument_name(action),
        _argument_details(action, service),
        action.__class__.__name__,
        str(action.nargs),
        repr(action.type),
        repr(action.const),
        _render_default(action, service),
    )


def _shared_arguments(
    parsers: Sequence[argparse.ArgumentParser], service: Service
) -> dict[tuple[str, ...], argparse.Action]:
    actions = [
        action for parser in parsers for action in _arguments(parser) if action.option_strings
    ]
    counts = Counter(_argument_key(action, service) for action in actions)
    shared: dict[tuple[str, ...], argparse.Action] = {}
    for action in actions:
        key = _argument_key(action, service)
        if counts[key] > 1:
            shared[key] = action
    return shared


def _argument_table(
    actions: Iterable[argparse.Action],
    service: Service,
    scopes: Mapping[tuple[str, ...], str] | None = None,
    global_arguments: Sequence[argparse.Action] = (),
) -> list[str]:
    lines = (
        ["| Argument | Commands | Details |", "| --- | --- | --- |"]
        if scopes is not None
        else ["| Argument | Details |", "| --- | --- |"]
    )
    global_definitions = {
        (_argument_name(action), _argument_details(action, service)) for action in global_arguments
    }
    for action in actions:
        scope = f" {scopes[_argument_key(action, service)]} |" if scopes is not None else ""
        details = _argument_details(action, service)
        if (_argument_name(action), details) in global_definitions:
            details = "See [global options](#global-options); also accepted here."
        lines.append(f"| `{_markdown(_argument_name(action))}` |{scope} {_markdown(details)} |")
    return lines


def _mutual_exclusions(parser: argparse.ArgumentParser) -> list[str]:
    exclusions: list[str] = []
    for group in parser._mutually_exclusive_groups:
        names = [action.option_strings[-1] for action in group._group_actions]
        if len(names) > 1:
            exclusions.append(
                ("Choose exactly one: " if group.required else "Mutually exclusive: ")
                + " / ".join(f"`{name}`" for name in names)
                + "."
            )
    return exclusions


def _render_service(service: Service) -> str:
    parser = _build_parser(service)
    prog = parser.prog
    commands = list(_commands(parser, (prog,)))
    shared = _shared_arguments([child for _path, child, _help in commands], service)
    constraints_by_command = {
        " ".join(path): [*_mutual_exclusions(child), *service.constraints.get(" ".join(path), ())]
        for path, child, _help in commands
    }
    constraint_counts = Counter(rule for rules in constraints_by_command.values() for rule in rules)
    lines = [
        f"# `{prog}` command reference",
        "",
        "> Generated by `scripts/generate_skill_references.py` from the argparse tree. Do not edit manually.",
        "",
        parser.description or "",
        "",
        "Arguments/options follow their command; global options precede it. Positional arguments are listed in order. Unmarked arguments are optional; `-h/--help` is available everywhere.",
        *(
            [
                "",
                "Low-level `api` output is unfiltered response data and has no `--format` option.",
            ]
            if service.skill_name != "nusmods"
            else []
        ),
        "",
        "## Environment variables",
        "",
        "| Variable | Effect |",
        "| --- | --- |",
    ]
    lines.extend(
        f"| `{name}` | {_markdown(description)} |" for name, description in service.environment
    )
    lines.extend(["", "## Global options", ""])
    lines.extend(_argument_table(_arguments(parser), service))
    if parser.epilog:
        lines.extend(["", _markdown(parser.epilog)])
    if shared:
        lines.extend(["", "## Shared options", ""])
        scopes = {
            key: ", ".join(
                f"`{' '.join(path[1:])}`"
                for path, child, _help in commands
                if any(_argument_key(action, service) == key for action in _arguments(child))
            )
            for key in shared
        }
        lines.extend(_argument_table(shared.values(), service, scopes, _arguments(parser)))
    if any(count > 1 for count in constraint_counts.values()):
        lines.extend(["", "## Shared constraints", "", "| Commands | Rule |", "| --- | --- |"])
        for rule, count in constraint_counts.items():
            if count > 1:
                scopes = ", ".join(
                    f"`{name.removeprefix(prog + ' ')}`"
                    for name, rules in constraints_by_command.items()
                    if rule in rules
                )
                lines.append(f"| {scopes} | {_markdown(rule)} |")

    for path, child, help_text in commands:
        command_name = " ".join(path)
        lines.extend(["", f"## `{command_name}`", ""])
        if help_text:
            lines.extend([_markdown(help_text), ""])
        subcommands = _subparser_action(child)
        if subcommands is not None:
            choices = ", ".join(
                f"`{name}`"
                for name in cast(Mapping[str, argparse.ArgumentParser], subcommands.choices)
            )
            label = "Required subcommand" if subcommands.required else "Optional subcommand"
            lines.extend([f"{label}: {choices}.", ""])
        local = [
            action for action in _arguments(child) if _argument_key(action, service) not in shared
        ]
        if local:
            lines.extend(_argument_table(local, service, global_arguments=_arguments(parser)))
        common = [
            action for action in _arguments(child) if _argument_key(action, service) in shared
        ]
        if common:
            names = ", ".join(f"`{action.option_strings[-1]}`" for action in common)
            lines.extend(["", f"Also accepts [shared options](#shared-options): {names}."])
        constraints = [
            rule for rule in constraints_by_command[command_name] if constraint_counts[rule] == 1
        ]
        if constraints:
            lines.append("")
            lines.extend(f"- {constraint}" for constraint in constraints)
        if child.epilog:
            lines.extend(["", _markdown(child.epilog)])
    # Parser descriptions/help can contain ANSI escapes supplied by the environment.
    return ANSI_ESCAPE_RE.sub("", "\n".join(lines)).rstrip() + "\n"


def generate_documents() -> dict[Path, str]:
    return {
        SKILLS_ROOT / service.skill_name / "references" / "commands.md": _render_service(service)
        for service in SERVICES
    }


def _format_markdown(path: Path, content: str) -> str:
    prettier = PROJECT_ROOT / "node_modules" / "prettier" / "bin" / "prettier.cjs"
    if not prettier.is_file():
        raise RuntimeError("Install the locked Markdown tools with `npm ci` first.")
    return subprocess.run(
        ["node", str(prettier), "--stdin-filepath", str(path)],
        input=content,
        stdout=subprocess.PIPE,
        check=True,
        text=True,
        cwd=PROJECT_ROOT,
    ).stdout


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if a generated reference differs from the checked-in file.",
    )
    args = parser.parse_args(argv)

    stale: list[Path] = []
    for path, content in generate_documents().items():
        content = _format_markdown(path, content)
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(path.relative_to(PROJECT_ROOT))

    if stale:
        for path in stale:
            print(f"stale: {path.relative_to(PROJECT_ROOT)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
