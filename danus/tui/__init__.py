"""Danus-R terminal UI entry point."""

from __future__ import annotations

import argparse
from typing import List

from danus.execution import layout as L


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the Danus-R terminal UI")
    parser.add_argument("project", nargs="?")
    args = parser.parse_args(argv)

    try:
        from .app import DanusTUI
        from .selector import ProjectSelector
    except ModuleNotFoundError as exc:
        if exc.name != "textual":
            raise
        parser.error("Textual is not installed; install Danus with: pip install -e '.[tui]'")

    project = args.project
    if project is None:
        project = ProjectSelector().run()
        if not project:
            return 0

    if not (L.project_dir(project) / "workers").is_dir():
        parser.error(f"unknown project: {project}")

    try:
        DanusTUI(project).run()
    except (FileNotFoundError, OSError, ValueError, KeyError) as exc:
        parser.error(f"cannot open project {project}: {exc}")
    return 0
