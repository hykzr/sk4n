from __future__ import annotations

import json
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONSOLE_SCRIPTS = ("agent-for-nus", "canvas", "nusmods", "talent-connect")
WHEEL_PACKAGES = ("agent_for_nus", "canvas_sync", "nusmods", "talent_connect", "tools")
WHEEL_SKILLS = {
    "nus-canvas": ("SKILL.md", "agents/openai.yaml", "references/commands.md", "references/low-level.md"),
    "nusmods": ("SKILL.md", "agents/openai.yaml", "references/commands.md"),
    "nus-talent-connect": (
        "SKILL.md",
        "agents/openai.yaml",
        "references/commands.md",
        "references/low-level.md",
    ),
}


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True, capture_output=True, text=True)


def assert_console_scripts(bin_dir: Path, *, cwd: Path, env: dict[str, str]) -> None:
    for script in CONSOLE_SCRIPTS:
        run([str(bin_dir / script), "--help"], cwd=cwd, env=env)
    assert not (bin_dir / "canvas-sync").exists()


def tool_environment(tmp_path: Path, name: str) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / name / "bin"
    env = os.environ.copy()
    env["UV_TOOL_DIR"] = str(tmp_path / name / "tools")
    env["UV_TOOL_BIN_DIR"] = str(bin_dir)
    env["AGENT_FOR_NUS_HOME"] = str(tmp_path / "user-data")
    return env, bin_dir


def test_wheel_and_source_tool_installs_run_outside_checkout(tmp_path: Path) -> None:
    outside = tmp_path / "unrelated-working-directory"
    outside.mkdir()
    wheel_dir = tmp_path / "dist"
    run(
        ["uv", "build", "--wheel", "--sdist", "--out-dir", str(wheel_dir)],
        cwd=PROJECT_ROOT,
    )
    wheel = next(wheel_dir.glob("agent_for_nus-*.whl"))
    source_archive = next(wheel_dir.glob("agent_for_nus-*.tar.gz"))

    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
    for package in WHEEL_PACKAGES:
        assert any(member.startswith(f"{package}/") for member in members)
    for skill, files in WHEEL_SKILLS.items():
        for filename in files:
            assert f"agent_for_nus/skills/{skill}/{filename}" in members
    assert not any(member.startswith(("data/", "sessions/")) for member in members)

    git_extract_dir = tmp_path / "git-source"
    with tarfile.open(source_archive) as archive:
        archive.extractall(git_extract_dir)
    git_source = next(git_extract_dir.iterdir())
    run(["git", "init", "--quiet"], cwd=git_source)
    run(["git", "add", "."], cwd=git_source)
    run(
        [
            "git",
            "-c",
            "user.name=Packaging Test",
            "-c",
            "user.email=packaging@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "packaging test",
        ],
        cwd=git_source,
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    installs = (
        ("wheel", [str(wheel)]),
        ("snapshot", [str(PROJECT_ROOT)]),
        ("editable", ["--editable", str(PROJECT_ROOT)]),
        ("git", [f"agent-for-nus @ git+file://{git_source}@{commit}"]),
    )
    for name, source_arguments in installs:
        env, bin_dir = tool_environment(tmp_path, name)
        run(["uv", "tool", "install", *source_arguments], cwd=outside, env=env)
        assert_console_scripts(bin_dir, cwd=outside, env=env)

    wheel_env, wheel_bin = tool_environment(tmp_path, "wheel")
    skill_project = tmp_path / "codex-skill-project"
    skill_project.mkdir()
    (skill_project / ".git").mkdir()
    run(
        [
            str(wheel_bin / "agent-for-nus"),
            "skills",
            "install",
            "--agents",
            "codex",
            "--scope",
            "project",
            "--project-root",
            str(skill_project),
        ],
        cwd=outside,
        env=wheel_env,
    )
    status = subprocess.run(
        [
            str(wheel_bin / "agent-for-nus"),
            "skills",
            "status",
            "--agents",
            "codex",
            "--scope",
            "project",
            "--project-root",
            str(skill_project),
            "--format",
            "json",
        ],
        cwd=outside,
        env=wheel_env,
        check=True,
        capture_output=True,
        text=True,
    )
    installations = json.loads(status.stdout)["installations"]
    assert len(installations) == len(WHEEL_SKILLS)
    assert all(item["state"] == "current" for item in installations)
    for skill in WHEEL_SKILLS:
        assert (skill_project / ".agents" / "skills" / skill / "SKILL.md").is_file()
    run(
        [
            str(wheel_bin / "agent-for-nus"),
            "skills",
            "uninstall",
            "--agents",
            "codex",
            "--scope",
            "project",
            "--project-root",
            str(skill_project),
        ],
        cwd=outside,
        env=wheel_env,
    )
    assert (skill_project / ".agents" / "skills").is_dir()
    assert not any((skill_project / ".agents" / "skills" / skill).exists() for skill in WHEEL_SKILLS)

    sentinel = tmp_path / "user-data" / "keep-after-reinstall"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("persistent", encoding="utf-8")
    run(
        ["uv", "tool", "install", "--force", str(wheel)],
        cwd=outside,
        env=wheel_env,
    )
    assert_console_scripts(wheel_bin, cwd=outside, env=wheel_env)
    assert sentinel.read_text(encoding="utf-8") == "persistent"
