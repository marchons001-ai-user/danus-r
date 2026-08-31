"""Read-only state aggregation for the Danus-R terminal UI."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.execution import layout as L
from danus.orchestration import cli as C
from danus.orchestration import quota as Q
from danus.orchestration.control import quota_policy


def _pending_change(project_dir: Path, plan_version: int) -> bool:
    changes = project_dir / "plan" / "changes"
    if not changes.is_dir():
        return False
    for path in changes.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(record, dict)
            and record.get("status") in {"PROPOSED", "APPROVED"}
            and record.get("base_plan_version") == plan_version
        ):
            return True
    return False


def _effective_completed(plan: Dict[str, Any], completed: set[str]) -> set[str]:
    nodes = plan["nodes"]

    def active_route(node_id: str) -> bool:
        current = node_id
        seen: set[str] = set()
        while current:
            if current in seen:
                return False
            seen.add(current)
            node = nodes.get(current)
            if node is None or node.get("state") != "ACTIVE":
                return False
            current = node.get("parent")
        return True

    children = {node_id: [] for node_id in nodes}
    for node_id, node in nodes.items():
        parent = node.get("parent")
        if parent in children and active_route(node_id):
            children[parent].append(node_id)

    effective = set(completed)
    changed = True
    while changed:
        changed = False
        for node_id in list(effective):
            if (
                not active_route(node_id)
                or any(child not in effective for child in children[node_id])
            ):
                effective.remove(node_id)
                changed = True
    return effective


def _worker_row(project: str, worker: str, ledger: ProgressLedger) -> Dict[str, Any]:
    wl = L.WorkerLayout(L.worker_dir(project, worker))
    status = C._read_status(wl)
    progress = ledger.current(worker, refresh_stale=False)
    paused = C._pause_reason(wl)
    alive = C._worker_alive(wl)
    last = status.get("last_round_at") or status.get("round_started_at") or status.get("updated_at")
    age = time.time() - last if isinstance(last, (int, float)) else None
    if paused is not None:
        runtime = "paused"
    elif alive:
        runtime = "working"
    else:
        runtime = str(status.get("state") or "stopped")
    return {
        "worker": worker,
        "role": C._read_worker_role(wl),
        "runtime": runtime,
        "node_id": progress.get("node_id") if progress else None,
        "progress_state": progress.get("state") if progress else None,
        "round": int(status.get("round") or 0),
        "age_s": round(age, 1) if age is not None else None,
    }


def load_project_snapshot(project: str) -> Dict[str, Any]:
    """Read one display snapshot without running Supervisor or quota refresh."""
    project_dir = L.project_dir(project)
    plan = ResearchPlanGraph(project_dir).snapshot()
    ledger = ProgressLedger(project_dir)
    completed = _effective_completed(plan, set(ledger.completed_nodes()))
    pending = _pending_change(project_dir, int(plan["plan_version"]))
    if plan["root_id"] in completed and not pending:
        project_state = "COMPLETE"
    elif (project_dir / L.PAUSE_FILE).is_file():
        project_state = "PAUSED"
    else:
        project_state = "RUNNING"

    nodes: List[Dict[str, Any]] = []
    for node_id, node in plan["nodes"].items():
        plan_state = str(node.get("state") or "ACTIVE")
        nodes.append(
            {
                "node_id": node_id,
                "title": str(node.get("title") or node_id),
                "parent": node.get("parent"),
                "depends_on": list(node.get("depends_on") or []),
                "state": "DONE" if node_id in completed else ("OPEN" if plan_state == "ACTIVE" else plan_state),
            }
        )

    workers = [
        _worker_row(project, worker, ledger)
        for worker in L.list_workers(project)
    ]
    quota = quota_policy(Q.load_quota(project_dir))
    return {
        "project": project,
        "project_state": project_state,
        "plan_version": int(plan["plan_version"]),
        "root_id": plan["root_id"],
        "nodes": nodes,
        "workers": workers,
        "quota": quota,
    }
