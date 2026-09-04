"""Deterministic auto-dispatch, handoff execution, and safe completion.

Scientific routing decisions come from ``control.reduce_supervisor_snapshot``. This
module performs the requested durable mutations and worker operations; it does not
choose Plan/Contract/Handoff routes itself.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
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


def _assignment_by_id(ledger: ProgressLedger, assignment_id: str) -> Optional[Dict[str, Any]]:
    assignment_id = str(assignment_id or "")
    return next(
        (item for item in ledger.assignments() if item.get("assignment_id") == assignment_id),
        None,
    )


def _handoff_for_subject(
    handoffs: HandoffRequests,
    *,
    kind: str,
    subject_type: str,
    subject_id: str,
) -> Optional[Dict[str, Any]]:
    matches = [
        item
        for item in handoffs.all()
        if item.get("kind") == kind
        and item.get("subject_type") == subject_type
        and item.get("subject_id") == subject_id
    ]
    matches.sort(key=lambda item: (str(item.get("created_at") or ""), item["handoff_id"]))
    return matches[-1] if matches else None


def _handoff_assignment(ledger: ProgressLedger, handoff: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    assignment_id = str(
        handoff.get("assigned_assignment_id")
        or handoff.get("resolution_assignment_id")
        or ""
    )
    return _assignment_by_id(ledger, assignment_id)


def _assignment_complete(assignment: Optional[Dict[str, Any]]) -> bool:
    return bool(
        assignment
        and assignment.get("state") == "DONE"
        and assignment.get("outcome") == "COMPLETE"
    )


def _dispatch_handoff(
    project: str,
    handoff: Dict[str, Any],
    *,
    rows: List[Dict[str, Any]],
    reserved: set[str],
    assign: Callable[..., Dict[str, Any]],
    start: Callable[[L.WorkerLayout], str],
    role_of: Callable[[L.WorkerLayout], str],
) -> Dict[str, Any]:
    """Execute ASSIGN_HANDOFF for one already OPEN handoff."""
    ledger = ProgressLedger(L.project_dir(project))
    current = HandoffRequests(ledger).get(handoff["handoff_id"]) or handoff
    assignment = _handoff_assignment(ledger, current)
    if current["status"] == "ASSIGNED":
        return {
            "handoff_id": current["handoff_id"],
            "role": current["target_role"],
            "worker": assignment.get("worker") if assignment else None,
            "assignment_id": current.get("assigned_assignment_id"),
            "result": "existing" if assignment else "missing_assignment",
            "state": assignment.get("state") if assignment else None,
            "outcome": assignment.get("outcome") if assignment else None,
        }
    if current["status"] != "OPEN":
        return {
            "handoff_id": current["handoff_id"],
            "role": current["target_role"],
            "result": current["status"].lower(),
        }
    role = str(current["target_role"])
    wl = _idle_worker(project, role, rows, reserved, role_of)
    if wl is None:
        return {
            "handoff_id": current["handoff_id"],
            "role": role,
            "result": "no_idle_worker",
        }
    try:
        assignment = assign(
            f"{project}/{wl.name}",
            current["request"],
            node_id=current["node_id"],
            handoff_id=current["handoff_id"],
        )
    except SystemExit as exc:
        return {
            "handoff_id": current["handoff_id"],
            "role": role,
            "worker": wl.name,
            "result": "blocked",
            "reason": str(exc),
        }
    reserved.add(wl.name)
    return {
        "handoff_id": current["handoff_id"],
        "role": role,
        "worker": wl.name,
        "assignment_id": assignment.get("assignment_id"),
        "result": "assigned",
        "start_result": start(wl),
    }


def _implementation_review_task(contract: Dict[str, Any]) -> str:
    return (
        "# Implementation Contract Review\n\n"
        f"Review implementation contract `{contract['contract_id']}` for Plan node "
        f"`{contract['node_id']}` under Plan v{contract['plan_version']}. Judge whether the exact "
        "implementation task preserves the scientific/control invariants and acceptance conditions.\n\n"
        f"{contract['task']}\n"
        f"Call `implementation_review_contract(contract_id=\"{contract['contract_id']}\", "
        "verdict=<APPROVE|REVISE|REJECT>, rationale=<specific rationale>)` exactly once. "
        "Do not implement the task. After recording the verdict, end with COMPLETE.\n"
    )


def _implementation_correction_task(contract: Dict[str, Any], kind: str) -> str:
    heading = "Revise Implementation Contract" if kind == "REVISION" else "Rethink Rejected Implementation Contract"
    instruction = (
        "Make the smallest correction that addresses the blocking rationale."
        if kind == "REVISION"
        else "Choose a materially different implementation direction; do not resubmit the rejected route cosmetically."
    )
    return (
        f"# {heading}\n\n"
        f"The Reviewer returned {contract.get('gate_status') or contract.get('status')} for implementation "
        f"contract `{contract['contract_id']}` on node `{contract['node_id']}`. {instruction}\n\n"
        f"Reviewer rationale:\n{str(contract.get('rationale') or 'No rationale recorded')}\n\n"
        f"Original exact contract:\n\n{contract['task']}\n"
        "Submit exactly one corrected replacement with `implementation_submit_contract(...)` using the current "
        "Plan version and node. The control plane derives replacement lineage from this correction handoff; do "
        "not edit ledger files. After the replacement contract is successfully submitted, end with COMPLETE.\n"
    )


def _generic_review_task(source: Dict[str, Any]) -> str:
    note = str(source.get("note") or "No review note was recorded.")
    return (
        "# Assignment Scientific Review\n\n"
        f"Review source assignment `{source['assignment_id']}` from role `{source.get('role')}` on Plan node "
        f"`{source['node_id']}`. The source explicitly ended with NEEDS_REVIEW.\n\n"
        f"Source note:\n{note}\n\n"
        "Inspect the source task/workspace and use only your existing role-authorized tools when a concrete action "
        "is already justified. This generic review has no universal APPROVE/REVISE/REJECT semantics: do not invent "
        "a generic verdict or mutate unrelated Plan/Contract state. End with COMPLETE when the review itself is done.\n"
    )


def _routing_label(action: Dict[str, Any]) -> str:
    subject_type = str(action.get("subject_type") or "")
    kind = str(action.get("handoff_kind") or "")
    if kind == "INTERPRET":
        return "INTERPRET"
    if subject_type == "IMPLEMENTATION_CONTRACT":
        return "IMPLEMENTATION_REVIEW" if kind == "REVIEW" else "IMPLEMENTATION_CORRECTION"
    if subject_type == "ASSIGNMENT":
        return "GENERIC_REVIEW"
    return "HANDOFF"


def _open_nonplan_handoff(
    ledger: ProgressLedger,
    handoffs: HandoffRequests,
    action: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    subject_type = str(action.get("subject_type") or "")
    subject_id = str(action.get("subject_id") or "")
    kind = str(action.get("handoff_kind") or "")
    existing = _handoff_for_subject(
        handoffs,
        kind=kind,
        subject_type=subject_type,
        subject_id=subject_id,
    )
    if existing is not None:
        return existing

    if subject_type == "IMPLEMENTATION_CONTRACT":
        contract = ledger.implementation_contract(subject_id)
        if contract is None:
            return None
        request = (
            _implementation_review_task(contract)
            if kind == "REVIEW"
            else _implementation_correction_task(contract, kind)
        )
        reason = {
            "REVIEW": "implementation contract requires independent review",
            "REVISION": "reviewer requested implementation contract revision",
            "RETHINK": "reviewer rejected implementation direction",
        }.get(kind, str(action.get("reason") or "deterministic handoff route"))
    elif subject_type == "ASSIGNMENT" and kind == "REVIEW":
        source = _assignment_by_id(ledger, subject_id)
        if source is None:
            return None
        request = _generic_review_task(source)
        reason = "assignment explicitly requested scientific review"
    else:
        return None

    return handoffs.open(
        kind=kind,
        plan_version=int(action["plan_version"]),
        node_id=str(action["node_id"]),
        subject_type=subject_type,
        subject_id=subject_id,
        requested_by=str(action.get("requested_by") or "supervisor"),
        source_role=str(action.get("source_role") or "researcher"),
        source_assignment_id=str(action.get("source_assignment_id") or ""),
        parent_handoff_id=str(action.get("parent_handoff_id") or ""),
        request=request,
        reason=reason,
    )


def _apply_routing_actions(
    project: str,
    actions: List[Dict[str, Any]],
    *,
    rows: List[Dict[str, Any]],
    reserved: set[str],
    assign: Callable[..., Dict[str, Any]],
    start: Callable[[L.WorkerLayout], str],
    role_of: Callable[[L.WorkerLayout], str],
) -> List[Dict[str, Any]]:
    """Execute reducer-owned Contract/Handoff actions; Plan actions stay in planning.py."""
    ledger = ProgressLedger(L.project_dir(project))
    handoffs = HandoffRequests(ledger)
    applied: List[Dict[str, Any]] = []

    for action in actions:
        kind = str(action.get("kind") or "")
        if action.get("subject_type") == "PLAN_CHANGE" or kind == "COMMIT_PLAN_CHANGE":
            continue

        if kind == "WAIT" and action.get("reason") == "escalation_pending_main":
            applied.append({
                "action": "ESCALATE",
                "handoff_id": action.get("handoff_id"),
                "source_assignment_id": action.get("subject_id"),
                "result": "pending_main",
            })
            continue

        if kind == "REOPEN_HANDOFF":
            current = handoffs.get(str(action.get("handoff_id") or ""))
            assignment_id = str(action.get("assignment_id") or "")
            if current and current.get("status") == "ASSIGNED" and assignment_id:
                current = handoffs.reopen(
                    current["handoff_id"],
                    assignment_id=assignment_id,
                    actor="supervisor",
                    reason="handoff assignment became stale",
                )
                applied.append({
                    "action": _routing_label(action),
                    "handoff_id": current["handoff_id"],
                    "result": "reopened",
                })
            continue

        if kind == "RESOLVE_HANDOFF":
            current = handoffs.get(str(action.get("handoff_id") or ""))
            assignment_id = str(action.get("assignment_id") or "")
            assignment = _assignment_by_id(ledger, assignment_id)
            if current and current.get("status") == "ASSIGNED" and assignment:
                current = handoffs.resolve(
                    current["handoff_id"],
                    assignment_id=assignment_id,
                    actor=str(assignment.get("worker") or "supervisor"),
                    summary=str(action.get("summary") or "handoff work completed"),
                )
                item = {
                    "action": _routing_label(action),
                    "handoff_id": current["handoff_id"],
                    "result": "resolved",
                }
                if action.get("replacement_contract_id"):
                    item["replacement_contract_id"] = action["replacement_contract_id"]
                applied.append(item)
            continue

        if kind == "OPEN_HANDOFF":
            current = _open_nonplan_handoff(ledger, handoffs, action)
            if current is None:
                continue
            routed = _dispatch_handoff(
                project,
                current,
                rows=rows,
                reserved=reserved,
                assign=assign,
                start=start,
                role_of=role_of,
            )
            applied.append({"action": _routing_label(action), **routed})
            continue

        if kind == "ASSIGN_HANDOFF":
            current = handoffs.get(str(action.get("handoff_id") or ""))
            if current is None:
                continue
            routed = _dispatch_handoff(
                project,
                current,
                rows=rows,
                reserved=reserved,
                assign=assign,
                start=start,
                role_of=role_of,
            )
            applied.append({"action": _routing_label({**action, "handoff_kind": current.get("kind")}), **routed})
            continue

        if kind != "ASSIGN_CONTRACT":
            continue
        contract = ledger.implementation_contract(str(action.get("contract_id") or ""))
        if contract is None:
            continue
        wl = _idle_worker(project, "experimenter", rows, reserved, role_of)
        if wl is None:
            applied.append({
                "action": "IMPLEMENTATION_ASSIGN",
                "contract_id": contract["contract_id"],
                "role": "experimenter",
                "result": "no_idle_worker",
            })
            continue
        try:
            assignment = assign(
                f"{project}/{wl.name}",
                contract["task"],
                node_id=contract["node_id"],
            )
        except SystemExit as exc:
            applied.append({
                "action": "IMPLEMENTATION_ASSIGN",
                "contract_id": contract["contract_id"],
                "role": "experimenter",
                "worker": wl.name,
                "result": "blocked",
                "reason": str(exc),
            })
            continue
        reserved.add(wl.name)
        applied.append({
            "action": "IMPLEMENTATION_ASSIGN",
            "contract_id": contract["contract_id"],
            "role": "experimenter",
            "worker": wl.name,
            "assignment_id": assignment.get("assignment_id"),
            "result": "assigned",
            "start_result": start(wl),
        })
    return applied


def apply_autonomy(
    project: str,
    decisions: List[Dict[str, Any]],
    *,
    rows: List[Dict[str, Any]],
    assign: Callable[..., Dict[str, Any]],
    start: Callable[[L.WorkerLayout], str],
    role_of: Callable[[L.WorkerLayout], str],
) -> List[Dict[str, Any]]:
    """Apply safe completion plus reducer-owned scientific routing actions."""
    project_dir = L.project_dir(project)
    if (project_dir / L.PAUSE_FILE).is_file():
        return []

    applied: List[Dict[str, Any]] = []
    for decision in decisions:
        result = _auto_complete(project_dir, decision)
        if result is not None:
            applied.append(result)
    applied.extend(_close_completed_containers(project_dir))

    # Ordinary task-file dispatch keeps the established resource-overlay behavior
    # in S3-B. Plan/Contract/Handoff scientific routing below is reducer-owned.
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
        applied.append({
            "action": "AUTO_DISPATCH",
            "node_id": node_id,
            "role": spec["role"],
            "worker": wl.name,
            "assignment_id": assignment.get("assignment_id"),
            "task_spec": spec["path"],
            "task_plan_version": spec["spec_plan_version"],
            "result": "assigned",
            "start_result": start(wl),
        })

    from .control import build_supervisor_snapshot, reduce_supervisor_snapshot

    routing_actions = reduce_supervisor_snapshot(
        build_supervisor_snapshot(project_dir, rows)
    )["actions"]
    applied.extend(
        _apply_routing_actions(
            project,
            routing_actions,
            rows=rows,
            reserved=reserved,
            assign=assign,
            start=start,
            role_of=role_of,
        )
    )
    return applied
