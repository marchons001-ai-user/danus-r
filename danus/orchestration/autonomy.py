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
            break
        if old_node != current_node or not graph.node_compatible(node_id, version, current):
            break
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
    return not ProgressLedger(graph.root)._is_plan_stale(assignment)


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
    current = _assignment_by_id(ledger, assignment_id)
    if not current or current["worker"] != worker:
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

    try:
        ledger.set_node_state(
            node_id,
            state="DONE",
            actor="supervisor",
            note=f"automatic closure from assignment {assignment_id}",
            expected_assignment_id=assignment_id,
        )
    except ValueError:
        return {"action": "AUTO_COMPLETE", "node_id": node_id, "worker": worker, "result": "assignment_changed"}
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
    *, retry_worker: str = "", retry_node: str = "", retry_outcome: str = "BLOCKED",
) -> Optional[L.WorkerLayout]:
    ledger = ProgressLedger(L.project_dir(project))
    rows_by_worker = {str(row.get("worker") or ""): row for row in rows}
    for worker in L.list_workers(project):
        if worker in reserved or (retry_worker and worker != retry_worker):
            continue
        wl = L.WorkerLayout(L.worker_dir(project, worker))
        if role_of(wl) != role or wl.pause.is_file():
            continue
        row = rows_by_worker.get(worker, {})
        if row.get("alive"):
            continue
        current = ledger.current(worker, refresh_stale=True)
        if retry_worker:
            if not (current and current.get("state") == "BLOCKED"
                    and current.get("outcome") == retry_outcome
                    and current.get("node_id") == retry_node):
                continue
            return wl
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
    if (
        current.get("subject_type") == "IMPLEMENTATION_CONTRACT"
        and current.get("kind") == "REVIEW"
    ):
        contract = ledger.implementation_contract(str(current.get("subject_id") or ""))
        if contract is None:
            return {"handoff_id": current["handoff_id"], "result": "missing_contract"}
        if current["node_id"] != contract["node_id"]:
            # A frozen V1 reviewer task can only start after R1 is completed.
            # The E1 experiment itself stays locked until this review resolves.
            graph = ResearchPlanGraph(L.project_dir(project)).snapshot()
            node = graph["nodes"].get(current["node_id"]) or {}
            completed = set(ledger.completed_nodes())
            if node.get("state") != "ACTIVE":
                return {"handoff_id": current["handoff_id"], "result": "review_node_inactive"}
            if any(dep not in completed for dep in node.get("depends_on") or []):
                return {"handoff_id": current["handoff_id"], "result": "waiting_dependencies"}
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
    if contract.get("status") == "STALE":
        return (f"# Reauthorize Implementation Contract\n\n"
                f"Contract `{contract['contract_id']}` for node `{contract['node_id']}` has stale authority.\n"
                f"Reassess this task under the current Plan and node state:\n\n{contract['task']}\n"
                "Submit a fresh contract with implementation_submit_contract using the current Plan version and node. "
                "Independent review is required before execution. End with COMPLETE only after submission.\n")
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
        "a generic verdict or mutate unrelated Plan/Contract state. Classify only whether a bounded scientific route "
        "choice remains after deterministic handling: end with exactly one standalone line "
        "`DANUS_REVIEW_CLASS: SCIENTIFIC_AMBIGUITY` when INTERPRET/RETHINK/ESCALATE still requires scientific "
        "judgment, otherwise `DANUS_REVIEW_CLASS: DETERMINISTIC` for engineering/runtime/data/evidence/governance "
        "cases or any case with an existing deterministic path. Then end with COMPLETE.\n"
    )


def _routing_label(action: Dict[str, Any]) -> str:
    subject_type = str(action.get("subject_type") or "")
    kind = str(action.get("handoff_kind") or "")
    if kind in {"INTERPRET", "RETHINK", "CLARIFY"}:
        return kind
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
    if existing is not None and existing.get("status") != "STALE":
        return existing

    if subject_type == "IMPLEMENTATION_CONTRACT":
        contract = ledger.implementation_contract(subject_id)
        if contract is None:
            return None
        if kind == "REVIEW" and str(action["node_id"]) != contract["node_id"]:
            from .control import _contract_review_node

            graph = ResearchPlanGraph(ledger.root).snapshot()
            expected = _contract_review_node(ledger.root, graph, contract)
            if expected != str(action["node_id"]):
                raise ValueError("contract REVIEW must bind the exact frozen Reviewer predecessor")
            spec = _task_spec(ledger.root, expected)
            # HandoffRequests.open() appends exactly one terminal newline.
            # Preserve the complete frozen V1 task byte-for-byte so AUTO_COMPLETE
            # can check its task hash and close the V1 Plan node.
            request = spec["task"][:-1]
        else:
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


def _apply_routing_events(
    project: str,
    actions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Advance control-plane Handoffs. Never allocate workers here.

    The authoritative dispatch queue is built *after* Handoff mutations, from
    a fresh reducer snapshot. Plan and Contract do not own separate schedulers.
    """
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
                    current["handoff_id"], assignment_id=assignment_id,
                    actor="supervisor", reason="handoff assignment became stale",
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
                    current["handoff_id"], assignment_id=assignment_id,
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
            if current is not None:
                applied.append({
                    "action": _routing_label(action),
                    "handoff_id": current["handoff_id"],
                    "result": "opened" if current["status"] == "OPEN" else "existing",
                })
    return applied


def dispatch_explicit(
    project: str, role: str, task: str, node_id: str, *,
    handoff_id: str = "",
    rows: Optional[List[Dict[str, Any]]] = None,
    reserved: Optional[set[str]] = None,
    assign: Optional[Callable[..., Dict[str, Any]]] = None,
    start: Optional[Callable[[L.WorkerLayout], str]] = None,
    role_of: Optional[Callable[[L.WorkerLayout], str]] = None,
    action: str = "PLAN_ASSIGN",
    retry_worker: str = "", retry_node: str = "", retry_outcome: str = "BLOCKED",
) -> Dict[str, Any]:
    """Shared allocation/claim/launch primitive for all research tasks.

    Legacy bootstrap/repair, Plan Review and reducer-authorized queued work
    use the same atomic ProgressLedger.bind and worker ownership rules. The
    callers must supply their *governed* task and its Plan/Handoff identity.
    """
    from . import cli as CLI

    if rows is None:
        rows = CLI._project_rows(project)
    if reserved is None:
        reserved = set()
    if assign is None:
        assign = CLI.do_assign
    if start is None:
        start = CLI._start_one
    if role_of is None:
        role_of = CLI._read_worker_role
    wl = _idle_worker(project, role, rows, reserved, role_of,
                      retry_worker=retry_worker, retry_node=retry_node,
                      retry_outcome=retry_outcome)
    if wl is None:
        return {"action": action, "role": role, "node_id": node_id, "result": "no_idle_worker"}
    try:
        record = assign(
            f"{project}/{wl.name}", task, node_id=node_id,
            **({"handoff_id": handoff_id} if handoff_id else {}),
        )
    except (SystemExit, ValueError) as exc:
        return {"action": action, "role": role, "node_id": node_id,
                "worker": wl.name, "result": "blocked", "reason": str(exc)}
    reserved.add(wl.name)
    return {
        "action": action, "role": role, "node_id": node_id,
        "worker": wl.name, "assignment_id": record.get("assignment_id"),
        **({"handoff_id": handoff_id} if handoff_id else {}),
        "result": "assigned", "start_result": start(wl),
    }


def _dispatch_candidates(
    project: str,
    *,
    rows: List[Dict[str, Any]],
    assign: Callable[..., Dict[str, Any]],
    start: Callable[[L.WorkerLayout], str],
    role_of: Callable[[L.WorkerLayout], str],
) -> List[Dict[str, Any]]:
    """One arbitration point for every worker assignment.

    Read the *current* Plan, Contract and Handoff states and translate their
    authorized ready work into one queue. The queue is advisory: the existing
    ledger.assign() enforces Plan/Contract/Handoff identity atomically.
    """
    from .control import build_supervisor_snapshot, reduce_supervisor_snapshot, _contract_review_node

    root = L.project_dir(project)
    graph = ResearchPlanGraph(root).snapshot()
    ledger = ProgressLedger(root)
    contracts = ledger.pending_implementation_contracts()
    review_reserved = {
        predecessor
        for contract in contracts
        if str(contract.get("gate_status") or contract.get("status") or "") == "PROPOSED"
        if (predecessor := _contract_review_node(root, graph, contract))
    }
    contract_targets = {str(c.get("node_id") or "") for c in contracts}
    reduction = reduce_supervisor_snapshot(build_supervisor_snapshot(root, rows))
    complete = set(ledger.completed_nodes())

    # A common JSON envelope; no item is executable until the reducer
    # authorized it and the ledger accepts the exact durable binding.
    queue: List[Dict[str, Any]] = []
    deferred: List[Dict[str, Any]] = []
    for action in reduction["actions"]:
        kind = action.get("kind")
        if kind == "ASSIGN_HANDOFF":
            handoff = HandoffRequests(ledger).get(str(action.get("handoff_id") or ""))
            if not handoff or handoff.get("status") != "OPEN":
                continue
            node_id = str(handoff["node_id"])
            node = graph["nodes"].get(node_id)
            if node is None or node.get("state") != "ACTIVE":
                continue
            missing = sorted(
                dep for dep in (node.get("depends_on") or [])
                if dep not in complete
            ) if node_id != graph["root_id"] else []
            if missing:
                deferred.append({
                    "action": "IMPLEMENTATION_REVIEW"
                              if handoff.get("subject_type") == "IMPLEMENTATION_CONTRACT"
                              else "HANDOFF",
                    "node_id": node_id,
                    "handoff_id": handoff["handoff_id"],
                    "role": handoff["target_role"],
                    "result": "waiting_dependencies",
                    "missing_dependencies": missing,
                })
                continue
            queue.append({
                "version": 1, "kind": "HANDOFF", "node_id": node_id,
                "role": handoff["target_role"], "task": handoff["request"],
                "handoff_id": handoff["handoff_id"], "priority": 0,
            })
        elif kind == "ASSIGN_CONTRACT":
            contract = ledger.implementation_contract(str(action.get("contract_id") or ""))
            if not contract or str(contract.get("gate_status") or contract.get("status")) != "APPROVE":
                continue
            node_id = str(contract["node_id"])
            node = graph["nodes"].get(node_id)
            if node is None or node.get("state") != "ACTIVE" or any(
                dep not in complete for dep in node.get("depends_on") or []
            ):
                continue
            queue.append({
                "version": 1, "kind": "CONTRACT", "node_id": node_id,
                "role": "experimenter", "task": contract["task"],
                "contract_id": contract["contract_id"], "priority": 1,
            })
    for decision in reduction["decisions"]:
        if (
            decision.get("action") != "RETASK"
            or decision.get("worker") is not None
            or decision.get("reason") != "ready_node_unassigned"
        ):
            continue
        node_id = str(decision.get("node_id") or "")
        if node_id in review_reserved or node_id in contract_targets:
            continue
        spec = _task_spec(root, node_id)
        if spec.get("status") == "ready":
            queue.append({
                "version": 1, "kind": "PLAN_NODE", "node_id": node_id,
                "role": spec["role"], "task": spec["task"],
                "task_spec": spec["path"], "priority": 2,
            })
        elif spec.get("status") == "invalid":
            queue.append({
                "version": 1, "kind": "INVALID", "node_id": node_id,
                "reason": spec.get("reason"), "priority": 3,
            })

    # A Worker Result may express a preference among *already authorized*
    # candidates, never invent a route or bypass its priority/gates.
    from .intent import terminal_intents

    hints = {
        (str(e["next_intent"].get("kind") or ""),
         str(e["next_intent"].get("subject_id") or ""))
        for e in terminal_intents(root)
    }
    handoff_cache = {
        h["handoff_id"]: h for h in HandoffRequests(ledger).all()
    }

    def intent_rank(item: Dict[str, Any]) -> int:
        if item["kind"] == "HANDOFF":
            handoff = handoff_cache.get(item["handoff_id"]) or {}
            category = (
                "REVIEW_CONTRACT" if handoff.get("subject_type") == "IMPLEMENTATION_CONTRACT"
                else "REVIEW_PLAN" if handoff.get("subject_type") == "PLAN_CHANGE"
                else ""
            )
            return 0 if (category, str(handoff.get("subject_id") or "")) in hints else 1
        if item["kind"] == "CONTRACT":
            return 0 if ("RUN_EXPERIMENT", item["contract_id"]) in hints else 1
        if item["kind"] == "PLAN_NODE":
            return 0 if ("RUN_NODE", item["node_id"]) in hints else 1
        return 1

    queue.sort(key=lambda item: (
        item["priority"], intent_rank(item), str(item.get("node_id") or ""),
        str(item.get("handoff_id") or item.get("contract_id") or ""),
    ))
    reserved_workers: set[str] = set()
    dispatched_nodes: set[str] = set()
    applied: List[Dict[str, Any]] = []

    for item in queue:
        kind = item["kind"]
        node_id = item["node_id"]
        if kind == "HANDOFF":
            source = handoff_cache.get(item["handoff_id"]) or {}
            action_name = (
                "IMPLEMENTATION_REVIEW"
                if source.get("subject_type") == "IMPLEMENTATION_CONTRACT"
                else "HANDOFF"
            )
            if source.get("kind") in {"INTERPRET", "RETHINK", "CLARIFY"}:
                action_name = source["kind"]
        else:
            action_name = {
                "CONTRACT": "IMPLEMENTATION_ASSIGN",
                "PLAN_NODE": "AUTO_DISPATCH",
                "INVALID": "AUTO_DISPATCH",
            }[kind]
        if kind == "INVALID":
            applied.append({"action": action_name, "node_id": node_id,
                            "result": "blocked", "reason": item.get("reason")})
            continue
        if node_id in dispatched_nodes:
            applied.append({"action": action_name, "node_id": node_id,
                            "result": "already_scheduled"})
            continue
        result = dispatch_explicit(
            project, item["role"], item["task"], node_id,
            handoff_id=item.get("handoff_id", ""),
            rows=rows, reserved=reserved_workers, assign=assign,
            start=start, role_of=role_of, action=action_name,
        )
        if result["result"] == "assigned":
            dispatched_nodes.add(node_id)
        result.update({
            **({"contract_id": item["contract_id"]} if kind == "CONTRACT" else {}),
            **({"task_spec": item["task_spec"]} if kind == "PLAN_NODE" else {}),
        })
        applied.append(result)
    return deferred + applied



def retry_blocked_review(project: str) -> Dict[str, Any]:
    """Retry one blocked Reviewer after an explicit operator restart."""
    from . import cli as CLI

    root = L.project_dir(project)
    ledger = ProgressLedger(root)
    plan = ResearchPlanGraph(root).snapshot()
    candidates = [
        a for a in ledger.snapshot(refresh_stale=True)
        if a.get("role") == "reviewer"
        and a.get("state") == "BLOCKED" and a.get("outcome") == "BLOCKED"
        and not ledger._is_plan_stale(a)
    ]
    if len(candidates) != 1:
        raise ValueError("retry requires exactly one blocked Reviewer assignment")
    source = candidates[0]
    node_id = source["node_id"]
    node = plan["nodes"].get(node_id)
    completed = set(ledger.completed_nodes())
    if (
        node is None or node.get("state") != "ACTIVE"
        or node_id in completed
        or any(dep not in completed for dep in node.get("depends_on") or [])
        or any(item.get("parent") == node_id and item.get("state") == "ACTIVE"
               for item in plan["nodes"].values())
    ):
        raise ValueError("blocked Reviewer Plan node is not ready for retry")
    spec = _task_spec(root, node_id)
    if (spec.get("status") != "ready" or spec.get("role") != "reviewer"
            or ProgressLedger.task_sha256(spec["task"]) != source.get("task_sha256")):
        raise ValueError("blocked Reviewer task changed since its Assignment")
    if any(
        h.get("node_id") == node_id and h.get("status") in {"OPEN", "ASSIGNED"}
        and h.get("subject_type") == "ASSIGNMENT"
        and h.get("subject_id") == source["assignment_id"]
        for h in HandoffRequests(ledger).all()
    ):
        raise ValueError("blocked Reviewer's escalation is still pending")
    # Only this Supervisor-owned pause is recoverable. Manual, budget and
    # quota pauses are never cleared by an audit retry.
    wl = L.WorkerLayout(L.worker_dir(project, source["worker"]))
    rows = CLI._project_rows(project)
    worker_row = next((row for row in rows if row.get("worker") == source["worker"]), None)
    if (root / L.PAUSE_FILE).exists() or worker_row is None or worker_row.get("alive"):
        raise ValueError("blocked Reviewer worker or project is still running or paused")
    pause_reason = CLI._pause_reason(wl)
    if pause_reason not in {None, "review:assignment_blocked"}:
        raise ValueError(f"blocked Reviewer has unrelated pause: {pause_reason}")
    if pause_reason:
        wl.pause.unlink()
        rows = CLI._project_rows(project)
    result = dispatch_explicit(
        project, "reviewer", spec["task"], node_id,
        rows=rows, assign=CLI.do_assign, start=CLI._start_one,
        role_of=CLI._read_worker_role, action="RETRY_BLOCKED_REVIEW",
        retry_worker=source["worker"], retry_node=node_id,
    )
    if result.get("result") != "assigned":
        raise ValueError("blocked Reviewer retry failed: " + str(result))
    return result



def recover_review_after_provenance_fix(project: str) -> Dict[str, Any]:
    """Explicitly re-audit a blocked Reviewer after a validated provenance-access fix.

    This does not accept a scientific conclusion or reopen/complete a Plan node.
    The historical NEEDS_REVIEW and ESCALATE remain in append-only records.
    No ordinary or unrelated main escalation may be cleared by this route.
    """
    from . import cli as CLI
    from danus.execution.loop import _review_governance_context
    from danus.core.evidence import EvidenceStore

    root = L.project_dir(project)
    graph = ResearchPlanGraph(root)
    plan = graph.snapshot()
    ledger = ProgressLedger(root)
    sources = [
        a for a in ledger.snapshot(refresh_stale=True)
        if a.get("role") == "reviewer"
        and a.get("state") == "BLOCKED"
        and a.get("outcome") == "NEEDS_REVIEW"
        and not ledger._is_plan_stale(a)
    ]
    if len(sources) != 1:
        raise ValueError("provenance recovery requires exactly one current NEEDS_REVIEW Reviewer")
    source = sources[0]
    node_id = source["node_id"]
    node = plan["nodes"].get(node_id)
    done = set(ledger.completed_nodes())
    if (node is None or node.get("state") != "ACTIVE" or node_id in done
        or any(dep not in done for dep in node.get("depends_on") or [])
        or any(n.get("parent") == node_id and n.get("state") == "ACTIVE"
               for n in plan["nodes"].values())):
        raise ValueError("Reviewer node is not ready for an unchanged re-audit")
    spec = _task_spec(root, node_id)
    if (spec.get("status") != "ready" or spec.get("role") != "reviewer"
        or ProgressLedger.task_sha256(spec["task"]) != source.get("task_sha256")):
        raise ValueError("Reviewer frozen task changed")

    # Validate the *committed* post-hoc change and its actual review authority.
    change_id = str(plan.get("committed_change_id") or "")
    if not change_id:
        raise ValueError("no committed Plan change provides provenance")
    change = graph.get_change(change_id)
    if (change.get("status") != "COMMITTED"
        or change.get("review_verdict") != "APPROVE"
        or not change.get("post_hoc")
        or not change.get("review_assignment_id")
        or not change.get("evidence_refs")
        or graph.committed_version(change, plan) != plan["plan_version"]):
        raise ValueError("committed post-hoc Plan provenance is not valid for recovery")
    graph._require_completed_review(change)
    store = EvidenceStore(root)
    if any(store.get_record(str(eid)) is None for eid in change["evidence_refs"]):
        raise ValueError("committed Plan provenance references missing evidence")
    context = _review_governance_context(project, "reviewer", source["worker"])
    if "<TRUSTED_PLAN_PROVENANCE>" not in context:
        raise ValueError("the updated Reviewer task does not receive verified Plan provenance")

    handoffs = HandoffRequests(ledger)
    related = [
        h for h in handoffs.all()
        if h.get("subject_type") == "ASSIGNMENT"
        and h.get("subject_id") == source["assignment_id"]
        and h.get("node_id") == node_id
    ]
    pending = [h for h in related if h.get("status") in {"OPEN", "ASSIGNED"}]
    retriable_cancellation = [
        h for h in related
        if h.get("status") == "CANCELLED"
        and h.get("terminal_reason") ==
        "source re-audit authorized after verified committed Plan provenance became available"
    ]
    # A previous attempt may have retired the handoff but failed to launch.
    # Permit only that exact interrupted recovery, never an arbitrary cancellation.
    if len(pending) == 1:
        handoff = pending[0]
    elif not pending and len(retriable_cancellation) == 1:
        handoff = retriable_cancellation[0]
    else:
        raise ValueError("requires one pending escalation or precisely one interrupted recovery")
    if (handoff.get("kind") != "ESCALATE"
        or handoff.get("target_role") != "main"
        or handoff.get("status") not in {"OPEN", "CANCELLED"}
        or handoff.get("source_role") != "reviewer"
        or int(handoff.get("plan_version") or 0) != plan["plan_version"]):
        raise ValueError("pending escalation is not a recoverable Reviewer provenance request")
    reason = str(handoff.get("reason") or "").lower() + " " + str(source.get("result") or "").lower()
    if not any(term in reason for term in ("provenance", "revision", "plan change", "plan v3")):
        raise ValueError("the escalation does not identify a Plan provenance problem")

    wl = L.WorkerLayout(L.worker_dir(project, source["worker"]))
    rows = CLI._project_rows(project)
    row = next((r for r in rows if r.get("worker") == source["worker"]), None)
    if (root / L.PAUSE_FILE).exists() or row is None or row.get("alive"):
        raise ValueError("Reviewer or project is still running or paused")
    pause_reason = CLI._pause_reason(wl)
    if pause_reason not in {None, "review:assignment_needs_review"}:
        raise ValueError(f"unrelated Reviewer pause cannot be cleared: {pause_reason}")

    # Do not mislabel an unanswered main escalation as resolved. Retire only
    # this outdated request and ask the Reviewer to decide again independently.
    if handoff["status"] == "OPEN":
        handoffs.cancel(
            handoff["handoff_id"], actor="supervisor",
            reason="source re-audit authorized after verified committed Plan provenance became available",
        )
    if pause_reason:
        wl.pause.unlink()
        rows = CLI._project_rows(project)
    result = dispatch_explicit(
        project, "reviewer", spec["task"], node_id,
        rows=rows, assign=CLI.do_assign, start=CLI._start_one,
        role_of=CLI._read_worker_role, action="RETRY_PLAN_PROVENANCE_REVIEW",
        retry_worker=source["worker"], retry_node=node_id,
        retry_outcome="NEEDS_REVIEW",
    )
    if result.get("result") != "assigned":
        raise ValueError("Reviewer recovery dispatch failed: " + str(result))
    return {**result, "retired_handoff_id": handoff["handoff_id"],
            "previous_assignment_id": source["assignment_id"],
            "validated_change_id": change_id}


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

    from .control import build_supervisor_snapshot, reduce_supervisor_snapshot

    # Mutate governance events first; do not assign workers while the
    # reducer is still using a stale event snapshot.
    route = reduce_supervisor_snapshot(
        build_supervisor_snapshot(project_dir, rows)
    )
    applied.extend(_apply_routing_events(project, route["actions"]))

    # Every Plan/Handoff/Contract Assignment now passes one arbiter. Its
    # fresh state read sees just-opened Handoffs and completed dependencies.
    applied.extend(_dispatch_candidates(
        project, rows=rows, assign=assign, start=start, role_of=role_of,
    ))
    return applied
