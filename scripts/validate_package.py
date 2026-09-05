#!/usr/bin/env python3
"""Validate built release artifacts outside the unit-test suite."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONSOLE_SCRIPTS = ("sk4n", "canvas", "nusmods", "talent-connect")
FORBIDDEN_TOP_LEVEL_PACKAGES = ("canvas", "nusmods", "talent_connect", "tools")
WHEEL_SKILLS = {
    "nus-canvas": (
        "SKILL.md",
        "agents/openai.yaml",
        "references/commands.md",
        "references/low-level.md",
    ),
    "nusmods": ("SKILL.md", "agents/openai.yaml", "references/commands.md"),
    "nus-talent-connect": (
        "SKILL.md",
        "agents/openai.yaml",
        "references/commands.md",
        "references/low-level.md",
    ),
}


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode:
        rendered_command = " ".join(command)
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: {rendered_command}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def select_artifacts(dist_dir: Path) -> tuple[Path, Path]:
    wheels = sorted(dist_dir.glob("sk4n-*.whl"))
    source_archives = sorted(dist_dir.glob("sk4n-*.tar.gz"))
    if len(wheels) != 1 or len(source_archives) != 1:
        raise RuntimeError(
            f"Expected one sk4n wheel and one source archive in {dist_dir}; "
            f"found {len(wheels)} wheel(s) and {len(source_archives)} source archive(s)."
        )
    return wheels[0], source_archives[0]


def validate_wheel_members(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
        if not any(member.startswith("sk4n/") for member in members):
            raise RuntimeError("Wheel does not contain the sk4n package.")
        forbidden_prefixes = tuple(f"{package}/" for package in FORBIDDEN_TOP_LEVEL_PACKAGES)
        if any(member.startswith(forbidden_prefixes) for member in members):
            raise RuntimeError("Wheel contains an unexpected top-level import package.")
        if any(member.startswith(("data/", "sessions/")) for member in members):
            raise RuntimeError("Wheel contains local data or session state.")

        for skill, filenames in WHEEL_SKILLS.items():
            for filename in filenames:
                member = f"sk4n/skills/{skill}/{filename}"
                if member not in members:
                    raise RuntimeError(f"Wheel is missing bundled skill file: {member}")
                if filename == "references/commands.md" and b"\x1b" in archive.read(member):
                    raise RuntimeError(f"Generated reference contains ANSI escapes: {member}")


def validate_install(artifact: Path, *, name: str, root: Path, outside: Path) -> None:
    venv_dir = root / name
    run([sys.executable, "-m", "venv", str(venv_dir)], cwd=PROJECT_ROOT)
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / ("python.exe" if os.name == "nt" else "python")
    env = os.environ.copy()
    env["SK4N_HOME"] = str(root / "user-data")

    run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", str(artifact)],
        cwd=outside,
        env=env,
    )
    run(
        [
            str(python),
            "-c",
            (
                "import importlib.util; import sk4n; "
                "forbidden=('canvas','nusmods','talent_connect','tools'); "
                "assert all(importlib.util.find_spec(name) is None for name in forbidden)"
            ),
        ],
        cwd=outside,
        env=env,
    )
    for script in CONSOLE_SCRIPTS:
        executable = bin_dir / (f"{script}.exe" if os.name == "nt" else script)
        run([str(executable), "--help"], cwd=outside, env=env)


def validate_package(dist_dir: Path) -> None:
    wheel, source_archive = select_artifacts(dist_dir)
    validate_wheel_members(wheel)

    with tempfile.TemporaryDirectory(prefix="sk4n-package-check-") as temporary_directory:
        root = Path(temporary_directory)
        outside = root / "outside-checkout"
        outside.mkdir()
        validate_install(wheel, name="wheel", root=root, outside=outside)
        validate_install(source_archive, name="sdist", root=root, outside=outside)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "dist_dir",
        nargs="?",
        default=PROJECT_ROOT / "dist",
        type=Path,
        help="Directory containing exactly one sk4n wheel and source archive.",
    )
    args = parser.parse_args()
    validate_package(args.dist_dir.resolve())
    print("Package artifact validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
