#!/usr/bin/env python3
"""Generate exhaustive, deterministic Agent Skill references from argparse parsers."""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILLS_ROOT = PROJECT_ROOT / "src" / "sk4n" / "skills"


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
        skill_name="nus-canvas",
        module_name="sk4n.canvas.cli",
        environment=(
            COMMON_HOME,
            COMMON_SESSIONS,
            ("CANVAS_BASE_URL", "Set the Canvas origin used by the global --base-url option."),
            ("CANVAS_SITE_NAME", "Set the saved-session namespace used by --site-name."),
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
        default_labels={
            "data_path": "<user-data>/sk4n/talent-connect/talent_connect.sqlite3"
        },
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


def _build_parser(service: Service) -> argparse.ArgumentParser:
    environment_names = {name for spec in SERVICES for name, _description in spec.environment}
    with _without_environment(environment_names):
        module = importlib.import_module(service.module_name)
        return module.build_parser()


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
    return action.__class__.__name__ in {"_AppendAction", "_AppendConstAction", "_CountAction"}


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


def _render_choices(action: argparse.Action) -> str:
    if action.choices is None:
        return "—"
    return ", ".join(map(str, action.choices))


def _render_help(action: argparse.Action, service: Service) -> str:
    if action.help == argparse.SUPPRESS:
        return service.hidden_help.get(action.dest, "Advanced internal option.")
    help_text = action.help or "—"
    if action.dest in service.default_labels:
        help_text = help_text.replace(str(action.default), service.default_labels[action.dest])
    return " ".join(help_text.split())


def _argument_table(parser: argparse.ArgumentParser, service: Service) -> list[str]:
    action_rows = [
        action for action in parser._actions if action.__class__.__name__ != "_SubParsersAction"
    ]
    if not action_rows:
        return ["This command has no arguments."]
    lines = [
        "| Argument | Required | Repeatable | Default | Choices | Description |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for action in action_rows:
        values = (
            f"`{_argument_name(action)}`",
            "yes" if _required(action) else "no",
            "yes" if _repeatable(action) else "no",
            f"`{_render_default(action, service)}`",
            _render_choices(action),
            _render_help(action, service),
        )
        lines.append("| " + " | ".join(_markdown(value) for value in values) + " |")
    return lines


def _mutual_exclusions(parser: argparse.ArgumentParser) -> list[str]:
    exclusions: list[str] = []
    for group in parser._mutually_exclusive_groups:
        names = [_argument_name(action).split(" ", 1)[0] for action in group._group_actions]
        if len(names) > 1:
            exclusions.append(
                "Mutually exclusive: " + " / ".join(f"`{name}`" for name in names) + "."
            )
    return exclusions


def _render_service(service: Service) -> str:
    parser = _build_parser(service)
    prog = parser.prog
    commands = list(_commands(parser, (prog,)))
    structured_output_note = (
        "- JSON and JSONL omit internal `_canvas*` cache metadata but remaining "
        "records may still be substantially larger than human output."
        if service.skill_name == "nus-canvas"
        else "- Structured formats preserve full records and may be substantially larger than human output."
    )
    lines = [
        f"# `{prog}` command reference",
        "",
        "> Generated by `scripts/generate_skill_references.py` from the argparse tree. Do not edit manually.",
        "",
        parser.description or "",
        "",
        "## Output formats",
        "",
        "- Omit `--format` for concise human-readable tables or details.",
        "- Use `--format json` for one complete JSON document.",
        "- Use `--format jsonl` for one complete JSON record per line when the command offers it.",
        "- Use `--format plain` for line-oriented `key: value` records suited to shell filtering when the command offers it.",
        structured_output_note,
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
    lines.extend(_argument_table(parser, service))
    lines.extend(["", "Global options must appear before the command name.", "", "## Commands", ""])
    lines.extend(
        f"- `{' '.join(path)}` — {_markdown(help_text) or 'No description.'}"
        for path, _child, help_text in commands
    )

    for path, child, help_text in commands:
        command_name = " ".join(path)
        lines.extend(["", f"## `{command_name}`", ""])
        if help_text:
            lines.extend([_markdown(help_text), ""])
        usage = " ".join(child.format_usage().removeprefix("usage: ").split())
        lines.extend([f"Usage: `{usage}`", ""])
        lines.extend(_argument_table(child, service))
        constraints = [*_mutual_exclusions(child), *service.constraints.get(command_name, ())]
        if constraints:
            lines.extend(["", "Constraints:", ""])
            lines.extend(f"- {constraint}" for constraint in constraints)
        if child.epilog:
            lines.extend(["", "Notes:", "", _markdown(child.epilog)])
    return "\n".join(lines).rstrip() + "\n"


def generate_documents() -> dict[Path, str]:
    return {
        SKILLS_ROOT / service.skill_name / "references" / "commands.md": _render_service(service)
        for service in SERVICES
    }


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
