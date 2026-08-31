"""Danus-R terminal UI entry point."""

from __future__ import annotations

import argparse
from typing import List

from danus.execution import layout as L


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the Danus-R terminal UI")
    parser.add_argument("project")
    args = parser.parse_args(argv)

    if not (L.project_dir(args.project) / "workers").is_dir():
        parser.error(f"unknown project: {args.project}")

    try:
        from .app import DanusTUI
    except ModuleNotFoundError as exc:
        if exc.name != "textual":
            raise
        parser.error("Textual is not installed; install Danus with: pip install -e '.[tui]'")

    DanusTUI(args.project).run()
    return 0
