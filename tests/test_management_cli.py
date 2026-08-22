from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_for_nus.cli as management_cli
import agent_for_nus.doctor as doctor
import talent_connect.cli as talent_cli
from agent_for_nus.errors import ExitCode, exit_code_for_error
from canvas_sync.client import CanvasAuthError, CanvasHTTPError, CanvasTransportError
from talent_connect.client import KinobiAuthError, KinobiHTTPError, KinobiTransportError


def test_management_parser_exposes_phase_one_commands() -> None:
    parser = management_cli.build_parser()

    assert parser.parse_args(["doctor", "--format", "json"]).format == "json"
    assert parser.parse_args(["doctor", "--browser-smoke"]).browser_smoke is True
    assert parser.parse_args(["paths", "--format", "json"]).command == "paths"
    calendar = parser.parse_args(
        [
            "calendar",
            "--date",
            "2026-08-14",
            "--academic-year",
            "2026/2027",
            "--no-refresh",
            "--format",
            "json",
        ]
    )
    assert calendar.date.isoformat() == "2026-08-14"
    assert calendar.academic_year == "2026/2027"
    assert calendar.refresh_mode == "none"
    browser = parser.parse_args(["browser", "install", "chromium"])
    assert (browser.browser_command, browser.browser) == ("install", "chromium")
    skills = parser.parse_args(
        [
            "skills",
            "install",
            "--agents",
            "codex,copilot",
            "--skills",
            "nusmods",
            "--scope",
            "project",
            "--dry-run",
        ]
    )
    assert skills.agents == ("codex", "copilot")
    assert skills.skills == ("nusmods",)
    assert skills.scope == "project"
    assert skills.dry_run is True


def test_paths_json_is_machine_readable(capsys: pytest.CaptureFixture[str]) -> None:
    assert management_cli.main(["paths", "--format", "json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "home",
        "sessions",
        "canvas",
        "nusmods",
        "talent_connect",
        "talent_connect_database",
    }
    assert all(Path(value).is_absolute() for value in payload.values())


def test_shared_calendar_command_uses_nusmods_data(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeCalendarClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def get_academic_calendar(self) -> dict[str, object]:
            return {"2026/2027": {"1": {"start": [2026, 8, 10]}}}

        def get_holidays(self) -> list[str]:
            return []

    monkeypatch.setattr(management_cli, "NUSModsClient", FakeCalendarClient)

    assert (
        management_cli.main(
            ["calendar", "--date", "2026-08-14", "--academic-year", "2026/2027", "--format", "json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["week"] == 1
    assert payload["source"] == "NUSMods academic calendar"


def test_shared_calendar_reports_date_outside_requested_academic_year(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeCalendarClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def get_academic_calendar(self) -> dict[str, object]:
            return {}

        def get_holidays(self) -> list[str]:
            return []

    monkeypatch.setattr(management_cli, "NUSModsClient", FakeCalendarClient)

    assert (
        management_cli.main(
            ["calendar", "--date", "2025-08-14", "--academic-year", "2026/2027"]
        )
        == 2
    )
    assert "outside AY2026/2027" in capsys.readouterr().err


def test_doctor_json_and_exit_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = {
        "schema_version": 1,
        "ok": False,
        "checks": [
            {
                "id": "example",
                "status": "error",
                "summary": "example failed",
                "details": {},
                "remediation": "fix it",
            }
        ],
    }
    monkeypatch.setattr(management_cli, "build_doctor_report", lambda **_kwargs: report)

    assert management_cli.main(["doctor", "--format", "json"]) == 1
    assert json.loads(capsys.readouterr().out) == report


def test_browser_install_uses_current_python(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        management_cli.subprocess,
        "call",
        lambda command: calls.append(command) or 0,
    )

    assert management_cli.main(["browser", "install", "chromium"]) == 0
    assert calls == [[management_cli.sys.executable, "-m", "playwright", "install", "chromium"]]


def test_python_playwright_check_uses_non_starting_cli_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_location = tmp_path / "chromium-1234"
    executable = (
        install_location
        / "chrome-mac-arm64"
        / "Google Chrome for Testing.app"
        / "Contents"
        / "MacOS"
        / "Google Chrome for Testing"
    )
    executable.parent.mkdir(parents=True)
    executable.write_text("browser", encoding="utf-8")
    executable.chmod(0o700)
    calls: list[list[str]] = []
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(doctor, "version", lambda _name: "1.62.0")

    def fake_run(command: list[str], *, timeout: float = 15) -> subprocess.CompletedProcess[str]:
        del timeout
        calls.append(command)
        output = (
            "Chrome for Testing 151 (playwright chromium v1234)\n"
            f"  Install location:    {install_location}\n"
        )
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(doctor, "_run", fake_run)

    check = doctor.python_playwright_check()

    assert check.status == "ok"
    assert calls == [
        [doctor.sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"]
    ]
    assert check.details["chromium_executable"] == str(executable)


def test_python_playwright_check_reports_missing_expected_browser(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_location = tmp_path / "chromium-1234"
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(doctor, "version", lambda _name: "1.62.0")
    monkeypatch.setattr(
        doctor,
        "_run",
        lambda command: subprocess.CompletedProcess(
            command,
            0,
            (
                "Chrome for Testing 151 (playwright chromium v1234)\n"
                f"  Install location:    {install_location}\n"
            ),
            "",
        ),
    )

    check = doctor.python_playwright_check()

    assert check.status == "error"
    assert check.details["chromium_install_location"] == str(install_location)


def test_browser_smoke_always_closes_session(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/bin/playwright-cli")

    def fake_run(command: list[str], *, timeout: float = 15) -> subprocess.CompletedProcess[str]:
        del timeout
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 1 if command[-1] == "snapshot" else 0, "", "boom"
        )

    monkeypatch.setattr(doctor, "_run", fake_run)

    check = doctor.browser_smoke_check()

    assert check.status == "error"
    assert calls[-1][-1] == "close"


def test_doctor_skill_check_requires_each_bundled_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doctor,
        "skill_status_report",
        lambda **_kwargs: {
            "known_installations": [
                {"skill": "nus-canvas", "state": "current"},
                {"skill": "nusmods", "state": "current"},
            ],
            "duplicates": {},
        },
    )

    check = doctor.skills_check()

    assert check.status == "warning"
    assert "nus-talent-connect" in check.summary


def test_talent_connect_auth_status_json_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        talent_cli,
        "check_auth_status",
        lambda **_kwargs: SimpleNamespace(
            authenticated=True,
            display_name="Student",
            email="student@example.test",
            user_id="user-1",
            error="",
        ),
    )
    args = talent_cli.build_parser().parse_args(["auth", "status", "--format", "json"])

    assert talent_cli.handle_auth(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "authenticated": True,
        "name": "Student",
        "email": "student@example.test",
        "user_id": "user-1",
        "error": None,
    }


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ValueError("bad input"), ExitCode.VALIDATION),
        (CanvasAuthError("auth"), ExitCode.AUTH),
        (KinobiAuthError("auth"), ExitCode.AUTH),
        (CanvasTransportError("network"), ExitCode.TRANSPORT),
        (KinobiTransportError("network"), ExitCode.TRANSPORT),
        (OSError("missing executable"), ExitCode.TRANSPORT),
        (CanvasHTTPError("remote"), ExitCode.REMOTE),
        (KinobiHTTPError("remote"), ExitCode.REMOTE),
    ],
)
def test_public_exit_code_contract(error: BaseException, expected: ExitCode) -> None:
    assert exit_code_for_error(error) == expected
