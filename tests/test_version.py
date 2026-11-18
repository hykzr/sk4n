from __future__ import annotations

import argparse
from importlib import import_module
from importlib.metadata import PackageNotFoundError

import pytest

import sk4n.version as version

CLI_MODULES = (
    ("sk4n", "sk4n.cli"),
    ("canvas", "sk4n.canvas.cli"),
    ("nusmods", "sk4n.nusmods.cli"),
    ("talent-connect", "sk4n.talent_connect.cli"),
    ("nus-outlook", "sk4n.outlook.cli"),
)


def test_package_version_reads_sk4n_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_version(distribution: str) -> str:
        assert distribution == "sk4n"
        return "1.2.3"

    monkeypatch.setattr(version, "version", fake_version)

    assert version.package_version() == "1.2.3"


def test_package_version_handles_missing_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_version(_distribution: str) -> str:
        raise PackageNotFoundError("sk4n")

    monkeypatch.setattr(version, "version", missing_version)

    assert version.package_version() == "unknown"


@pytest.mark.parametrize(("program", "module_name"), CLI_MODULES)
@pytest.mark.parametrize("flag", ["-v", "--version"])
@pytest.mark.parametrize(
    "python_executable",
    [
        "/Applications/Python installations/" + "long directory/" * 8 + "bin/python3",
        r"C:\Program Files\Python installations\Python314\python.exe",
    ],
)
def test_version_flags_print_two_unwrapped_lines_and_exit(
    program: str,
    module_name: str,
    flag: str,
    python_executable: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = import_module(module_name).build_parser()
    monkeypatch.setattr(version, "package_version", lambda: "1.2.3")
    monkeypatch.setattr(version.sys, "executable", python_executable)

    with pytest.raises(SystemExit) as exc:
        parser.parse_args([flag])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == f"{program} 1.2.3\nPython: {python_executable}\n"
    assert captured.err == ""


@pytest.mark.parametrize(("program", "module_name"), CLI_MODULES)
@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_flags_exit_successfully(
    program: str,
    module_name: str,
    flag: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = import_module(module_name).build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args([flag])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert f"usage: {program} " in captured.out
    assert "--version" in captured.out
    assert captured.err == ""


def test_version_argument_does_not_add_a_namespace_default() -> None:
    parser = argparse.ArgumentParser(prog="example")
    version.add_version_argument(parser)

    assert vars(parser.parse_args([])) == {}
