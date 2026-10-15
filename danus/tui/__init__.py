"""Danus-R terminal UI entry point."""

from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path
from typing import List

from danus.execution import layout as L
from danus.orchestration import quota as Q

_QUOTA_REFRESH_SECONDS = 5 * 60
_QUOTA_POLL_SECONDS = 60


def _quota_refresh_due(project_dir: Path) -> bool:
    try:
        age = time.time() - Q.quota_path(project_dir).stat().st_mtime
    except OSError:
        return True
    return age >= _QUOTA_REFRESH_SECONDS


def _quota_refresh_loop(project_dir: Path, stop: threading.Event) -> None:
    while not stop.wait(_QUOTA_POLL_SECONDS):
        if _quota_refresh_due(project_dir):
            Q.refresh_quota(project_dir, timeout=30.0)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the Danus-R terminal UI")
    parser.add_argument("project", nargs="?")
    parser.add_argument("--v8", action="store_true", help="monitor a V8 adaptive project")
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
        if args.v8:
            parser.error("--v8 requires an explicit project name")
        project = ProjectSelector().run()
        if not project:
            return 0

    from danus.v8 import project_root
    v8_dir = project_root(project)
    v7_dir = L.project_dir(project)
    v8_mode = args.v8 or (
        (v8_dir / "EVENTS.jsonl").is_file() and not (v7_dir / "workers").is_dir()
    )
    if v8_mode:
        if not (v8_dir / "EVENTS.jsonl").is_file():
            parser.error(f"unknown V8 project: {project}")
        try:
            DanusTUI(project, v8=True).run()
        except (FileNotFoundError, OSError, ValueError, KeyError) as exc:
            parser.error(f"cannot open V8 project {project}: {exc}")
        return 0

    project_dir = v7_dir
    if not (project_dir / "workers").is_dir():
        parser.error(f"unknown project: {project}")

    Q.refresh_quota(project_dir, timeout=30.0)
    stop = threading.Event()
    threading.Thread(
        target=_quota_refresh_loop,
        args=(project_dir, stop),
        daemon=True,
    ).start()
    try:
        DanusTUI(project).run()
    except (FileNotFoundError, OSError, ValueError, KeyError) as exc:
        parser.error(f"cannot open project {project}: {exc}")
    finally:
        stop.set()
    return 0
