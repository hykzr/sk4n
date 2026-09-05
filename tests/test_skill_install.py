from __future__ import annotations

import json
from pathlib import Path

import pytest

from sk4n.skill_install import (
    MANIFEST_NAME,
    NUS_SKILLS,
    bundled_skill_files,
    destination_targets,
    install_skills,
    parse_selection,
    skill_status_report,
    uninstall_skills,
)


def test_selection_and_destination_resolution(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"

    assert parse_selection("all", choices=("a", "b"), label="items") == ("a", "b")
    assert parse_selection("b,a,b", choices=("a", "b"), label="items") == ("b", "a")
    with pytest.raises(ValueError, match="cannot be combined"):
        parse_selection("all,a", choices=("a", "b"), label="items")

    all_user = destination_targets(("codex", "copilot", "claude", "antigravity"), "user", home=home)
    assert [(target.agents, target.root) for target in all_user] == [
        (("codex", "copilot"), home / ".agents" / "skills"),
        (("claude",), home / ".claude" / "skills"),
        (("antigravity",), home / ".gemini" / "config" / "skills"),
    ]
    copilot_user = destination_targets(("copilot",), "user", home=home)
    assert copilot_user[0].root == home / ".copilot" / "skills"
    all_project = destination_targets(
        ("codex", "copilot", "claude", "antigravity"),
        "project",
        home=home,
        project_root=project,
    )
    assert [(target.agents, target.root) for target in all_project] == [
        (
            ("codex", "copilot", "antigravity"),
            project / ".agents" / "skills",
        ),
        (("claude",), project / ".claude" / "skills"),
    ]


def test_install_update_and_uninstall_preserve_unrelated_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    actions = install_skills(
        agents=("codex",),
        skills=("nus-canvas",),
        scope="project",
        project_root=project,
    )
    destination = project / ".agents" / "skills" / "nus-canvas"

    assert actions[0]["action"] == "create"
    assert (destination / "SKILL.md").read_bytes() == bundled_skill_files("nus-canvas")["SKILL.md"]
    manifest = json.loads((destination / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert set(manifest["managed_files"]) == set(bundled_skill_files("nus-canvas"))

    unrelated = destination / "my-notes.txt"
    unrelated.write_text("keep me", encoding="utf-8")
    (destination / "SKILL.md").write_text("tampered", encoding="utf-8")
    updated = install_skills(
        agents=("codex",),
        skills=("nus-canvas",),
        scope="project",
        project_root=project,
    )

    assert updated[0]["action"] == "update"
    assert unrelated.read_text(encoding="utf-8") == "keep me"
    assert (destination / "SKILL.md").read_bytes() == bundled_skill_files("nus-canvas")["SKILL.md"]

    removed = uninstall_skills(
        agents=("codex",),
        skills=("nus-canvas",),
        scope="project",
        project_root=project,
    )

    assert removed[0]["action"] == "remove-managed-files"
    assert unrelated.read_text(encoding="utf-8") == "keep me"
    assert not (destination / "SKILL.md").exists()
    assert not (destination / MANIFEST_NAME).exists()
    assert (project / ".agents" / "skills").is_dir()


def test_dry_run_and_unmanaged_collision(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    dry_run = install_skills(
        agents=("codex",),
        skills=("nusmods",),
        scope="project",
        project_root=project,
        dry_run=True,
    )
    assert dry_run[0]["action"] == "create"
    assert not (project / ".agents").exists()

    destination = project / ".agents" / "skills" / "nusmods"
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text("unmanaged", encoding="utf-8")
    (destination / "notes.txt").write_text("preserve", encoding="utf-8")
    refused = install_skills(
        agents=("codex",),
        skills=("nusmods",),
        scope="project",
        project_root=project,
    )
    assert refused[0]["action"] == "error"
    assert (destination / "SKILL.md").read_text(encoding="utf-8") == "unmanaged"

    forced = install_skills(
        agents=("codex",),
        skills=("nusmods",),
        scope="project",
        project_root=project,
        force=True,
    )
    assert forced[0]["action"] == "take-over"
    assert (destination / "notes.txt").read_text(encoding="utf-8") == "preserve"
    assert (destination / "SKILL.md").read_bytes() == bundled_skill_files("nusmods")["SKILL.md"]


def test_status_reports_current_hashes_and_duplicates(tmp_path: Path) -> None:
    home = tmp_path / "home"
    install_skills(
        agents=("codex", "copilot", "claude"),
        skills=NUS_SKILLS,
        scope="user",
        home=home,
    )
    shared_report = skill_status_report(
        agents=("codex", "copilot", "claude"), scope="user", home=home
    )
    assert shared_report["duplicates"] == {}
    install_skills(
        agents=("copilot",),
        skills=("nusmods",),
        scope="user",
        home=home,
    )

    report = skill_status_report(agents=("codex",), scope="user", home=home)

    assert all(item["state"] == "current" for item in report["installations"])
    assert {item["skill"] for item in report["installations"]} == set(NUS_SKILLS)
    assert set(report["duplicates"]) == {"nusmods"}
    assert len(report["duplicates"]["nusmods"]) == 2


def test_project_root_is_discovered_from_nested_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    nested = project / "a" / "b"
    nested.mkdir(parents=True)
    (project / ".git").mkdir()
    monkeypatch.chdir(nested)

    actions = install_skills(
        agents=("codex",),
        skills=("nusmods",),
        scope="project",
    )

    assert actions[0]["destination"] == str(project / ".agents" / "skills" / "nusmods")
