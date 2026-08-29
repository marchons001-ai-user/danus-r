"""Deterministic Supervisor reconciliation over Plan, Progress, and worker status.

This module decides what the control plane should look at next. It deliberately
does not start/kill workers or invent replacement tasks; A2-7 wires these decisions
to the long-running runtime. Semantic node closure stays an explicit Supervisor
progress action, not an automatic consequence of one worker reporting DONE.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import time
from typing import Any, Dict, List, Optional

from danus.core import ProgressLedger, ResearchPlanGraph

_FIVE_HOURS_SECONDS = 5 * 60 * 60
_QUOTA_SNAPSHOT_MAX_AGE_SECONDS = 15 * 60


def _snapshot_age(snapshot: Dict[str, Any], now: float) -> Optional[float]:
    raw = snapshot.get("observed_at")
    if not isinstance(raw, str):
        return None
    try:
        return max(0.0, now - datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def quota_policy(snapshot: Optional[Dict[str, Any]], *, now: Optional[float] = None) -> Dict[str, Any]:
    """Turn quota telemetry into a small fail-open safe-boundary policy.

    No fixed quota size is assumed. When the local 5h proxy is ready, its recent
    weekly-equivalent burn is projected only until the current official weekly
    reset. Pressure means that continuing at that recent pace would exhaust the
    currently reported weekly remainder before reset.
    """
    now = float(time.time() if now is None else now)
    base = {
        "state": "UNKNOWN",
        "reason": "telemetry_unavailable",
        "block_new_codex_rounds": False,
        "weekly_remaining_percent": None,
        "weekly_reset_at": None,
        "five_hour_proxy_status": None,
        "projected_weekly_burn_to_reset": None,
        "snapshot_age_seconds": None,
    }
    if not isinstance(snapshot, dict) or not snapshot.get("available"):
        return base

    age = _snapshot_age(snapshot, now)
    base["snapshot_age_seconds"] = None if age is None else round(age, 1)
    if age is None or age > _QUOTA_SNAPSHOT_MAX_AGE_SECONDS:
        base["reason"] = "telemetry_stale"
        return base

    windows = snapshot.get("windows")
    weekly = windows.get("weekly") if isinstance(windows, dict) else None
    if not isinstance(weekly, dict):
        base["reason"] = "weekly_unavailable"
        return base

    remaining = weekly.get("remaining_percent")
    reset_at = weekly.get("resets_at")
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
        base["reason"] = "weekly_remaining_unavailable"
        return base
    if isinstance(reset_at, bool) or not isinstance(reset_at, int):
        base["reason"] = "weekly_reset_unavailable"
        return base

    remaining = max(0.0, min(100.0, float(remaining)))
    base["weekly_remaining_percent"] = remaining
    base["weekly_reset_at"] = reset_at

    five = windows.get("5h") if isinstance(windows, dict) else None
    five_remaining = five.get("remaining_percent") if isinstance(five, dict) else None
    if snapshot.get("rate_limit_reached_type") or remaining <= 0 or (
        isinstance(five_remaining, (int, float))
        and not isinstance(five_remaining, bool)
        and float(five_remaining) <= 0
    ):
        base.update({
            "state": "EXHAUSTED",
            "reason": "official_limit_reached",
            "block_new_codex_rounds": True,
        })
        return base

    proxy = snapshot.get("five_hour_proxy")
    proxy_status = proxy.get("status") if isinstance(proxy, dict) else None
    base["five_hour_proxy_status"] = proxy_status
    if proxy_status != "ready":
        base["reason"] = "short_window_not_ready"
        return base

    weekly_equivalent = proxy.get("weekly_equivalent_percent")
    if isinstance(weekly_equivalent, bool) or not isinstance(weekly_equivalent, (int, float)):
        base["reason"] = "short_window_rate_unavailable"
        return base
    weekly_equivalent = max(0.0, float(weekly_equivalent))

    remaining_seconds = max(0.0, float(reset_at) - now)
    projected = weekly_equivalent * (remaining_seconds / _FIVE_HOURS_SECONDS)
    base["projected_weekly_burn_to_reset"] = round(projected, 3)
    if projected > remaining:
        base.update({
            "state": "PRESSURE",
            "reason": "recent_pace_exhausts_before_weekly_reset",
            "block_new_codex_rounds": True,
        })
    else:
        base.update({
            "state": "NORMAL",
            "reason": "recent_pace_within_weekly_headroom",
        })
    return base


def reviewer_cost_policy(
    quota: Dict[str, Any],
    *,
    dsh_available: bool,
    dsh_quota_exhausted: bool = False,
) -> Dict[str, Any]:
    """Choose the backend for an explicitly auto-managed Reviewer.

    Reviewer heterogeneity is intentional: DSH/DeepSeek is the primary backend.
    Terra is a fallback only after the current Reviewer assignment receives DSH's
    explicit provider-neutral ``QUOTA`` failure. Missing DSH provisioning, transient
    rate limits, auth failures, and other errors do not silently consume Terra.
    """
    state = str(quota.get("state") or "UNKNOWN").upper()
    codex_blocked = bool(quota.get("block_new_codex_rounds"))
    if dsh_quota_exhausted:
        backend = "chatgpt"
        reason = "dsh_quota_exhausted_fallback_terra"
    else:
        backend = "dsh"
        reason = "dsh_primary" if dsh_available else "dsh_primary_unavailable"
    return {
        "policy": "dsh_first_terra_fallback",
        "quota_state": state,
        "codex_blocked": codex_blocked,
        "dsh_available": bool(dsh_available),
        "dsh_quota_exhausted": bool(dsh_quota_exhausted),
        "selected_backend": backend,
        "reason": reason,
    }


def reconcile(project_dir: Path, worker_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the current ready frontier and deterministic control decisions."""
    root = Path(project_dir)
    graph = ResearchPlanGraph(root)
    plan = graph.snapshot()
    pending_plan_changes = graph.pending_changes()
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
    project_state = (
        "COMPLETE"
        if plan.get("root_id") in completed and not frontier and not pending_plan_changes
        else "RUNNING"
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
        if row.get("paused"):
            pause_reason = str(row.get("pause_reason") or "manual")
            if pause_reason.startswith("review:"):
                add("REVIEW", worker=worker, node_id=node_id, assignment_id=aid,
                    reason=pause_reason.split(":", 1)[1] or "worker_paused_for_review")
            else:
                add("CONTINUE", worker=worker, node_id=node_id, assignment_id=aid,
                    reason="worker_paused")
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
    if project_state == "COMPLETE":
        alive_workers = {
            str(row.get("worker") or "")
            for row in worker_rows
            if row.get("alive")
        }
        decisions = [
            decision
            for decision in decisions
            if decision["action"] == "STOP" and decision.get("worker") in alive_workers
        ]
    return {
        "project_state": project_state,
        "plan_version": plan["plan_version"],
        "plan_changes": pending_plan_changes,
        "frontier": frontier,
        "completed_nodes": sorted(completed),
        "decisions": decisions,
    }