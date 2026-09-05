"""Deterministic Supervisor reconciliation over durable project state.

S3 makes the control boundary explicit: ``build_supervisor_snapshot`` reads the
current durable/runtime state once, ``reduce_supervisor_snapshot`` decides what the
control plane should do, and execution remains elsewhere. The reducer performs no
I/O and does not start/kill workers or invent scientific work.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import time
from typing import Any, Dict, List, Optional

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.execution import layout as L

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


def _all_plan_changes(graph: ResearchPlanGraph) -> List[Dict[str, Any]]:
    if not graph.changes_dir.is_dir():
        return []
    return [graph.get_change(path.stem) for path in sorted(graph.changes_dir.glob("*.json"))]


def build_supervisor_snapshot(project_dir: Path, worker_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Read the state needed by the deterministic Supervisor reducer once."""
    root = Path(project_dir)
    graph = ResearchPlanGraph(root)
    ledger = ProgressLedger(root)
    current_assignments = ledger.snapshot(refresh_stale=True)
    pending_plan_changes = graph.pending_changes()
    return {
        "plan": graph.snapshot(),
        "plan_changes": pending_plan_changes,
        "all_plan_changes": _all_plan_changes(graph),
        "current_assignments": current_assignments,
        "assignments": ledger.assignments(),
        "completed_nodes": ledger.completed_nodes(),
        "implementation_contracts": ledger.implementation_contracts(),
        "pending_implementation_contracts": ledger.pending_implementation_contracts(),
        "handoffs": HandoffRequests(ledger).all(),
        "workers": [dict(row) for row in worker_rows],
        "project_paused": (root / L.PAUSE_FILE).is_file(),
    }


def _protocol_action(decision: Dict[str, Any], plan_version: int) -> Dict[str, Any]:
    """Project the legacy decision into the S3 action protocol without changing behavior."""
    legacy = str(decision.get("action") or "")
    if legacy == "STOP":
        kind = "STOP_WORKER"
    elif legacy == "REVIEW":
        kind = "REVIEW_ASSIGNMENT"
    elif legacy == "RETASK":
        kind = "START_WORKER" if decision.get("worker") else "ASSIGN_NODE"
    else:
        kind = "WAIT"
    return {
        "kind": kind,
        "plan_version": int(plan_version),
        "node_id": str(decision.get("node_id") or ""),
        "assignment_id": decision.get("assignment_id"),
        "worker": decision.get("worker"),
        "reason": str(decision.get("reason") or ""),
    }


def _proposal_id_from_change(change: Dict[str, Any]) -> str:
    reason = str(change.get("reason") or "")
    prefix = "[proposal:"
    if not reason.startswith(prefix):
        return ""
    end = reason.find("]")
    return reason[len(prefix):end] if end > len(prefix) else ""


def _latest_handoff(
    handoffs: List[Dict[str, Any]],
    *,
    kind: str,
    subject_type: str,
    subject_id: str,
) -> Optional[Dict[str, Any]]:
    matches = [
        item
        for item in handoffs
        if item.get("kind") == kind
        and item.get("subject_type") == subject_type
        and item.get("subject_id") == subject_id
    ]
    matches.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("handoff_id") or "")))
    return matches[-1] if matches else None


def _contract_status(contract: Dict[str, Any]) -> str:
    return str(contract.get("gate_status") or contract.get("status") or "").upper()


def reduce_supervisor_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce one captured snapshot into deterministic control actions."""
    plan = snapshot["plan"]
    pending_plan_changes = snapshot["plan_changes"]
    all_plan_changes = snapshot.get("all_plan_changes", pending_plan_changes)
    current_assignments = snapshot["current_assignments"]
    all_assignments = snapshot["assignments"]
    all_implementation_contracts = snapshot["implementation_contracts"]
    pending_implementation_contracts = snapshot.get(
        "pending_implementation_contracts", all_implementation_contracts
    )
    handoffs = snapshot["handoffs"]
    worker_rows = snapshot["workers"]
    pending_handoffs = [
        {
            "handoff_id": item["handoff_id"],
            "kind": item["kind"],
            "status": item["status"],
            "node_id": item["node_id"],
            "target_role": item["target_role"],
        }
        for item in handoffs
        if item.get("status") in {"OPEN", "ASSIGNED"}
    ]
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

    completed = set(snapshot["completed_nodes"])
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
    unresolved_implementation_contracts = [
        {
            "contract_id": str(item.get("contract_id") or ""),
            "node_id": str(item.get("node_id") or ""),
            "status": _contract_status(item),
        }
        for item in pending_implementation_contracts
        if active_route(str(item.get("node_id") or ""))
        and _contract_status(item) != "APPROVE"
    ]
    complete = bool(
        plan.get("root_id") in completed
        and not frontier
        and not pending_plan_changes
        and not unresolved_implementation_contracts
        and not pending_handoffs
    )
    project_state = (
        "COMPLETE" if complete else "PAUSED" if snapshot.get("project_paused") else "RUNNING"
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
    current_by_worker: Dict[str, Dict[str, Any]] = {}
    for assignment in current_assignments:
        node_id = assignment["node_id"]
        current_by_node.setdefault(node_id, []).append(assignment)
        current_by_worker[assignment["worker"]] = assignment
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
        current = current_by_worker.get(assignment["worker"])
        if current and current["assignment_id"] != assignment["assignment_id"]:
            continue
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
    if project_state == "PAUSED":
        decisions = []
    elif project_state == "COMPLETE":
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

    plan_version = int(plan["plan_version"])
    routing: List[Dict[str, Any]] = []

    def route(kind: str, *, reason: str, **fields: Any) -> None:
        action = {
            "kind": kind,
            "plan_version": plan_version,
            "node_id": str(fields.pop("node_id", "") or ""),
            "assignment_id": fields.pop("assignment_id", None),
            "worker": fields.pop("worker", None),
            "reason": reason,
        }
        action.update(fields)
        routing.append(action)

    if project_state == "RUNNING":
        assignments_by_id = {
            str(item.get("assignment_id") or ""): item
            for item in all_assignments
            if item.get("assignment_id")
        }
        contracts_by_id = {
            str(item.get("contract_id") or ""): item
            for item in all_implementation_contracts
            if item.get("contract_id")
        }
        changes_by_id = {
            str(item.get("change_id") or ""): item
            for item in all_plan_changes
            if item.get("change_id")
        }
        replacements: Dict[str, List[Dict[str, Any]]] = {}
        for contract in all_implementation_contracts:
            parent = str(contract.get("replaces_contract_id") or "")
            if parent:
                replacements.setdefault(parent, []).append(contract)

        def handoff_assignment(handoff: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            if not handoff:
                return None
            aid = str(
                handoff.get("assigned_assignment_id")
                or handoff.get("resolution_assignment_id")
                or ""
            )
            return assignments_by_id.get(aid)

        def assignment_complete(assignment: Optional[Dict[str, Any]]) -> bool:
            return bool(
                assignment
                and assignment.get("state") == "DONE"
                and assignment.get("outcome") == "COMPLETE"
            )

        resolve_ids = set()
        for handoff in sorted(
            (item for item in handoffs if item.get("status") in {"OPEN", "ASSIGNED"}),
            key=lambda item: (str(item.get("created_at") or ""), str(item.get("handoff_id") or "")),
        ):
            hid = str(handoff["handoff_id"])
            target = str(handoff.get("target_role") or "")
            assignment = handoff_assignment(handoff)
            if handoff.get("status") == "OPEN":
                if target == "main":
                    route(
                        "WAIT",
                        node_id=handoff.get("node_id"),
                        reason="escalation_pending_main",
                        handoff_id=hid,
                        handoff_kind=handoff.get("kind"),
                        subject_type=handoff.get("subject_type"),
                        subject_id=handoff.get("subject_id"),
                        target_role=target,
                    )
                else:
                    route(
                        "ASSIGN_HANDOFF",
                        node_id=handoff.get("node_id"),
                        reason="open_handoff_ready",
                        handoff_id=hid,
                        handoff_kind=handoff.get("kind"),
                        subject_type=handoff.get("subject_type"),
                        subject_id=handoff.get("subject_id"),
                        target_role=target,
                    )
                continue
            if assignment and assignment.get("state") == "STALE":
                route(
                    "REOPEN_HANDOFF",
                    node_id=handoff.get("node_id"),
                    assignment_id=assignment.get("assignment_id"),
                    worker=assignment.get("worker"),
                    reason="handoff_assignment_stale",
                    handoff_id=hid,
                    handoff_kind=handoff.get("kind"),
                    subject_type=handoff.get("subject_type"),
                    subject_id=handoff.get("subject_id"),
                    target_role=target,
                )
                if target != "main":
                    route(
                        "ASSIGN_HANDOFF",
                        node_id=handoff.get("node_id"),
                        reason="reopened_handoff_ready",
                        handoff_id=hid,
                        handoff_kind=handoff.get("kind"),
                        subject_type=handoff.get("subject_type"),
                        subject_id=handoff.get("subject_id"),
                        target_role=target,
                    )
                continue
            if not assignment_complete(assignment):
                continue

            subject_type = str(handoff.get("subject_type") or "")
            subject_id = str(handoff.get("subject_id") or "")
            kind = str(handoff.get("kind") or "")
            summary = ""
            extra: Dict[str, Any] = {}
            if kind == "INTERPRET":
                summary = "interpretation request completed; source disposition remains explicit"
            elif kind == "RETHINK" and subject_type == "ASSIGNMENT":
                summary = "assignment rethink completed; source disposition remains explicit"
            elif kind == "REVIEW" and subject_type == "ASSIGNMENT":
                summary = "generic assignment review completed; source disposition remains explicit"
            elif kind == "REVIEW" and subject_type == "IMPLEMENTATION_CONTRACT":
                contract = contracts_by_id.get(subject_id)
                status = _contract_status(contract or {})
                if status in ProgressLedger.IMPLEMENTATION_VERDICTS:
                    summary = f"implementation review recorded {status}"
            elif kind in {"REVISION", "RETHINK"} and subject_type == "IMPLEMENTATION_CONTRACT":
                candidates = replacements.get(subject_id, [])
                if candidates:
                    replacement = candidates[-1]
                    summary = f"replacement implementation contract {replacement['contract_id']} captured"
                    extra["replacement_contract_id"] = replacement["contract_id"]
            elif kind == "REVIEW" and subject_type == "PLAN_CHANGE":
                change = changes_by_id.get(subject_id)
                verdict = str((change or {}).get("review_verdict") or "").upper()
                if verdict in ResearchPlanGraph.REVIEW_VERDICTS:
                    summary = f"plan review recorded {verdict}"
            if summary:
                route(
                    "RESOLVE_HANDOFF",
                    node_id=handoff.get("node_id"),
                    assignment_id=assignment.get("assignment_id"),
                    worker=assignment.get("worker"),
                    reason="handoff_work_completed",
                    handoff_id=hid,
                    handoff_kind=kind,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    target_role=target,
                    summary=summary,
                    **extra,
                )
                resolve_ids.add(hid)
                continue

            required_effect = (
                kind == "REVIEW"
                and subject_type in {"IMPLEMENTATION_CONTRACT", "PLAN_CHANGE"}
            ) or (
                kind in {"REVISION", "RETHINK"}
                and subject_type in {"IMPLEMENTATION_CONTRACT", "PLAN_CHANGE"}
            )
            if not required_effect:
                continue
            if int(handoff.get("attempt") or 0) < 2:
                route(
                    "REOPEN_HANDOFF",
                    node_id=handoff.get("node_id"),
                    assignment_id=assignment.get("assignment_id"),
                    worker=assignment.get("worker"),
                    reason="required_side_effect_missing",
                    handoff_id=hid,
                    handoff_kind=kind,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    target_role=target,
                )
                if target != "main":
                    route(
                        "ASSIGN_HANDOFF",
                        node_id=handoff.get("node_id"),
                        reason="governance_retry_ready",
                        handoff_id=hid,
                        handoff_kind=kind,
                        subject_type=subject_type,
                        subject_id=subject_id,
                        target_role=target,
                    )
            else:
                route(
                    "WAIT",
                    node_id=handoff.get("node_id"),
                    assignment_id=assignment.get("assignment_id"),
                    worker=assignment.get("worker"),
                    reason="required_side_effect_missing_after_retry",
                    handoff_id=hid,
                    handoff_kind=kind,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    target_role=target,
                )

        def review_ready(review: Optional[Dict[str, Any]]) -> bool:
            return bool(
                review
                and (
                    review.get("status") == "RESOLVED"
                    or str(review.get("handoff_id") or "") in resolve_ids
                )
                and assignment_complete(handoff_assignment(review))
            )

        for contract in pending_implementation_contracts:
            status = _contract_status(contract)
            if status == "STALE":
                continue
            contract_id = str(contract["contract_id"])
            review = _latest_handoff(
                handoffs,
                kind="REVIEW",
                subject_type="IMPLEMENTATION_CONTRACT",
                subject_id=contract_id,
            )
            if status == "PROPOSED":
                if review is None:
                    source = current_by_worker.get(str(contract.get("proposer") or ""))
                    source_id = ""
                    if (
                        source
                        and source.get("node_id") == contract.get("node_id")
                        and int(source.get("plan_version") or 0) == int(contract.get("plan_version") or 0)
                    ):
                        source_id = str(source.get("assignment_id") or "")
                    route(
                        "OPEN_HANDOFF",
                        node_id=contract.get("node_id"),
                        reason="implementation_contract_requires_review",
                        handoff_kind="REVIEW",
                        subject_type="IMPLEMENTATION_CONTRACT",
                        subject_id=contract_id,
                        target_role="reviewer",
                        requested_by=str(contract.get("proposer") or "researcher"),
                        source_role="researcher",
                        source_assignment_id=source_id,
                    )
                continue
            if status == "APPROVE" and (review is None or review_ready(review)):
                route(
                    "ASSIGN_CONTRACT",
                    node_id=contract.get("node_id"),
                    reason="approved_implementation_contract_ready",
                    contract_id=contract_id,
                    target_role="experimenter",
                )
                continue
            if status not in {"REVISE", "REJECT"} or not review_ready(review):
                continue
            kind = "REVISION" if status == "REVISE" else "RETHINK"
            correction = _latest_handoff(
                handoffs,
                kind=kind,
                subject_type="IMPLEMENTATION_CONTRACT",
                subject_id=contract_id,
            )
            if correction is None:
                review_assignment = handoff_assignment(review)
                route(
                    "OPEN_HANDOFF",
                    node_id=contract.get("node_id"),
                    reason=(
                        "implementation_contract_revision_required"
                        if kind == "REVISION"
                        else "implementation_contract_rethink_required"
                    ),
                    handoff_kind=kind,
                    subject_type="IMPLEMENTATION_CONTRACT",
                    subject_id=contract_id,
                    target_role="researcher",
                    requested_by=str(contract.get("reviewer") or (review_assignment or {}).get("worker") or "reviewer"),
                    source_role="reviewer",
                    source_assignment_id=str((review_assignment or {}).get("assignment_id") or ""),
                    parent_handoff_id=str((review or {}).get("handoff_id") or ""),
                )

        for source in current_assignments:
            if (
                source.get("state") != "BLOCKED"
                or source.get("outcome") != "NEEDS_REVIEW"
                or str(source.get("role") or "").lower() == "reviewer"
            ):
                continue
            source_id = str(source["assignment_id"])
            if any(
                item.get("kind") in {"INTERPRET", "RETHINK", "ESCALATE"}
                and item.get("subject_type") == "ASSIGNMENT"
                and item.get("subject_id") == source_id
                for item in handoffs
            ):
                continue
            review = _latest_handoff(
                handoffs,
                kind="REVIEW",
                subject_type="ASSIGNMENT",
                subject_id=source_id,
            )
            if review is None:
                route(
                    "OPEN_HANDOFF",
                    node_id=source.get("node_id"),
                    reason="assignment_explicitly_requested_review",
                    handoff_kind="REVIEW",
                    subject_type="ASSIGNMENT",
                    subject_id=source_id,
                    target_role="reviewer",
                    requested_by=str(source.get("worker") or "worker"),
                    source_role=str(source.get("role") or "researcher"),
                    source_assignment_id=source_id,
                )
            elif review_ready(review):
                review_assignment = handoff_assignment(review)
                route(
                    "SCIENTIFIC_ROUTE_REQUIRED",
                    node_id=source.get("node_id"),
                    assignment_id=source_id,
                    worker=source.get("worker"),
                    reason="review_completed_source_disposition_ambiguous",
                    subject_type="ASSIGNMENT",
                    subject_id=source_id,
                    source_role=str(source.get("role") or "researcher"),
                    review_handoff_id=str(review.get("handoff_id") or ""),
                    review_assignment_id=str((review_assignment or {}).get("assignment_id") or ""),
                    candidate_routes=["INTERPRET", "RETHINK", "ESCALATE"],
                )

        for change in sorted(
            all_plan_changes,
            key=lambda item: (str(item.get("created_at") or ""), str(item.get("change_id") or "")),
        ):
            proposal_id = _proposal_id_from_change(change)
            if not proposal_id or int(change.get("base_plan_version") or 0) != plan_version:
                continue
            status = str(change.get("status") or "")
            if status not in {"PROPOSED", "APPROVED", "REVISE", "REJECTED"}:
                continue
            change_id = str(change["change_id"])
            review = _latest_handoff(
                handoffs,
                kind="REVIEW",
                subject_type="PLAN_CHANGE",
                subject_id=change_id,
            )
            if status == "PROPOSED":
                if review is None:
                    route(
                        "OPEN_HANDOFF",
                        node_id=plan.get("root_id"),
                        reason="plan_change_requires_review",
                        handoff_kind="REVIEW",
                        subject_type="PLAN_CHANGE",
                        subject_id=change_id,
                        proposal_id=proposal_id,
                        target_role="reviewer",
                        requested_by=str(change.get("proposer") or "researcher"),
                        source_role="researcher",
                    )
                continue
            if status == "APPROVED":
                if review_ready(review):
                    route(
                        "COMMIT_PLAN_CHANGE",
                        node_id=plan.get("root_id"),
                        reason="approved_plan_change_ready",
                        change_id=change_id,
                        proposal_id=proposal_id,
                    )
                continue
            if not review_ready(review):
                continue
            verdict = str(change.get("review_verdict") or "").upper()
            kind = (
                "REVISION"
                if status == "REVISE" or (status == "REJECTED" and verdict != "REJECT")
                else "RETHINK"
            )
            correction = _latest_handoff(
                handoffs,
                kind=kind,
                subject_type="PLAN_CHANGE",
                subject_id=change_id,
            )
            if correction is None:
                review_assignment = handoff_assignment(review)
                route(
                    "OPEN_HANDOFF",
                    node_id=plan.get("root_id"),
                    reason="plan_revision_required" if kind == "REVISION" else "plan_rethink_required",
                    handoff_kind=kind,
                    subject_type="PLAN_CHANGE",
                    subject_id=change_id,
                    proposal_id=proposal_id,
                    target_role="researcher",
                    requested_by=str(change.get("reviewer") or (review_assignment or {}).get("worker") or "reviewer"),
                    source_role="reviewer",
                    source_assignment_id=str((review_assignment or {}).get("assignment_id") or ""),
                    parent_handoff_id=str((review or {}).get("handoff_id") or ""),
                )

    protocol = [_protocol_action(decision, plan_version) for decision in decisions]
    action_order = {
        "REOPEN_HANDOFF": 0,
        "RESOLVE_HANDOFF": 1,
        "OPEN_HANDOFF": 2,
        "COMMIT_PLAN_CHANGE": 3,
        "ASSIGN_CONTRACT": 4,
        "ASSIGN_HANDOFF": 5,
        "STOP_WORKER": 6,
        "REVIEW_ASSIGNMENT": 7,
        "ASSIGN_NODE": 8,
        "START_WORKER": 9,
        "WAIT": 10,
    }
    actions = routing + protocol
    actions.sort(
        key=lambda item: (
            action_order.get(str(item.get("kind") or ""), 99),
            str(item.get("subject_type") or ""),
            str(item.get("subject_id") or item.get("contract_id") or item.get("change_id") or ""),
            str(item.get("handoff_id") or ""),
            str(item.get("node_id") or ""),
            str(item.get("worker") or ""),
        )
    )
    return {
        "project_state": project_state,
        "plan_version": plan_version,
        "plan_changes": pending_plan_changes,
        "frontier": frontier,
        "completed_nodes": sorted(completed),
        "unresolved_implementation_contracts": unresolved_implementation_contracts,
        "pending_handoffs": pending_handoffs,
        "decisions": decisions,
        "actions": actions,
    }


def reconcile(project_dir: Path, worker_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Capture current state and reduce it; retained as the public compatibility entrypoint."""
    return reduce_supervisor_snapshot(build_supervisor_snapshot(project_dir, worker_rows))
