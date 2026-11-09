from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from typing import Any


def package_version() -> str:
    try:
        return version("sk4n")
    except PackageNotFoundError:
        return "unknown"


class _VersionAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        # Print directly so argparse does not wrap paths containing spaces.
        print(f"{parser.prog} {package_version()}\nPython: {sys.executable}")
        parser.exit()


def add_version_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--version",
        "-v",
        action=_VersionAction,
        nargs=0,
        default=argparse.SUPPRESS,
        help="Show the package version and Python executable, then exit.",
    )
