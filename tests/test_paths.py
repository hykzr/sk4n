from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import canvas_sync.cli as canvas_cli
import nusmods.cli as nusmods_cli
import talent_connect.cli as talent_connect_cli
from agent_for_nus import paths
from tools import shared


def test_application_home_controls_all_default_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "state"
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(home))
    monkeypatch.delenv(paths.SESSION_DIR_ENV_VAR, raising=False)

    assert paths.home_dir() == home
    assert paths.sessions_dir() == home / "sessions"
    assert paths.canvas_data_dir() == home / "canvas"
    assert paths.nusmods_data_dir() == home / "nusmods"
    assert paths.talent_connect_data_dir() == home / "talent-connect"
    assert paths.talent_connect_database_path() == (
        home / "talent-connect" / "talent_connect.sqlite3"
    )


def test_service_cli_flags_override_home_and_legacy_environments_are_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    explicit = tmp_path / "explicit"
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(home))
    monkeypatch.setenv("CANVAS_DATA_PATH", str(tmp_path / "legacy-canvas"))
    monkeypatch.setenv("NUSMODS_DATA_PATH", str(tmp_path / "legacy-nusmods"))
    monkeypatch.setenv("TALENT_CONNECT_DATA_PATH", str(tmp_path / "legacy-talent"))

    canvas_parser = canvas_cli.build_parser()
    nusmods_parser = nusmods_cli.build_parser()
    talent_parser = talent_connect_cli.build_parser()

    assert canvas_parser.get_default("data_path") == home / "canvas"
    assert nusmods_parser.get_default("data_path") == home / "nusmods"
    assert talent_parser.get_default("data_path") == (
        home / "talent-connect" / "talent_connect.sqlite3"
    )
    assert (
        canvas_parser.parse_args(["--data-path", str(explicit), "auth", "status"]).data_path
        == explicit
    )
    assert (
        nusmods_parser.parse_args(["--data-path", str(explicit), "search", "CS"]).data_path
        == explicit
    )
    assert (
        talent_parser.parse_args(["--data-path", str(explicit), "auth", "status"]).data_path
        == explicit
    )


def test_session_override_is_private_and_independent_of_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    session_directory = tmp_path / "recovery-sessions"
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(home))
    monkeypatch.setenv(paths.SESSION_DIR_ENV_VAR, str(session_directory))

    message = shared.save_session("nus_canvas", {"cookies": [{"name": "secret"}]})
    session_file = session_directory / "nus_canvas.json"

    assert str(session_file) in message
    assert json.loads(session_file.read_text(encoding="utf-8"))["cookies"][0]["name"] == ("secret")
    assert not (home / "sessions").exists()
    assert list(session_directory.glob("*.tmp")) == []
    if os.name == "posix":
        assert stat.S_IMODE(session_directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(session_file.stat().st_mode) == 0o600


def test_failed_session_write_preserves_previous_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(paths.SESSION_DIR_ENV_VAR, str(tmp_path / "sessions"))
    shared.save_session("nus_canvas", {"value": "original"})

    def fail_dump(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated interrupted write")

    monkeypatch.setattr(shared.json, "dump", fail_dump)
    with pytest.raises(OSError, match="interrupted"):
        shared.save_session("nus_canvas", {"value": "replacement"})

    session_file = tmp_path / "sessions" / "nus_canvas.json"
    assert json.loads(session_file.read_text(encoding="utf-8")) == {"value": "original"}
    assert list(session_file.parent.glob("*.tmp")) == []


@pytest.mark.parametrize("site_name", ["../escape", "nested/name", "", ".."])
def test_session_name_cannot_escape_session_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, site_name: str
) -> None:
    monkeypatch.setenv(paths.SESSION_DIR_ENV_VAR, str(tmp_path / "sessions"))
    with pytest.raises(ValueError, match="file-safe"):
        shared.session_path(site_name)
