"""Deterministic auto-dispatch and completion for opt-in Research Plan nodes.

A node opts in by providing one exact versioned task file:
``tasks/v<plan_version>/<role>/<node_id>.md``. The file is execution input, not
scientific Plan state. Older task specs remain reusable only while that Plan node
is byte-for-byte unchanged in the current revision.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.execution import layout as L

_TERMINAL_ASSIGNMENT_STATES = {"DONE", "STALE"}


def _safe_node_filename(node_id: str) -> bool:
    return bool(node_id) and Path(node_id).name == node_id and node_id not in {".", ".."}


def _task_spec(project_dir: Path, node_id: str) -> Dict[str, Any]:
    """Resolve one exact role/task spec valid for the current node revision."""
    if not _safe_node_filename(node_id):
        return {"status": "invalid", "reason": "node_id_not_file_safe"}

    graph = ResearchPlanGraph(project_dir)
    current = graph.snapshot()
    current_node = current["nodes"].get(node_id)
    if current_node is None or current_node.get("state") != "ACTIVE":
        return {"status": "invalid", "reason": "node_not_active"}

    for version in range(int(current["plan_version"]), 0, -1):
        try:
            old_node = graph.revision(version)["nodes"].get(node_id)
        except ValueError:
            continue
        if old_node != current_node:
            continue
        matches = []
        for role in sorted(L.SCIENTIFIC_ROLES):
            path = project_dir / "tasks" / f"v{version}" / role / f"{node_id}.md"
            if path.is_file():
                matches.append((role, path))
        if len(matches) > 1:
            return {"status": "invalid", "reason": "ambiguous_role_task"}
        if not matches:
            continue
        role, path = matches[0]
        try:
            task = path.read_text(encoding="utf-8")
        except OSError:
            return {"status": "invalid", "reason": "task_unreadable"}
        if not task.strip():
            return {"status": "invalid", "reason": "task_empty"}
        if not task.endswith("\n"):
            task += "\n"
        return {
            "status": "ready",
            "role": role,
            "task": task,
            "path": str(path),
            "spec_plan_version": version,
        }
    return {"status": "missing", "reason": "no_autonomous_task"}


def _assignment_matches_current_node(
    graph: ResearchPlanGraph,
    assignment: Dict[str, Any],
) -> bool:
    try:
        old = graph.revision(int(assignment["plan_version"]))
        current = graph.snapshot()
    except (KeyError, TypeError, ValueError):
        return False
    node_id = str(assignment.get("node_id") or "")
    old_node = old["nodes"].get(node_id)
    current_node = current["nodes"].get(node_id)
    return bool(
        old_node is not None
        and current_node is not None
        and current_node.get("state") == "ACTIVE"
        and old_node == current_node
    )


def _auto_complete(
    project_dir: Path,
    decision: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Close one opt-in leaf only after its exact assignment reports COMPLETE."""
    if decision.get("action") != "REVIEW" or decision.get("reason") != "completion_not_recorded":
        return None
    worker = str(decision.get("worker") or "")
    node_id = str(decision.get("node_id") or "")
    assignment_id = str(decision.get("assignment_id") or "")
    if not worker or not node_id or not assignment_id:
        return None

    ledger = ProgressLedger(project_dir)
    current = ledger.current(worker, refresh_stale=True)
    if not current or str(current.get("assignment_id") or "") != assignment_id:
        return {"action": "AUTO_COMPLETE", "node_id": node_id, "worker": worker, "result": "assignment_changed"}
    if current.get("state") != "DONE" or current.get("outcome") != "COMPLETE":
        return None

    spec = _task_spec(project_dir, node_id)
    if spec.get("status") != "ready":
        return None
    if str(current.get("role") or "").lower() != spec["role"]:
        return {"action": "AUTO_COMPLETE", "node_id": node_id, "worker": worker, "result": "role_mismatch"}
    if ProgressLedger.task_sha256(spec["task"]) != current.get("task_sha256"):
        return {"action": "AUTO_COMPLETE", "node_id": node_id, "worker": worker, "result": "task_changed"}
    graph = ResearchPlanGraph(project_dir)
    if not _assignment_matches_current_node(graph, current):
        return {"action": "AUTO_COMPLETE", "node_id": node_id, "worker": worker, "result": "plan_binding_stale"}

    ledger.set_node_state(
        node_id,
        state="DONE",
        actor="supervisor",
        note=f"automatic closure from assignment {assignment_id}",
    )
    return {"action": "AUTO_COMPLETE", "node_id": node_id, "worker": worker, "result": "completed"}


def _close_completed_containers(project_dir: Path) -> List[Dict[str, Any]]:
    """Close non-leaf Plan containers once all active children are complete."""
    graph = ResearchPlanGraph(project_dir)
    plan = graph.snapshot()
    nodes = plan["nodes"]
    ledger = ProgressLedger(project_dir)
    completed = set(ledger.completed_nodes())

    def active_route(node_id: str) -> bool:
        current = node_id
        seen = set()
        while current:
            if current in seen:
                return False
            seen.add(current)
            node = nodes.get(current)
            if node is None or node.get("state") != "ACTIVE":
                return False
            current = node.get("parent")
        return True

    children: Dict[str, List[str]] = {node_id: [] for node_id in nodes}
    for node_id, node in nodes.items():
        parent = node.get("parent")
        if parent in children and active_route(node_id):
            children[parent].append(node_id)

    applied: List[Dict[str, Any]] = []
    changed = True
    while changed:
        changed = False
        for node_id in sorted(nodes):
            if node_id in completed or not active_route(node_id) or not children[node_id]:
                continue
            deps = nodes[node_id].get("depends_on", [])
            if any(dep not in completed for dep in deps):
                continue
            if any(child not in completed for child in children[node_id]):
                continue
            ledger.set_node_state(
                node_id,
                state="DONE",
                actor="supervisor",
                note="automatic container closure: all active children complete",
            )
            completed.add(node_id)
            applied.append({"action": "AUTO_COMPLETE_CONTAINER", "node_id": node_id, "result": "completed"})
            changed = True
    return applied


def _idle_worker(
    project: str,
    role: str,
    rows: List[Dict[str, Any]],
    reserved: set[str],
    role_of: Callable[[L.WorkerLayout], str],
) -> Optional[L.WorkerLayout]:
    ledger = ProgressLedger(L.project_dir(project))
    rows_by_worker = {str(row.get("worker") or ""): row for row in rows}
    for worker in L.list_workers(project):
        if worker in reserved:
            continue
        wl = L.WorkerLayout(L.worker_dir(project, worker))
        if role_of(wl) != role or wl.pause.is_file():
            continue
        row = rows_by_worker.get(worker, {})
        if row.get("alive"):
            continue
        current = ledger.current(worker, refresh_stale=True)
        if current and not (
            current.get("state") in _TERMINAL_ASSIGNMENT_STATES
            or (current.get("state") == "BLOCKED" and current.get("outcome") == "NEEDS_REVIEW")
        ):
            continue
        return wl
    return None


def apply_autonomy(
    project: str,
    decisions: List[Dict[str, Any]],
    *,
    rows: List[Dict[str, Any]],
    assign: Callable[..., Dict[str, Any]],
    start: Callable[[L.WorkerLayout], str],
    role_of: Callable[[L.WorkerLayout], str],
) -> List[Dict[str, Any]]:
    """Apply only explicit opt-in auto-completion and auto-dispatch actions."""
    project_dir = L.project_dir(project)
    if (project_dir / L.PAUSE_FILE).is_file():
        return []

    applied: List[Dict[str, Any]] = []
    for decision in decisions:
        result = _auto_complete(project_dir, decision)
        if result is not None:
            applied.append(result)
    applied.extend(_close_completed_containers(project_dir))

    reserved: set[str] = set()
    for decision in decisions:
        if (
            decision.get("action") != "RETASK"
            or decision.get("worker") is not None
            or decision.get("reason") != "ready_node_unassigned"
        ):
            continue
        node_id = str(decision.get("node_id") or "")
        spec = _task_spec(project_dir, node_id)
        if spec.get("status") != "ready":
            if spec.get("status") == "invalid":
                applied.append({
                    "action": "AUTO_DISPATCH",
                    "node_id": node_id,
                    "result": "blocked",
                    "reason": spec.get("reason"),
                })
            continue
        wl = _idle_worker(project, spec["role"], rows, reserved, role_of)
        if wl is None:
            applied.append({
                "action": "AUTO_DISPATCH",
                "node_id": node_id,
                "role": spec["role"],
                "result": "no_idle_worker",
            })
            continue
        try:
            assignment = assign(f"{project}/{wl.name}", spec["task"], node_id=node_id)
        except SystemExit as exc:
            applied.append({
                "action": "AUTO_DISPATCH",
                "node_id": node_id,
                "role": spec["role"],
                "worker": wl.name,
                "result": "blocked",
                "reason": str(exc),
            })
            continue
        reserved.add(wl.name)
        start_result = start(wl)
        applied.append({
            "action": "AUTO_DISPATCH",
            "node_id": node_id,
            "role": spec["role"],
            "worker": wl.name,
            "assignment_id": assignment.get("assignment_id"),
            "task_spec": spec["path"],
            "task_plan_version": spec["spec_plan_version"],
            "result": "assigned",
            "start_result": start_result,
        })
    return applied
