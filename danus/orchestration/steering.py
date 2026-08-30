"""Minimal project-level human steering for scientific Danus-R runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from danus.execution import layout as L
from danus.execution.scaffold import atomic_write

from .cli import do_start, do_status, do_stop

_GUIDANCE_FILE = "HUMAN_GUIDANCE.md"


def guidance_path(project: str) -> Path:
    return L.project_dir(project) / _GUIDANCE_FILE


def do_guide(project: str, text: str, *, interrupt: bool = False) -> Dict[str, Any]:
    """Persist current human guidance; optionally restart active rounds immediately."""
    pdir = L.project_dir(project)
    if not pdir.is_dir():
        raise SystemExit(f"no such project: {project}")
    if not L.research_charter(project).is_file():
        raise SystemExit(f"project {project} is not a scientific research project")

    message = text.strip()
    if not message:
        raise SystemExit("refusing empty guidance")

    path = guidance_path(project)
    atomic_write(path, f"# Current Human Guidance\n\n{message}\n")

    interrupted: List[Dict[str, str]] = []
    if interrupt:
        # Only workers that were live at command time are restarted. Their current
        # assignment remains untouched; Supervisor/Progress provenance is unchanged.
        for row in do_status(project):
            if not row.get("alive"):
                continue
            worker = str(row["worker"])
            target = f"{project}/{worker}"
            stopped = do_stop(target, force=True)[0]["result"]
            started = do_start(target, stagger=0)[0]["result"]
            interrupted.append({
                "worker": worker,
                "stop_result": str(stopped),
                "start_result": str(started),
            })

    return {
        "project": project,
        "guidance_file": str(path),
        "mode": "interrupt" if interrupt else "guide",
        "interrupted": interrupted,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="danus guide",
        description="Persist current human guidance for a scientific project.",
    )
    parser.add_argument("project")
    parser.add_argument("text")
    parser.add_argument(
        "--interrupt",
        action="store_true",
        help="restart currently active rounds immediately so they read the new guidance",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    result = do_guide(args.project, args.text, interrupt=args.interrupt)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
