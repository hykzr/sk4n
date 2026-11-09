#!/usr/bin/env python3
"""Check workflow actions against the GitHub Advisory Database used by Dependabot.

Resolve workflow action references to release versions and query advisories for those versions.
API, parsing, and action-resolution failures fail the audit rather than skipping coverage.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ACTION_VERSION = re.compile(r"v?(\d+\.\d+\.\d+(?:-[\w.-]+)?)")


def github_pages(endpoint: str, fields: tuple[tuple[str, str], ...] = ()) -> list[dict[str, Any]]:
    command = [
        "gh",
        "api",
        "--hostname",
        "github.com",
        "--method",
        "GET",
        "--paginate",
        "--slurp",
        endpoint,
        "-f",
        "per_page=100",
    ]
    for name, value in fields:
        command.extend(("-f", f"{name}={value}"))
    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=60)
    pages = json.loads(result.stdout)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError(f"Unexpected GitHub API response for {endpoint}")
    return [item for page in pages for item in page]


@cache
def action_version(repository: str, ref: str) -> str:
    if match := ACTION_VERSION.fullmatch(ref):
        return match[1]

    # Major tags and pinned SHAs must be checked as their actual release versions.
    # A SHA passed as a version to the advisory API can silently miss vulnerabilities.
    tags = github_pages(f"repos/{repository}/tags")
    commit = ref if re.fullmatch(r"[a-fA-F0-9]{40}", ref) else None
    for tag in tags:
        if tag["name"] == ref:
            commit = tag["commit"]["sha"]
            break
    for tag in tags:
        if tag["commit"]["sha"] == commit and (match := ACTION_VERSION.fullmatch(tag["name"])):
            return match[1]
    raise ValueError(f"Cannot resolve {repository}@{ref} to a release version")


def action_uses(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "uses":
                if not isinstance(child, str):
                    raise ValueError(f"Invalid action reference: {child!r}")
                yield child
            else:
                yield from action_uses(child)
    elif isinstance(value, list):
        for child in value:
            yield from action_uses(child)


def checkout_actions(root: Path) -> set[str]:
    actions = set()
    workflows = root / ".github" / "workflows"
    for path in sorted(workflows.iterdir()):
        if path.suffix not in {".yml", ".yaml"}:
            continue
        for action in action_uses(yaml.safe_load(path.read_text(encoding="utf-8"))):
            if action.startswith("./"):
                continue
            name, separator, ref = action.rpartition("@")
            if not separator or len(name.split("/")) < 2 or action.startswith("docker://"):
                raise ValueError(f"Unsupported action reference in {path}: {action}")
            repository = "/".join(name.split("/")[:2])
            actions.add(f"{repository}@{action_version(repository, ref)}")
    if not actions:
        raise ValueError("Expected GitHub Actions dependencies in this checkout")
    return actions


def audit(actions: set[str]) -> int:
    findings: dict[str, dict[str, Any]] = {}
    print(f"Checking {len(actions)} GitHub Actions against GitHub advisories...", flush=True)
    versions = sorted(actions)
    # Bound query-string length while checking every action version.
    for start in range(0, len(versions), 50):
        fields = (
            ("ecosystem", "actions"),
            *(("affects[]", version) for version in versions[start : start + 50]),
        )
        for advisory in github_pages("advisories", fields):
            if not advisory.get("withdrawn_at"):
                findings[advisory["ghsa_id"]] = advisory
    for identifier, advisory in sorted(findings.items()):
        print(
            f"{identifier} ({advisory['severity']}): {advisory['summary']}\n  {advisory['html_url']}"
        )
    if findings:
        print(f"Found {len(findings)} GitHub Actions security advisories affecting this checkout.")
        return 1
    print("No GitHub Actions security advisories found for this checkout.")
    return 0


def main() -> int:
    try:
        return audit(checkout_actions(PROJECT_ROOT))
    except subprocess.CalledProcessError as error:
        print(f"GitHub audit failed: {error.stderr.strip()}", file=sys.stderr)
    except (
        OSError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        yaml.YAMLError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"GitHub audit failed: {error}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
