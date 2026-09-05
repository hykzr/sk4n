from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from .paths import (
    canvas_data_dir,
    home_dir,
    nusmods_data_dir,
    sessions_dir,
    talent_connect_data_dir,
    talent_connect_database_path,
)
from .skill_install import (
    AGENTS,
    NUS_SKILLS,
    find_project_root,
    known_skill_roots,
    skill_status_report,
)

CONSOLE_SCRIPTS = ("sk4n", "canvas", "nusmods", "talent-connect")
MINIMUM_NODE_MAJOR = 18


@dataclass(frozen=True)
class Check:
    id: str
    status: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    remediation: str | None = None


def package_version() -> str:
    try:
        return version("sk4n")
    except PackageNotFoundError:
        return "unknown"


def path_report() -> dict[str, str]:
    return {
        "home": str(home_dir()),
        "sessions": str(sessions_dir()),
        "canvas": str(canvas_data_dir()),
        "nusmods": str(nusmods_data_dir()),
        "talent_connect": str(talent_connect_data_dir()),
        "talent_connect_database": str(talent_connect_database_path()),
    }


def _run(
    command: list[str],
    *,
    timeout: float = 15,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )


def _result_text(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or result.stderr).strip()


def _same_interpreter(script: Path) -> bool | None:
    """Compare a POSIX console-script shebang with this interpreter when possible."""
    try:
        first_line = script.read_bytes().splitlines()[0].decode("utf-8")
    except (OSError, UnicodeDecodeError, IndexError):
        return None
    if not first_line.startswith("#!"):
        return None
    interpreter = Path(first_line[2:].strip().split()[0])
    if interpreter.name == "env":
        return None
    try:
        return interpreter.samefile(sys.executable)
    except OSError:
        return False


def _console_script_check() -> Check:
    scripts: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    incompatible: list[str] = []
    for name in CONSOLE_SCRIPTS:
        resolved = shutil.which(name)
        if not resolved:
            scripts[name] = {"path": None, "compatible": False}
            missing.append(name)
            continue
        path = Path(resolved).resolve()
        compatible = _same_interpreter(path)
        scripts[name] = {"path": str(path), "compatible": compatible}
        if compatible is False:
            incompatible.append(name)
    if missing:
        return Check(
            "console_scripts",
            "error",
            f"Missing console scripts on PATH: {', '.join(missing)}",
            {"scripts": scripts},
            "Reinstall with `python -m pip install --force-reinstall sk4n`.",
        )
    if incompatible:
        return Check(
            "console_scripts",
            "error",
            f"Console scripts use another Python installation: {', '.join(incompatible)}",
            {"scripts": scripts},
            "Remove stale scripts from PATH, then reinstall sk4n.",
        )
    return Check(
        "console_scripts", "ok", "All console scripts resolve on PATH.", {"scripts": scripts}
    )


def _existing_ancestor(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _data_directory_check() -> Check:
    paths = [
        home_dir(),
        sessions_dir(),
        canvas_data_dir(),
        nusmods_data_dir(),
        talent_connect_data_dir(),
    ]
    details: dict[str, Any] = {}
    unwritable: list[str] = []
    for path in paths:
        probe = path if path.exists() else _existing_ancestor(path)
        writable = probe.is_dir() and os.access(probe, os.W_OK | os.X_OK)
        details[str(path)] = {
            "exists": path.exists(),
            "writable": writable,
            "checked_at": str(probe),
        }
        if not writable:
            unwritable.append(str(path))

    insecure: list[str] = []
    session_path = sessions_dir()
    if os.name == "posix" and session_path.exists():
        directory_mode = stat.S_IMODE(session_path.stat().st_mode)
        details[str(session_path)]["mode"] = f"{directory_mode:04o}"
        if directory_mode & 0o077:
            insecure.append(str(session_path))
        for item in sorted(session_path.glob("*.json")):
            mode = stat.S_IMODE(item.stat().st_mode)
            details[f"session_file:{item}"] = {"mode": f"{mode:04o}"}
            if mode & 0o077:
                insecure.append(str(item))

    if unwritable:
        return Check(
            "data_directories",
            "error",
            "One or more application directories are not writable.",
            {"paths": details, "unwritable": unwritable},
            "Fix ownership/permissions or set SK4N_HOME to a writable directory.",
        )
    if insecure:
        return Check(
            "data_directories",
            "error",
            "Saved-session permissions allow access by other users.",
            {"paths": details, "insecure": insecure},
            "Restrict the sessions directory to mode 0700 and session JSON files to 0600.",
        )
    return Check(
        "data_directories",
        "ok",
        "Application paths are writable and saved-session permissions are secure.",
        {"paths": details},
    )


def _chromium_install_location(output: str) -> Path | None:
    chromium_block = re.search(
        r"(?m)^.*\(playwright chromium v\d+\)\s*$\n\s*Install location:\s*(.+?)\s*$",
        output,
    )
    if chromium_block:
        return Path(chromium_block.group(1)).expanduser()
    fallback = re.search(r"(?m)^\s*Install location:\s*(.+?)\s*$", output)
    return Path(fallback.group(1)).expanduser() if fallback else None


def _chromium_executable(install_location: Path) -> Path | None:
    relative_candidates = (
        "chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chrome-linux/chrome",
        "chrome-linux64/chrome",
        "chrome-win/chrome.exe",
        "chrome-win64/chrome.exe",
    )
    for relative in relative_candidates:
        candidate = install_location / relative
        if candidate.is_file():
            return candidate
    return None


def python_playwright_check() -> Check:
    if importlib.util.find_spec("playwright") is None:
        return Check(
            "python_playwright",
            "error",
            "Python Playwright cannot be imported.",
            remediation="Reinstall sk4n so its Python dependencies are restored.",
        )
    command = [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"]
    try:
        result = _run(command)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(
            "python_playwright",
            "error",
            "Python Playwright could not report its expected Chromium installation.",
            {"importable": True, "error": str(exc)},
            "Reinstall sk4n so its Python dependencies are restored.",
        )
    details: dict[str, Any] = {
        "importable": True,
        "playwright_version": version("playwright"),
        "probe_returncode": result.returncode,
    }
    if result.returncode:
        details["error"] = _result_text(result)
        return Check(
            "python_playwright",
            "error",
            "Python Playwright could not report its expected Chromium installation.",
            details,
            "Reinstall sk4n so its Python dependencies are restored.",
        )
    install_location = _chromium_install_location(result.stdout)
    if install_location is None:
        details["error"] = "Playwright output did not contain a Chromium install location."
        return Check(
            "python_playwright",
            "error",
            "Python Playwright returned an unrecognized browser-installation report.",
            details,
            "Run `sk4n browser install chromium`, then retry doctor.",
        )
    executable = _chromium_executable(install_location)
    details["chromium_install_location"] = str(install_location)
    details["chromium_executable"] = str(executable) if executable else None
    if executable is not None and os.access(executable, os.X_OK):
        return Check(
            "python_playwright",
            "ok",
            "Python Playwright and its Chromium executable are available.",
            details,
        )
    return Check(
        "python_playwright",
        "error",
        "Python Playwright is installed, but its Chromium executable is missing.",
        details,
        "Run `sk4n browser install chromium`.",
    )


def _node_check() -> Check:
    executable = shutil.which("node")
    if not executable:
        return Check(
            "node",
            "error",
            "Node.js is not available on PATH.",
            remediation="Install Node.js 18 or newer.",
        )
    try:
        result = _run([executable, "--version"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("node", "error", "Node.js could not be executed.", {"error": str(exc)})
    text = _result_text(result)
    try:
        major = int(text.removeprefix("v").split(".", 1)[0])
    except ValueError:
        major = 0
    details = {"path": executable, "version": text, "minimum_major": MINIMUM_NODE_MAJOR}
    if result.returncode == 0 and major >= MINIMUM_NODE_MAJOR:
        return Check("node", "ok", f"Node.js {text} is available.", details)
    return Check(
        "node",
        "error",
        f"Node.js 18+ is required; detected {text or 'an unknown version'}.",
        details,
        "Install or activate Node.js 18 or newer.",
    )


def _playwright_cli_check() -> Check:
    executable = shutil.which("playwright-cli")
    if not executable:
        return Check(
            "playwright_cli",
            "error",
            "playwright-cli is not available on PATH.",
            remediation=(
                "Run `npm install -g @playwright/cli@latest`, then "
                "`playwright-cli install-browser`."
            ),
        )
    commands = {
        "version": [executable, "--version"],
        "help": [executable, "--help"],
        "list": [executable, "list", "--json"],
    }
    details: dict[str, Any] = {"path": executable}
    failures: list[str] = []
    for label, command in commands.items():
        try:
            result = _run(command)
            command_details: dict[str, Any] = {"returncode": result.returncode}
            if label == "version" or result.returncode:
                command_details["output"] = _result_text(result)
            details[label] = command_details
            if result.returncode:
                failures.append(label)
            elif label == "list":
                try:
                    payload = json.loads(result.stdout)
                    browsers = payload.get("browsers") if isinstance(payload, dict) else None
                    if not isinstance(browsers, list):
                        failures.append(label)
                    else:
                        command_details["session_count"] = len(browsers)
                except json.JSONDecodeError:
                    failures.append(label)
        except (OSError, subprocess.TimeoutExpired) as exc:
            details[label] = {"error": str(exc)}
            failures.append(label)
    if failures:
        return Check(
            "playwright_cli",
            "error",
            f"playwright-cli checks failed: {', '.join(dict.fromkeys(failures))}.",
            details,
            "Reinstall @playwright/cli and run `playwright-cli install-browser`.",
        )
    cli_version = details["version"]["output"]
    return Check(
        "playwright_cli",
        "ok",
        f"playwright-cli is operational ({cli_version}).",
        details,
    )


def directory_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(
        (item for item in path.rglob("*") if item.is_file()), key=lambda p: p.as_posix()
    ):
        relative = item.relative_to(path).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        content = item.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def skills_check() -> Check:
    report = skill_status_report(agents=AGENTS, scope="all")
    known = report["known_installations"]
    current_count = sum(item["state"] == "current" for item in known)
    problem_count = sum(item["state"] != "current" for item in known)
    current_skills = {item["skill"] for item in known if item["state"] == "current"}
    missing_skills = sorted(set(NUS_SKILLS) - current_skills)
    if missing_skills:
        status = "warning"
        summary = f"No current installed copy was found for: {', '.join(missing_skills)}."
        remediation = "Run `sk4n skills install --agents all --scope user`."
    elif problem_count or report["duplicates"]:
        status = "warning"
        summary = f"Found {current_count} current NUS skill copy/copies, with stale, unmanaged, or duplicate copies."
        remediation = "Run `sk4n skills status`, then update or remove duplicate copies."
    else:
        status = "ok"
        summary = f"Found {current_count} current installed NUS skill copy/copies."
        remediation = None
    return Check(
        "nus_skills",
        status,
        summary,
        report,
        remediation,
    )


def _playwright_skill_check() -> Check:
    roots = known_skill_roots(project_root=find_project_root())
    found: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for root in roots:
        root_path = root.root
        if not root_path.is_dir():
            continue
        for path in sorted(root_path.glob("*playwright*")):
            resolved = path.resolve()
            if not path.is_dir() or resolved in seen:
                continue
            seen.add(resolved)
            skill_file = path / "SKILL.md"
            detected_version = None
            if skill_file.is_file():
                for line in skill_file.read_text(encoding="utf-8", errors="replace").splitlines()[
                    :30
                ]:
                    if line.lower().startswith("version:"):
                        detected_version = line.split(":", 1)[1].strip().strip("\"'") or None
                        break
            found.append(
                {
                    "scope": root.scope,
                    "agents": list(root.agents),
                    "path": str(path),
                    "version": detected_version,
                    "sha256": directory_hash(path),
                }
            )
    if found:
        return Check(
            "playwright_cli_skill",
            "ok",
            f"Detected {len(found)} upstream Playwright skill copy/copies.",
            {"installations": found},
        )
    return Check(
        "playwright_cli_skill",
        "warning",
        "No upstream Playwright CLI skill was detected in known skill roots.",
        {"installations": []},
        "Optional: run `playwright-cli install --skills`.",
    )


def browser_smoke_check() -> Check:
    executable = shutil.which("playwright-cli")
    if not executable:
        return Check(
            "browser_smoke",
            "error",
            "Browser smoke test could not start because playwright-cli is missing.",
        )
    session = f"sk4n-doctor-{uuid.uuid4().hex[:12]}"
    details: dict[str, Any] = {"session": session}
    failure: str | None = None
    try:
        opened = _run([executable, f"-s={session}", "open", "about:blank"], timeout=30)
        details["open_returncode"] = opened.returncode
        if opened.returncode:
            failure = _result_text(opened) or f"open exited {opened.returncode}"
        else:
            snapshot = _run([executable, f"-s={session}", "snapshot"], timeout=30)
            details["snapshot_returncode"] = snapshot.returncode
            if snapshot.returncode:
                failure = _result_text(snapshot) or f"snapshot exited {snapshot.returncode}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        failure = str(exc)
    finally:
        try:
            closed = _run([executable, f"-s={session}", "close"], timeout=15)
            details["close_returncode"] = closed.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            details["close_error"] = str(exc)
            if failure is None:
                failure = f"session cleanup failed: {exc}"
    if failure:
        details["error"] = failure
        return Check(
            "browser_smoke",
            "error",
            "The playwright-cli browser smoke test failed.",
            details,
            "Run `playwright-cli install-browser` and retry.",
        )
    return Check(
        "browser_smoke",
        "ok",
        "The playwright-cli browser opened, responded, and closed successfully.",
        details,
    )


def _safe_check(identifier: str, factory: Callable[[], Check]) -> Check:
    try:
        return factory()
    except Exception as exc:
        return Check(
            identifier,
            "error",
            f"The {identifier.replace('_', ' ')} diagnostic could not complete.",
            {"error": str(exc)},
        )


def build_doctor_report(*, browser_smoke: bool = False) -> dict[str, Any]:
    checks = [
        Check(
            "runtime",
            "ok",
            f"sk4n {package_version()} on Python {sys.version.split()[0]}.",
            {
                "package_version": package_version(),
                "python_version": sys.version.split()[0],
                "python_executable": sys.executable,
            },
        ),
        _safe_check("console_scripts", _console_script_check),
        _safe_check("data_directories", _data_directory_check),
        _safe_check("python_playwright", python_playwright_check),
        _safe_check("node", _node_check),
        _safe_check("playwright_cli", _playwright_cli_check),
        _safe_check("nus_skills", skills_check),
        _safe_check("playwright_cli_skill", _playwright_skill_check),
    ]
    if browser_smoke:
        checks.append(_safe_check("browser_smoke", browser_smoke_check))
    serialized = [asdict(check) for check in checks]
    return {
        "schema_version": 1,
        "ok": not any(check.status == "error" for check in checks),
        "checks": serialized,
    }


def print_doctor_report(report: dict[str, Any], *, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return
    for check in report["checks"]:
        marker = {"ok": "OK", "warning": "WARN", "error": "ERROR"}[check["status"]]
        print(f"[{marker}] {check['summary']}")
        if check.get("remediation"):
            print(f"        {check['remediation']}")
