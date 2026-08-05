from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import tools.playwright_cli as playwright_cli
from tools.playwright_cli import PlaywrightCLIError


def completed(
    arguments: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)


def test_missing_playwright_cli_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(playwright_cli.shutil, "which", lambda _name: None)

    with pytest.raises(PlaywrightCLIError, match="not installed"):
        playwright_cli.playwright_cli_executable()


def test_existing_session_fails_without_opening(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(_executable: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return completed(arguments, stdout=json.dumps({"browsers": [{"name": "canvas"}]}))

    monkeypatch.setattr(playwright_cli, "_run", fake_run)

    with pytest.raises(PlaywrightCLIError, match="already exists"):
        playwright_cli.ensure_session_available("playwright-cli", "canvas")
    assert calls == [["list", "--json"]]


def test_authenticated_session_loads_state_and_stays_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    loaded_state: dict[str, Any] = {}
    state_path: Path | None = None

    monkeypatch.setattr(
        playwright_cli,
        "load_session",
        lambda _site: {
            "storage_state": {
                "cookies": [{"name": "auth", "value": "secret"}],
                "origins": [],
            }
        },
    )

    def fake_run(_executable: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal state_path
        calls.append(arguments)
        if arguments == ["list", "--json"]:
            return completed(arguments, stdout='{"browsers": []}')
        if "state-load" in arguments:
            state_path = Path(arguments[-1])
            assert state_path.exists()
            loaded_state.update(json.loads(state_path.read_text(encoding="utf-8")))
        return completed(arguments)

    monkeypatch.setattr(playwright_cli, "_run", fake_run)

    playwright_cli.open_authenticated_session(
        executable="playwright-cli",
        session_id="canvas-test",
        site_name="nus_canvas",
        url="https://canvas.example.test/course/1",
        headed=True,
    )

    assert loaded_state["cookies"][0]["name"] == "auth"
    assert calls[0] == ["list", "--json"]
    assert calls[1] == [
        "-s=canvas-test",
        "open",
        "https://canvas.example.test/course/1",
        "--headed",
    ]
    assert calls[2][0:2] == ["-s=canvas-test", "state-load"]
    assert calls[3] == [
        "-s=canvas-test",
        "goto",
        "https://canvas.example.test/course/1",
    ]
    assert all("close" not in call for call in calls)
    assert state_path is not None
    assert not state_path.exists()


def test_open_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        playwright_cli,
        "load_session",
        lambda _site: {"storage_state": {"cookies": [], "origins": []}},
    )

    def fake_run(_executable: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if arguments == ["list", "--json"]:
            return completed(arguments, stdout='{"browsers": []}')
        if arguments[-1] == "close":
            return completed(arguments)
        return completed(arguments, returncode=1, stderr="browser launch failed")

    monkeypatch.setattr(playwright_cli, "_run", fake_run)

    with pytest.raises(PlaywrightCLIError, match="browser launch failed"):
        playwright_cli.open_authenticated_session(
            executable="playwright-cli",
            session_id="canvas",
            site_name="nus_canvas",
            url="https://canvas.example.test",
        )
    assert calls[-1] == ["-s=canvas", "close"]


def test_injection_failure_closes_new_session(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        playwright_cli,
        "load_session",
        lambda _site: {"storage_state": {"cookies": [], "origins": []}},
    )

    def fake_run(_executable: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if arguments == ["list", "--json"]:
            return completed(arguments, stdout='{"browsers": []}')
        if "state-load" in arguments:
            return completed(arguments, returncode=1, stderr="invalid state")
        return completed(arguments)

    monkeypatch.setattr(playwright_cli, "_run", fake_run)

    with pytest.raises(PlaywrightCLIError, match="invalid state"):
        playwright_cli.open_authenticated_session(
            executable="playwright-cli",
            session_id="talent-connect",
            site_name="nus_talent_connect",
            url="https://talent.example.test",
        )
    assert calls[-1] == ["-s=talent-connect", "close"]
