"""Deterministic Supervisor reconciliation over Plan, Progress, and worker status.

This module decides what the control plane should look at next. It deliberately
does not start/kill workers or invent replacement tasks; A2-7 wires these decisions
to the long-running runtime. Semantic node closure stays an explicit Supervisor
progress action, not an automatic consequence of one worker reporting DONE.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from danus.core import ProgressLedger, ResearchPlanGraph


def reconcile(project_dir: Path, worker_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the current ready frontier and deterministic control decisions."""
    root = Path(project_dir)
    plan = ResearchPlanGraph(root).snapshot()
    ledger = ProgressLedger(root)
    current_assignments = ledger.snapshot(refresh_stale=True)
    all_assignments = ledger.assignments()
    nodes = plan["nodes"]
    status_by_worker = {str(row.get("worker") or ""): row for row in worker_rows}

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

    active_children = {node_id: [] for node_id in nodes}
    for node_id, node in nodes.items():
        parent = node.get("parent")
        if parent in active_children and active_route(node_id):
            active_children[parent].append(node_id)

    # A recorded DONE remains effective only while its route is active and every
    # active child is also complete. Progressive expansion therefore reopens the
    # parent for dependency purposes without rewriting the old ledger event.
    completed = set(ledger.completed_nodes())
    changed = True
    while changed:
        changed = False
        for node_id in list(completed):
            if (
                not active_route(node_id)
                or any(child not in completed for child in active_children[node_id])
            ):
                completed.remove(node_id)
                changed = True

    def blocked_dependencies(node_id: str) -> List[str]:
        node = nodes.get(node_id) or {}
        return [dep for dep in node.get("depends_on", []) if dep not in completed]

    frontier = sorted(
        node_id
        for node_id in nodes
        if active_route(node_id)
        and node_id not in completed
        and not active_children[node_id]
        and not blocked_dependencies(node_id)
    )

    decisions: List[Dict[str, Any]] = []

    def add(action: str, *, worker: Any, node_id: str, assignment_id: Any, reason: str) -> None:
        decisions.append(
            {
                "action": action,
                "worker": worker,
                "node_id": node_id,
                "assignment_id": assignment_id,
                "reason": reason,
            }
        )

    current_by_node: Dict[str, List[Dict[str, Any]]] = {}
    for assignment in current_assignments:
        node_id = assignment["node_id"]
        current_by_node.setdefault(node_id, []).append(assignment)
        worker = assignment["worker"]
        row = status_by_worker.get(worker, {})
        state = assignment["state"]
        aid = assignment["assignment_id"]

        if state == "STALE":
            add("STOP", worker=worker, node_id=node_id, assignment_id=aid,
                reason="assignment_stale")
            continue
        if not active_route(node_id):
            add("STOP", worker=worker, node_id=node_id, assignment_id=aid,
                reason="plan_route_retired")
            continue
        if blocked_dependencies(node_id):
            add("STOP", worker=worker, node_id=node_id, assignment_id=aid,
                reason="dependencies_not_complete")
            continue
        if state == "BLOCKED":
            reason = (
                "assignment_needs_review"
                if assignment.get("outcome") == "NEEDS_REVIEW"
                else "assignment_blocked"
            )
            add("REVIEW", worker=worker, node_id=node_id, assignment_id=aid,
                reason=reason)
            continue
        if state == "DONE":
            if node_id not in completed:
                add("REVIEW", worker=worker, node_id=node_id, assignment_id=aid,
                    reason="completion_not_recorded")
            elif row.get("alive"):
                add("STOP", worker=worker, node_id=node_id, assignment_id=aid,
                    reason="node_completed")
            else:
                add("CONTINUE", worker=worker, node_id=node_id, assignment_id=aid,
                    reason="node_completed_worker_idle")
            continue
        if row.get("label") == "stuck?":
            add("REVIEW", worker=worker, node_id=node_id, assignment_id=aid,
                reason="worker_stuck")
            continue
        if not row.get("alive", False):
            add("RETASK", worker=worker, node_id=node_id, assignment_id=aid,
                reason="worker_not_running")
            continue
        add("CONTINUE", worker=worker, node_id=node_id, assignment_id=aid,
            reason="work_in_progress")

    latest_by_node: Dict[str, Dict[str, Any]] = {}
    for assignment in all_assignments:
        latest_by_node[assignment["node_id"]] = assignment

    completion_waiting = set()
    for node_id, assignment in latest_by_node.items():
        if (
            assignment["state"] == "DONE"
            and node_id not in completed
            and active_route(node_id)
            and not blocked_dependencies(node_id)
        ):
            completion_waiting.add(node_id)
            if not any(
                d["node_id"] == node_id and d["reason"] == "completion_not_recorded"
                for d in decisions
            ):
                add(
                    "REVIEW",
                    worker=assignment["worker"],
                    node_id=node_id,
                    assignment_id=assignment["assignment_id"],
                    reason="completion_not_recorded",
                )

    for node_id in frontier:
        assignments = current_by_node.get(node_id, [])
        covered = any(a["state"] in {"ASSIGNED", "RUNNING", "BLOCKED"} for a in assignments)
        if not covered and node_id not in completion_waiting:
            add(
                "RETASK",
                worker=None,
                node_id=node_id,
                assignment_id=None,
                reason="ready_node_unassigned",
            )

    order = {"STOP": 0, "REVIEW": 1, "RETASK": 2, "CONTINUE": 3}
    decisions.sort(
        key=lambda d: (
            order.get(d["action"], 9),
            str(d["node_id"]),
            str(d["worker"] or ""),
            str(d["assignment_id"] or ""),
        )
    )
    return {
        "plan_version": plan["plan_version"],
        "frontier": frontier,
        "completed_nodes": sorted(completed),
        "decisions": decisions,
    }
