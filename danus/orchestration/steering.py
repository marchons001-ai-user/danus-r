"""Minimal project-level human steering for scientific Danus-R runs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from danus.core import ProgressLedger
from danus.core.handoff import HandoffRequests
from danus.execution import layout as L
from danus.execution.scaffold import atomic_write

from .cli import _do_resume_unlocked as _legacy_resume
from .cli import do_start, do_status, do_stop, project_control_lock

_GUIDANCE_FILE = "HUMAN_GUIDANCE.md"
_ACTIVE_PROGRESS = frozenset({"ASSIGNED", "RUNNING"})


def guidance_path(project: str) -> Path:
    return L.project_dir(project) / _GUIDANCE_FILE


def _write_guidance(project: str, message: str) -> Path:
    path = guidance_path(project)
    atomic_write(path, f"# Current Human Guidance\n\n{message}\n")
    return path


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

    interrupted: List[Dict[str, str]] = []
    if not interrupt:
        path = _write_guidance(project, message)
    else:
        # Share the Supervisor/planning mutation lock so no automatic dispatch can
        # replace TASK.md in the kill/restart window. Assignment provenance itself
        # stays unchanged.
        with project_control_lock(pdir):
            path = _write_guidance(project, message)
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


def _resume_worker(
    project: str, worker: str, *,
    budget_limit: Optional[int] = None, accept_incomplete_usage: bool = False,
) -> List[Dict[str, str]]:
    pdir = L.project_dir(project)
    ledger_path = pdir / "progress" / "ledger.jsonl"
    row = do_status(f"{project}/{worker}")[0]
    ledger = ProgressLedger(pdir) if ledger_path.is_file() else None
    current = ledger.current(worker, refresh_stale=True) if ledger else None

    # Do not let the older BLOCKED/NEEDS_REVIEW recovery branch implicitly
    # clear an unrelated system-owned budget pause.
    if (current and current.get("state") == "BLOCKED"
            and str(row.get("pause_reason") or "").startswith("budget:")):
        return [{"worker": worker, "result": "system-paused"}]

    if current and current.get("state") == "BLOCKED":
        outcome = str(current.get("outcome") or "").upper()
        if outcome == "NEEDS_REVIEW":
            source_id = str(current.get("assignment_id") or "")
            handoffs = HandoffRequests(ledger)
            routes = [
                item
                for item in handoffs.all()
                if item.get("kind") in {"INTERPRET", "RETHINK", "ESCALATE"}
                and item.get("subject_type") == "ASSIGNMENT"
                and item.get("subject_id") == source_id
            ]
            active = [item for item in routes if item.get("status") in {"OPEN", "ASSIGNED"}]
            if any(item.get("kind") != "ESCALATE" for item in active):
                return [{"worker": worker, "result": "needs-review"}]
            guidance = guidance_path(project)
            if not routes or not guidance.is_file() or not guidance.read_text(encoding="utf-8").strip():
                return [{"worker": worker, "result": "needs-review"}]
            for handoff in active:
                handoffs.cancel(
                    handoff["handoff_id"],
                    actor="operator",
                    reason="resolved by explicit human guidance",
                )
            ledger.update(
                worker=worker,
                state="RUNNING",
                actor="operator",
                note="explicit human guidance resolved scientific route",
            )
        elif outcome == "BLOCKED":
            ledger.update(
                worker=worker,
                state="RUNNING",
                actor="operator",
                note="external prerequisite restored by explicit worker resume",
            )
        else:
            return [{"worker": worker, "result": "blocked"}]
    else:
        pause = str(row.get("pause_reason") or "")
        if pause.startswith("budget:"):
            # Deliberate operator-only budget recovery. A larger Supervisor
            # budget does NOT make missing Reviewer usage magically known.
            # Keep the default system pause unless the operator supplies the
            # exact new cumulative limit and acknowledges that uncertainty.
            if budget_limit is None or current is None or current.get("state") not in _ACTIVE_PROGRESS:
                return [{"worker": worker, "result": "system-paused"}]
            from . import cli as CLI

            rows = CLI._project_rows(project)
            budget = CLI._resource_snapshot(pdir, rows, budget_limit)
            tokens = budget["tokens"]
            if budget["time"]["exceeded"] or tokens["exceeded"] is True:
                return [{"worker": worker, "result": "budget-exceeded"}]
            if tokens["exceeded"] is None and not accept_incomplete_usage:
                return [{"worker": worker, "result": "budget-usage-incomplete"}]
            if tokens["used"] >= budget_limit:
                return [{"worker": worker, "result": "budget-exceeded"}]
            if not (pdir / "workers" / worker / L.PAUSE_FILE).is_file():
                return [{"worker": worker, "result": "budget-pause-changed"}]

            # Persist operator authorization *before* changing .pause or
            # launching the existing Assignment, under project_control_lock.
            record = {
                "at": datetime.now(timezone.utc).isoformat(),
                "action": "AUTHORIZE_BUDGET_RESUME",
                "actor": "operator", "worker": worker,
                "assignment_id": current["assignment_id"],
                "pause_reason": pause, "new_cumulative_budget": budget_limit,
                "known_usage_lower_bound": tokens["used"],
                "unknown_workers": tokens.get("unknown_workers", []),
                "accepted_incomplete_usage": bool(accept_incomplete_usage),
            }
            with (pdir / "operator_budget_resumes.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            return _legacy_resume(f"{project}/{worker}", stagger=0)
        if pause.startswith(("quota:", "review:")):
            return [{"worker": worker, "result": "system-paused"}]

    return _legacy_resume(f"{project}/{worker}", stagger=0)


def do_resume(
    target: str, *,
    budget_limit: Optional[int] = None, accept_incomplete_usage: bool = False,
) -> List[Dict[str, str]]:
    """Resume work. Budget-locked workers require explicit bounded operator authorization."""
    project, worker = L.resolve_target(target)
    if budget_limit is not None:
        if budget_limit <= 0 or not worker:
            raise ValueError("budget-limit must be positive and target exactly one worker")
    if accept_incomplete_usage and budget_limit is None:
        raise ValueError("accept-incomplete-usage requires --budget-limit")
    with project_control_lock(L.project_dir(project)):
        return _do_resume_unlocked(
            target, budget_limit=budget_limit,
            accept_incomplete_usage=accept_incomplete_usage,
        )


def _do_resume_unlocked(
    target: str, *,
    budget_limit: Optional[int] = None, accept_incomplete_usage: bool = False,
) -> List[Dict[str, str]]:
    project, worker = L.resolve_target(target)
    if worker:
        return _resume_worker(
            project, worker, budget_limit=budget_limit,
            accept_incomplete_usage=accept_incomplete_usage,
        )

    pdir = L.project_dir(project)
    if not pdir.is_dir():
        raise SystemExit(f"no such project: {project}")
    ledger = pdir / "progress" / "ledger.jsonl"
    if not ledger.is_file():
        return _legacy_resume(target)

    (pdir / L.PAUSE_FILE).unlink(missing_ok=True)
    out: List[Dict[str, str]] = []
    for row in do_status(project):
        worker_name = str(row["worker"])
        if row.get("progress_state") in _ACTIVE_PROGRESS:
            result = do_start(f"{project}/{worker_name}", stagger=0)[0]["result"]
        else:
            result = "paused" if row.get("paused") else "idle"
        out.append({"worker": worker_name, "result": str(result)})
    return out


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


def resume_main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="danus resume",
        description="Resume worker/project execution without starting unassigned planned workers.",
    )
    parser.add_argument("target", help="<project> or <project>/<worker>")
    parser.add_argument("--budget-limit", type=int, default=None,
                        help="operator-authorized new cumulative token ceiling for one budget-paused worker")
    parser.add_argument("--accept-incomplete-usage", action="store_true",
                        help="acknowledge that some workers have incomplete token telemetry")
    args = parser.parse_args(argv)
    try:
        results = do_resume(
            args.target, budget_limit=args.budget_limit,
            accept_incomplete_usage=args.accept_incomplete_usage,
        )
    except ValueError as exc:
        parser.error(str(exc))
    for row in results:
        print(f"{row['worker']}: {row['result']}")
    return 0
