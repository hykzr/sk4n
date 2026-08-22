from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Any

NUS_SKILLS = ("nus-canvas", "nusmods", "nus-talent-connect")
AGENTS = ("codex", "copilot", "claude")
SCOPES = ("user", "project")
MANIFEST_NAME = ".agent-for-nus-managed.json"
MANIFEST_MANAGER = "agent-for-nus"
MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SkillTarget:
    scope: str
    agents: tuple[str, ...]
    root: Path


def package_version() -> str:
    try:
        return version("agent-for-nus")
    except PackageNotFoundError:
        return "unknown"


def parse_selection(value: str, *, choices: Sequence[str], label: str) -> tuple[str, ...]:
    selected = tuple(
        dict.fromkeys(part.strip().lower() for part in value.split(",") if part.strip())
    )
    if not selected:
        raise ValueError(f"{label} must select at least one value.")
    unknown = sorted(set(selected) - {*choices, "all"})
    if unknown:
        raise ValueError(
            f"Unknown {label}: {', '.join(unknown)}. Choose from {', '.join((*choices, 'all'))}."
        )
    if "all" in selected and len(selected) > 1:
        raise ValueError(f"{label} value `all` cannot be combined with other values.")
    return tuple(choices) if selected == ("all",) else selected


def find_project_root(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).expanduser().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def resolve_project_root(project_root: Path | None, *, required: bool) -> Path | None:
    if project_root is not None:
        resolved = project_root.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"Project root is not a directory: {resolved}")
        return resolved
    discovered = find_project_root()
    if required and discovered is None:
        raise ValueError("Project scope requires a Git root or an explicit --project-root path.")
    return discovered


def _scope_targets(scope: str, home: Path, project_root: Path | None) -> dict[str, Path]:
    if scope == "user":
        return {
            "shared": home / ".agents" / "skills",
            "copilot": home / ".copilot" / "skills",
            "claude": home / ".claude" / "skills",
        }
    if project_root is None:
        raise ValueError("Project scope requires a project root.")
    return {
        "shared": project_root / ".agents" / "skills",
        "copilot": project_root / ".github" / "skills",
        "claude": project_root / ".claude" / "skills",
    }


def destination_targets(
    agents: Sequence[str],
    scope: str,
    *,
    home: Path | None = None,
    project_root: Path | None = None,
) -> tuple[SkillTarget, ...]:
    selected = set(agents)
    if not selected or not selected <= set(AGENTS):
        raise ValueError(f"Agents must be selected from {', '.join(AGENTS)}.")
    if scope not in SCOPES:
        raise ValueError(f"Scope must be one of {', '.join(SCOPES)}.")
    roots = _scope_targets(scope, (home or Path.home()).expanduser().resolve(), project_root)
    targets: list[SkillTarget] = []
    if "codex" in selected:
        shared_agents = tuple(agent for agent in ("codex", "copilot") if agent in selected)
        targets.append(SkillTarget(scope, shared_agents, roots["shared"]))
    elif "copilot" in selected:
        targets.append(SkillTarget(scope, ("copilot",), roots["copilot"]))
    if "claude" in selected:
        targets.append(SkillTarget(scope, ("claude",), roots["claude"]))
    return tuple(targets)


def known_skill_roots(
    *, home: Path | None = None, project_root: Path | None = None
) -> tuple[SkillTarget, ...]:
    resolved_home = (home or Path.home()).expanduser().resolve()
    targets = [
        SkillTarget("user", ("codex", "copilot"), resolved_home / ".agents" / "skills"),
        SkillTarget("user", ("copilot",), resolved_home / ".copilot" / "skills"),
        SkillTarget("user", ("claude",), resolved_home / ".claude" / "skills"),
    ]
    if project_root is not None:
        targets.extend(
            [
                SkillTarget("project", ("codex", "copilot"), project_root / ".agents" / "skills"),
                SkillTarget("project", ("copilot",), project_root / ".github" / "skills"),
                SkillTarget("project", ("claude",), project_root / ".claude" / "skills"),
            ]
        )
    return tuple(targets)


def _walk_resource(node: Any, prefix: PurePosixPath | None = None) -> Iterable[tuple[str, bytes]]:
    prefix = prefix or PurePosixPath()
    for child in sorted(node.iterdir(), key=lambda item: item.name):
        if child.name in {"__pycache__", ".DS_Store"}:
            continue
        relative = prefix / child.name
        if child.is_dir():
            yield from _walk_resource(child, relative)
        elif child.is_file():
            yield relative.as_posix(), child.read_bytes()


def bundled_skill_files(skill: str) -> dict[str, bytes]:
    if skill not in NUS_SKILLS:
        raise ValueError(f"Unknown bundled skill: {skill}")
    root = resources.files("agent_for_nus").joinpath("skills").joinpath(skill)
    if not root.is_dir():
        raise FileNotFoundError(f"Bundled skill resource is missing: {skill}")
    files = dict(_walk_resource(root))
    if "SKILL.md" not in files:
        raise FileNotFoundError(f"Bundled skill has no SKILL.md: {skill}")
    return files


def _content_hash(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative, content in sorted(files.items()):
        encoded = relative.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_relative(value: str) -> PurePosixPath | None:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path


def _manifest(skill: str, files: Mapping[str, bytes]) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "manager": MANIFEST_MANAGER,
        "package_version": package_version(),
        "skill": skill,
        "source_sha256": _content_hash(files),
        "managed_files": {
            relative: _file_hash(content) for relative, content in sorted(files.items())
        },
    }


def _read_manifest(destination: Path, skill: str) -> dict[str, Any] | None:
    try:
        payload = json.loads((destination / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or payload.get("manager") != MANIFEST_MANAGER
        or payload.get("skill") != skill
        or not isinstance(payload.get("managed_files"), dict)
    ):
        return None
    if any(_safe_relative(str(relative)) is None for relative in payload["managed_files"]):
        return None
    return payload


def installation_status(
    destination: Path, skill: str, files: Mapping[str, bytes]
) -> dict[str, Any]:
    source_hash = _content_hash(files)
    base = {
        "skill": skill,
        "path": str(destination),
        "source": f"package:agent_for_nus/skills/{skill}",
        "source_sha256": source_hash,
    }
    if destination.is_symlink() or not destination.is_dir():
        return {
            **base,
            "state": "unmanaged" if destination.exists() else "missing",
            "managed": False,
            "matches_bundled": False,
        }
    manifest = _read_manifest(destination, skill)
    if manifest is None:
        return {**base, "state": "unmanaged", "managed": False, "matches_bundled": False}

    expected_hashes = {relative: _file_hash(content) for relative, content in files.items()}
    installed_hashes: dict[str, str | None] = {}
    for relative in manifest["managed_files"]:
        safe = _safe_relative(str(relative))
        current = destination
        valid_file = safe is not None
        if safe is not None:
            for part in safe.parts:
                current = current / part
                if current.is_symlink():
                    valid_file = False
                    break
        installed_hashes[str(relative)] = (
            _file_hash(current.read_bytes()) if valid_file and current.is_file() else None
        )
    matches = (
        manifest.get("source_sha256") == source_hash
        and manifest["managed_files"] == expected_hashes
        and installed_hashes == expected_hashes
    )
    return {
        **base,
        "state": "current" if matches else "stale",
        "managed": True,
        "matches_bundled": matches,
        "installed_package_version": manifest.get("package_version"),
        "installed_sha256": manifest.get("source_sha256"),
    }


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _ensure_parent(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            _remove_path(current)
        current.mkdir(exist_ok=True)
    return root.joinpath(*relative.parts)


def _prune_empty_parents(path: Path, root: Path) -> None:
    current = path.parent
    while current != root and current.is_relative_to(root):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _remove_relative(root: Path, relative: PurePosixPath) -> None:
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            current.unlink()
            _prune_empty_parents(current, root)
            return
        if not current.is_dir():
            return
    path = root.joinpath(*relative.parts)
    if path.exists() or path.is_symlink():
        _remove_path(path)
        _prune_empty_parents(path, root)


def _prepare_directory(
    temporary: Path,
    destination: Path,
    files: Mapping[str, bytes],
    manifest: Mapping[str, Any],
    previous_manifest: Mapping[str, Any] | None,
) -> None:
    if destination.is_dir() and not destination.is_symlink():
        shutil.copytree(destination, temporary, symlinks=True)
    else:
        temporary.mkdir()
    if previous_manifest is not None:
        for relative in previous_manifest["managed_files"]:
            safe = _safe_relative(str(relative))
            if safe is None:
                continue
            _remove_relative(temporary, safe)
    manifest_path = temporary / MANIFEST_NAME
    if manifest_path.exists() or manifest_path.is_symlink():
        _remove_path(manifest_path)
    for relative, content in sorted(files.items()):
        safe = _safe_relative(relative)
        if safe is None:
            raise ValueError(f"Unsafe bundled skill path: {relative}")
        path = _ensure_parent(temporary, safe)
        if path.exists() or path.is_symlink():
            _remove_path(path)
        path.write_bytes(content)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _swap_directory(temporary: Path, destination: Path) -> None:
    backup = destination.parent / f".{destination.name}.backup-{uuid.uuid4().hex}"
    replaced = destination.exists() or destination.is_symlink()
    try:
        if replaced:
            os.replace(destination, backup)
        os.replace(temporary, destination)
    except BaseException:
        if replaced and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    if backup.exists() or backup.is_symlink():
        _remove_path(backup)


def _install_one(
    target: SkillTarget,
    skill: str,
    files: Mapping[str, bytes],
    *,
    dry_run: bool,
    force: bool,
) -> dict[str, Any]:
    destination = target.root / skill
    status = installation_status(destination, skill, files)
    source = status["source"]
    common = {
        "operation": "install",
        "scope": target.scope,
        "agents": list(target.agents),
        "skill": skill,
        "source": source,
        "destination": str(destination),
        "dry_run": dry_run,
    }
    if status["state"] == "current":
        return {**common, "action": "unchanged", "changed": False}
    if status["state"] == "unmanaged" and not force:
        return {
            **common,
            "action": "error",
            "changed": False,
            "error": "Destination exists but is not managed by agent-for-nus; rerun with --force to take it over.",
        }
    action = {
        "missing": "create",
        "stale": "update",
        "unmanaged": "take-over",
    }[status["state"]]
    if dry_run:
        return {**common, "action": action, "changed": False}

    target.root.mkdir(parents=True, exist_ok=True)
    temporary = target.root / f".{skill}.tmp-{uuid.uuid4().hex}"
    previous_manifest = _read_manifest(destination, skill)
    try:
        _prepare_directory(
            temporary, destination, files, _manifest(skill, files), previous_manifest
        )
        _swap_directory(temporary, destination)
    except Exception as exc:
        if temporary.exists():
            shutil.rmtree(temporary)
        return {**common, "action": "error", "changed": False, "error": str(exc)}
    return {**common, "action": action, "changed": True}


def install_skills(
    *,
    agents: Sequence[str],
    skills: Sequence[str],
    scope: str,
    project_root: Path | None = None,
    home: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> list[dict[str, Any]]:
    resolved_project = resolve_project_root(project_root, required=scope == "project")
    targets = destination_targets(agents, scope, home=home, project_root=resolved_project)
    bundles = {skill: bundled_skill_files(skill) for skill in skills}
    return [
        _install_one(target, skill, bundles[skill], dry_run=dry_run, force=force)
        for target in targets
        for skill in skills
    ]


def _directory_has_entries(path: Path) -> bool:
    return path.is_dir() and next(path.iterdir(), None) is not None


def _uninstall_one(target: SkillTarget, skill: str, *, dry_run: bool) -> dict[str, Any]:
    destination = target.root / skill
    common = {
        "operation": "uninstall",
        "scope": target.scope,
        "agents": list(target.agents),
        "skill": skill,
        "destination": str(destination),
        "dry_run": dry_run,
    }
    if not destination.exists():
        return {**common, "action": "missing", "changed": False}
    manifest = _read_manifest(destination, skill)
    if manifest is None:
        return {
            **common,
            "action": "error",
            "changed": False,
            "error": "Destination is not managed by agent-for-nus and was not removed.",
        }
    if dry_run:
        return {**common, "action": "remove-managed-files", "changed": False}

    temporary = target.root / f".{skill}.tmp-{uuid.uuid4().hex}"
    try:
        shutil.copytree(destination, temporary, symlinks=True)
        for relative in manifest["managed_files"]:
            safe = _safe_relative(str(relative))
            if safe is None:
                continue
            _remove_relative(temporary, safe)
        manifest_path = temporary / MANIFEST_NAME
        if manifest_path.exists() or manifest_path.is_symlink():
            _remove_path(manifest_path)
        if _directory_has_entries(temporary):
            _swap_directory(temporary, destination)
        else:
            shutil.rmtree(temporary)
            backup = target.root / f".{skill}.backup-{uuid.uuid4().hex}"
            os.replace(destination, backup)
            _remove_path(backup)
    except Exception as exc:
        if temporary.exists():
            shutil.rmtree(temporary)
        return {**common, "action": "error", "changed": False, "error": str(exc)}
    return {**common, "action": "remove-managed-files", "changed": True}


def uninstall_skills(
    *,
    agents: Sequence[str],
    skills: Sequence[str],
    scope: str,
    project_root: Path | None = None,
    home: Path | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    resolved_project = resolve_project_root(project_root, required=scope == "project")
    targets = destination_targets(agents, scope, home=home, project_root=resolved_project)
    return [
        _uninstall_one(target, skill, dry_run=dry_run) for target in targets for skill in skills
    ]


def skill_status_report(
    *,
    agents: Sequence[str],
    scope: str,
    project_root: Path | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    if scope not in (*SCOPES, "all"):
        raise ValueError("Status scope must be user, project, or all.")
    resolved_project = resolve_project_root(project_root, required=scope == "project")
    requested_scopes = SCOPES if scope == "all" else (scope,)
    targets: list[SkillTarget] = []
    for requested_scope in requested_scopes:
        if requested_scope == "project" and resolved_project is None:
            continue
        targets.extend(
            destination_targets(
                agents,
                requested_scope,
                home=home,
                project_root=resolved_project,
            )
        )
    bundles = {skill: bundled_skill_files(skill) for skill in NUS_SKILLS}
    installations: list[dict[str, Any]] = []
    for target in targets:
        for skill, files in bundles.items():
            record = installation_status(target.root / skill, skill, files)
            record.update({"scope": target.scope, "agents": list(target.agents)})
            installations.append(record)

    inventory: dict[str, list[dict[str, Any]]] = {skill: [] for skill in NUS_SKILLS}
    known_installations: list[dict[str, Any]] = []
    for target in known_skill_roots(home=home, project_root=resolved_project):
        for skill in NUS_SKILLS:
            path = target.root / skill
            if path.exists():
                record = installation_status(path, skill, bundles[skill])
                record.update({"scope": target.scope, "agents": list(target.agents)})
                known_installations.append(record)
                inventory[skill].append(record)
    duplicates: dict[str, list[dict[str, Any]]] = {}
    for skill, copies in inventory.items():
        overlapping = [
            copy
            for index, copy in enumerate(copies)
            if any(
                set(copy["agents"]) & set(other["agents"])
                for other_index, other in enumerate(copies)
                if other_index != index
            )
        ]
        if overlapping:
            duplicates[skill] = overlapping
    return {
        "schema_version": 1,
        "package_version": package_version(),
        "project_root": str(resolved_project) if resolved_project else None,
        "installations": installations,
        "known_installations": known_installations,
        "duplicates": duplicates,
    }


def print_actions(actions: Sequence[Mapping[str, Any]]) -> None:
    for action in actions:
        prefix = "DRY-RUN " if action["dry_run"] else ""
        agents = ",".join(action["agents"])
        source = f" {action['source']} ->" if action.get("source") else ""
        print(
            f"{prefix}{str(action['action']).upper()} "
            f"[{action['scope']}:{agents}] {action['skill']}:{source} {action['destination']}"
        )
        if action.get("error"):
            print(f"  {action['error']}")


def print_status_report(report: Mapping[str, Any], *, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return
    for installation in report["installations"]:
        agents = ",".join(installation["agents"])
        print(
            f"{str(installation['state']).upper()} "
            f"[{installation['scope']}:{agents}] {installation['skill']}: {installation['path']}"
        )
    for skill, copies in report["duplicates"].items():
        paths = ", ".join(copy["path"] for copy in copies)
        print(f"DUPLICATE {skill}: {paths}")
