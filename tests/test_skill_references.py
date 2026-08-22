from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILLS_ROOT = PROJECT_ROOT / "agent_for_nus" / "skills"
SKILLS = {
    "nus-canvas": ("commands.md", "low-level.md"),
    "nusmods": ("commands.md",),
    "nus-talent-connect": ("commands.md", "low-level.md"),
}


def test_bundled_skills_have_portable_structure_and_metadata() -> None:
    for name, references in SKILLS.items():
        skill_dir = SKILLS_ROOT / name
        skill_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = skill_text.split("---", 2)[1]
        keys = {line.partition(":")[0] for line in frontmatter.splitlines() if line.strip()}

        assert keys == {"name", "description"}
        assert f"name: {name}" in frontmatter
        assert "TODO" not in skill_text
        assert len(skill_text.splitlines()) < 500

        metadata = (skill_dir / "agents" / "openai.yaml").read_text(encoding="utf-8")
        assert {line.strip().partition(":")[0] for line in metadata.splitlines()} == {
            "interface",
            "display_name",
            "short_description",
            "default_prompt",
        }
        assert f"${name}" in metadata

        for reference in references:
            path = skill_dir / "references" / reference
            assert path.is_file()
            assert f"references/{reference}" in skill_text


def test_generated_skill_command_references_are_current() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/generate_skill_references.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
