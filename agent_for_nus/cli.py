from __future__ import annotations

import argparse
from collections.abc import Sequence
from importlib.metadata import version


def package_version() -> str:
    return version("agent-for-nus")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-for-nus",
        description="Manage the local Agent for NUS command-line tools.",
    )
    parser.add_argument(
        "--version", "-v", action="version", version=f"%(prog)s {package_version()}"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0
