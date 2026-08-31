"""Read-only state aggregation for the Danus-R terminal UI."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.evidence import EvidenceStore
from danus.execution import layout as L
from danus.orchestration import cli as C
from danus.orchestration import quota as Q
from danus.orchestration.control import quota_policy


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _pending_change(project_dir: Path, plan_version: int) -> bool:
    changes = project_dir / "plan" / "changes"
    if not changes.is_dir():
        return False
    for path in changes.glob("*.json"):
        record = _read_json(path)
        if (
            record is not None
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
    if paused is not None:
        runtime = "paused"
    elif alive:
        runtime = "working"
    else:
        runtime = str(status.get("state") or "stopped")
    round_started = status.get("round_started_at")
    age = (
        time.time() - round_started
        if alive and status.get("state") == "running" and isinstance(round_started, (int, float))
        else None
    )
    return {
        "worker": worker,
        "role": C._read_worker_role(wl),
        "runtime": runtime,
        "node_id": progress.get("node_id") if progress else None,
        "progress_state": progress.get("state") if progress else None,
        "round": int(status.get("round") or 0),
        "age_s": round(age, 1) if age is not None else None,
    }


def _protocol_total(protocol: Optional[Dict[str, Any]]) -> int:
    matrix = protocol.get("matrix") if protocol else None
    units = matrix.get("units") if isinstance(matrix, dict) else None
    return len(units) if isinstance(units, list) else 0


def _experiment_rows(project_dir: Path) -> List[Dict[str, Any]]:
    runtime_root = project_dir / "experiment_runtime"
    if not runtime_root.is_dir():
        return []
    store = EvidenceStore(project_dir)
    rows: List[Dict[str, Any]] = []
    for runtime_dir in sorted(path for path in runtime_root.iterdir() if path.is_dir()):
        status = _read_json(runtime_dir / "status.json")
        checkpoint = _read_json(runtime_dir / "checkpoint.json")
        if status is None and checkpoint is None:
            continue
        protocol_id = runtime_dir.name
        protocol = store.get_protocol(protocol_id)
        completed_map = checkpoint.get("completed") if checkpoint else None
        if isinstance(completed_map, dict):
            completed = len(completed_map)
        else:
            completed = int(status.get("completed_units") or 0) if status else 0
        total = int(status.get("total_units") or 0) if status else 0
        if total <= 0:
            total = _protocol_total(protocol)
        if total < completed:
            total = completed
        evidence_id = checkpoint.get("evidence_id") if checkpoint else None
        if status is not None:
            state = str(status.get("state") or "unknown")
        elif evidence_id:
            state = "complete"
        else:
            state = "checkpoint"
        percent = round(100.0 * completed / total, 1) if total else 0.0
        question = str(protocol.get("question") or "") if protocol else ""
        rows.append({
            "protocol_id": protocol_id,
            "label": question or protocol_id,
            "state": state,
            "completed_units": completed,
            "total_units": total,
            "percent": percent,
            "current_unit": status.get("current_unit") if status else None,
            "attempt": status.get("attempt") if status else None,
            "evidence_id": status.get("evidence_id") if status else evidence_id,
        })
    return rows


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
        "experiments": _experiment_rows(project_dir),
        "quota": quota,
    }
