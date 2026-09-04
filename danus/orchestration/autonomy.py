"""Deterministic auto-dispatch, handoff routing, and safe completion.

Ordinary Plan-node work still opts in through one exact versioned task file under
``tasks/v<plan_version>/<role>/<node_id>.md``. Durable HandoffRequests are used only
for role-to-role work that already has explicit semantics; this module does not
invent scientific verdicts.
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
    ledger = ProgressLedger(L.project_dir(project))
    handoffs = HandoffRequests(ledger)
    current = handoffs.get(handoff["handoff_id"]) or handoff
    assignment = _handoff_assignment(ledger, current)
    if current["status"] == "ASSIGNED" and assignment and assignment.get("state") == "STALE":
        current = handoffs.reopen(
            current["handoff_id"],
            assignment_id=assignment["assignment_id"],
            actor="supervisor",
            reason="handoff assignment became stale",
        )
        assignment = None
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


def _apply_implementation_governance(
    project: str,
    *,
    rows: List[Dict[str, Any]],
    reserved: set[str],
    assign: Callable[..., Dict[str, Any]],
    start: Callable[[L.WorkerLayout], str],
    role_of: Callable[[L.WorkerLayout], str],
) -> List[Dict[str, Any]]:
    ledger = ProgressLedger(L.project_dir(project))
    handoffs = HandoffRequests(ledger)
    applied: List[Dict[str, Any]] = []

    # Resolve/recover correction handoffs independently of which contract is now
    # latest on the node; a replacement immediately supersedes the old pending view.
    contracts = ledger.implementation_contracts()
    replacements: Dict[str, List[Dict[str, Any]]] = {}
    for contract in contracts:
        parent = str(contract.get("replaces_contract_id") or "")
        if parent:
            replacements.setdefault(parent, []).append(contract)
    for handoff in list(handoffs.pending()):
        if handoff.get("subject_type") != "IMPLEMENTATION_CONTRACT" or handoff.get("kind") not in {"REVISION", "RETHINK"}:
            continue
        assignment = _handoff_assignment(ledger, handoff)
        if handoff.get("status") == "ASSIGNED" and _assignment_complete(assignment):
            candidates = replacements.get(str(handoff.get("subject_id") or ""), [])
            if candidates:
                replacement = candidates[-1]
                resolved = handoffs.resolve(
                    handoff["handoff_id"],
                    assignment_id=assignment["assignment_id"],
                    actor=str(assignment.get("worker") or "researcher"),
                    summary=f"replacement implementation contract {replacement['contract_id']} captured",
                )
                applied.append({
                    "action": "IMPLEMENTATION_CORRECTION",
                    "handoff_id": resolved["handoff_id"],
                    "replacement_contract_id": replacement["contract_id"],
                    "result": "resolved",
                })
                continue
            applied.append({
                "action": "IMPLEMENTATION_CORRECTION",
                "handoff_id": handoff["handoff_id"],
                "result": "replacement_not_recorded",
            })
            continue
        routed = _dispatch_handoff(
            project,
            handoff,
            rows=rows,
            reserved=reserved,
            assign=assign,
            start=start,
            role_of=role_of,
        )
        applied.append({"action": "IMPLEMENTATION_CORRECTION", **routed})

    for contract in ledger.pending_implementation_contracts():
        status = str(contract.get("gate_status") or contract.get("status") or "")
        if status == "STALE":
            continue
        contract_id = str(contract["contract_id"])
        review = _handoff_for_subject(
            handoffs,
            kind="REVIEW",
            subject_type="IMPLEMENTATION_CONTRACT",
            subject_id=contract_id,
        )
        if status == "PROPOSED":
            if review is None:
                source = ledger.current(str(contract.get("proposer") or ""), refresh_stale=True)
                source_id = ""
                if (
                    source
                    and source.get("node_id") == contract.get("node_id")
                    and int(source.get("plan_version") or 0) == int(contract.get("plan_version") or 0)
                ):
                    source_id = str(source.get("assignment_id") or "")
                review = handoffs.open(
                    kind="REVIEW",
                    plan_version=int(contract["plan_version"]),
                    node_id=str(contract["node_id"]),
                    subject_type="IMPLEMENTATION_CONTRACT",
                    subject_id=contract_id,
                    requested_by=str(contract.get("proposer") or "researcher"),
                    source_role="researcher",
                    source_assignment_id=source_id,
                    request=_implementation_review_task(contract),
                    reason="implementation contract requires independent review",
                )
            routed = _dispatch_handoff(
                project,
                review,
                rows=rows,
                reserved=reserved,
                assign=assign,
                start=start,
                role_of=role_of,
            )
            applied.append({
                "action": "IMPLEMENTATION_REVIEW",
                "contract_id": contract_id,
                **routed,
            })
            continue

        review_assignment = _handoff_assignment(ledger, review) if review else None
        if review and review.get("status") == "ASSIGNED" and _assignment_complete(review_assignment):
            review = handoffs.resolve(
                review["handoff_id"],
                assignment_id=review_assignment["assignment_id"],
                actor=str(contract.get("reviewer") or review_assignment.get("worker") or "reviewer"),
                summary=f"implementation review recorded {status}",
            )
            applied.append({
                "action": "IMPLEMENTATION_REVIEW",
                "contract_id": contract_id,
                "handoff_id": review["handoff_id"],
                "result": "resolved",
                "verdict": status,
            })
        if review and review.get("status") != "RESOLVED":
            routed = _dispatch_handoff(
                project,
                review,
                rows=rows,
                reserved=reserved,
                assign=assign,
                start=start,
                role_of=role_of,
            )
            applied.append({"action": "IMPLEMENTATION_REVIEW", "contract_id": contract_id, **routed})
            continue

        if status == "APPROVE":
            wl = _idle_worker(project, "experimenter", rows, reserved, role_of)
            if wl is None:
                applied.append({
                    "action": "IMPLEMENTATION_ASSIGN",
                    "contract_id": contract_id,
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
                    "contract_id": contract_id,
                    "role": "experimenter",
                    "worker": wl.name,
                    "result": "blocked",
                    "reason": str(exc),
                })
                continue
            reserved.add(wl.name)
            applied.append({
                "action": "IMPLEMENTATION_ASSIGN",
                "contract_id": contract_id,
                "role": "experimenter",
                "worker": wl.name,
                "assignment_id": assignment.get("assignment_id"),
                "result": "assigned",
                "start_result": start(wl),
            })
            continue

        if status not in {"REVISE", "REJECT"} or review is None or review_assignment is None:
            continue
        kind = "REVISION" if status == "REVISE" else "RETHINK"
        correction = _handoff_for_subject(
            handoffs,
            kind=kind,
            subject_type="IMPLEMENTATION_CONTRACT",
            subject_id=contract_id,
        )
        if correction is None:
            correction = handoffs.open(
                kind=kind,
                plan_version=int(contract["plan_version"]),
                node_id=str(contract["node_id"]),
                subject_type="IMPLEMENTATION_CONTRACT",
                subject_id=contract_id,
                requested_by=str(contract.get("reviewer") or review_assignment.get("worker") or "reviewer"),
                source_role="reviewer",
                source_assignment_id=str(review_assignment.get("assignment_id") or ""),
                parent_handoff_id=review["handoff_id"],
                request=_implementation_correction_task(contract, kind),
                reason=(
                    "reviewer requested implementation contract revision"
                    if kind == "REVISION"
                    else "reviewer rejected implementation direction"
                ),
            )
        routed = _dispatch_handoff(
            project,
            correction,
            rows=rows,
            reserved=reserved,
            assign=assign,
            start=start,
            role_of=role_of,
        )
        applied.append({
            "action": "IMPLEMENTATION_CORRECTION",
            "contract_id": contract_id,
            "handoff_kind": kind,
            "parent_handoff_id": correction.get("parent_handoff_id") or None,
            **routed,
        })
    return applied


def _apply_upward_handoffs(
    project: str,
    *,
    rows: List[Dict[str, Any]],
    reserved: set[str],
    assign: Callable[..., Dict[str, Any]],
    start: Callable[[L.WorkerLayout], str],
    role_of: Callable[[L.WorkerLayout], str],
) -> List[Dict[str, Any]]:
    """Dispatch INTERPRET and expose ESCALATE without inventing disposition."""
    ledger = ProgressLedger(L.project_dir(project))
    handoffs = HandoffRequests(ledger)
    applied: List[Dict[str, Any]] = []
    for handoff in list(handoffs.pending()):
        kind = str(handoff.get("kind") or "")
        if kind == "ESCALATE":
            applied.append({
                "action": "ESCALATE",
                "handoff_id": handoff["handoff_id"],
                "source_assignment_id": handoff.get("source_assignment_id"),
                "result": "pending_main",
            })
            continue
        if kind != "INTERPRET":
            continue
        assignment = _handoff_assignment(ledger, handoff)
        if handoff.get("status") == "ASSIGNED" and _assignment_complete(assignment):
            resolved = handoffs.resolve(
                handoff["handoff_id"],
                assignment_id=assignment["assignment_id"],
                actor=str(assignment.get("worker") or "researcher"),
                summary="interpretation request completed; source disposition remains explicit",
            )
            applied.append({
                "action": "INTERPRET",
                "handoff_id": resolved["handoff_id"],
                "source_assignment_id": resolved.get("source_assignment_id"),
                "result": "resolved",
            })
            continue
        routed = _dispatch_handoff(
            project,
            handoff,
            rows=rows,
            reserved=reserved,
            assign=assign,
            start=start,
            role_of=role_of,
        )
        applied.append({
            "action": "INTERPRET",
            "source_assignment_id": handoff.get("source_assignment_id"),
            **routed,
        })
    return applied


def _apply_generic_needs_review(
    project: str,
    decisions: List[Dict[str, Any]],
    *,
    rows: List[Dict[str, Any]],
    reserved: set[str],
    assign: Callable[..., Dict[str, Any]],
    start: Callable[[L.WorkerLayout], str],
    role_of: Callable[[L.WorkerLayout], str],
) -> List[Dict[str, Any]]:
    ledger = ProgressLedger(L.project_dir(project))
    handoffs = HandoffRequests(ledger)
    by_id = {item["assignment_id"]: item for item in ledger.assignments()}
    applied: List[Dict[str, Any]] = []

    for decision in decisions:
        if decision.get("action") != "REVIEW" or decision.get("reason") != "assignment_needs_review":
            continue
        source = by_id.get(str(decision.get("assignment_id") or ""))
        if not source or str(source.get("role") or "").lower() == "reviewer":
            continue
        if any(
            item.get("kind") in {"INTERPRET", "ESCALATE"}
            and item.get("subject_type") == "ASSIGNMENT"
            and item.get("subject_id") == source["assignment_id"]
            for item in handoffs.pending()
        ):
            continue
        handoff = _handoff_for_subject(
            handoffs,
            kind="REVIEW",
            subject_type="ASSIGNMENT",
            subject_id=source["assignment_id"],
        )
        if handoff is None:
            handoff = handoffs.open(
                kind="REVIEW",
                plan_version=int(source["plan_version"]),
                node_id=str(source["node_id"]),
                subject_type="ASSIGNMENT",
                subject_id=str(source["assignment_id"]),
                requested_by=str(source.get("worker") or "worker"),
                source_role=str(source.get("role") or "researcher"),
                source_assignment_id=str(source["assignment_id"]),
                request=_generic_review_task(source),
                reason="assignment explicitly requested scientific review",
            )
        current = handoffs.get(handoff["handoff_id"]) or handoff
        review_assignment = _handoff_assignment(ledger, current)
        if current.get("status") == "ASSIGNED" and _assignment_complete(review_assignment):
            current = handoffs.resolve(
                current["handoff_id"],
                assignment_id=review_assignment["assignment_id"],
                actor=str(review_assignment.get("worker") or "reviewer"),
                summary="generic assignment review completed; source disposition remains explicit",
            )
            applied.append({
                "action": "GENERIC_REVIEW",
                "source_assignment_id": source["assignment_id"],
                "handoff_id": current["handoff_id"],
                "result": "resolved",
            })
            continue
        if current.get("status") in {"OPEN", "ASSIGNED"}:
            routed = _dispatch_handoff(
                project,
                current,
                rows=rows,
                reserved=reserved,
                assign=assign,
                start=start,
                role_of=role_of,
            )
            applied.append({
                "action": "GENERIC_REVIEW",
                "source_assignment_id": source["assignment_id"],
                **routed,
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
    """Apply deterministic completion, opt-in dispatch, and durable handoff routing."""
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

    applied.extend(
        _apply_implementation_governance(
            project,
            rows=rows,
            reserved=reserved,
            assign=assign,
            start=start,
            role_of=role_of,
        )
    )
    applied.extend(
        _apply_upward_handoffs(
            project,
            rows=rows,
            reserved=reserved,
            assign=assign,
            start=start,
            role_of=role_of,
        )
    )
    applied.extend(
        _apply_generic_needs_review(
            project,
            decisions,
            rows=rows,
            reserved=reserved,
            assign=assign,
            start=start,
            role_of=role_of,
        )
    )
    return applied