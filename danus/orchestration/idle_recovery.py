"""Role-first recovery of an idle scientific Plan, without deadlock-type whitelists.

The restricted model selects researcher/reviewer/experimenter and a Plan node.
This module interprets that role preference through existing frozen tasks,
ProgressLedger.assign, and HandoffRequests; it never approves any result.
"""
from __future__ import annotations

from typing import Any, Dict

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.execution import layout as L

_ROLES = {"researcher", "reviewer", "experimenter"}
_CANCEL_REASON = "Supervisor idle-watchdog re-routed an unresolved main escalation"


def dispatch_role_choice(
    project: str, *, target_role: str, node_id: str, reason: str,
    token_budget: int = 0,
) -> Dict[str, Any]:
    """Attempt one governed role route, or explain why no legal route exists.

    Model-supplied rationale is diagnostic only, never an authorization token.
    Scientific work and governance events remain append-only.
    """
    from . import autonomy as A
    from . import cli as CLI
    from .deadlock_watch import _eligible

    role = str(target_role or "").lower()
    node_id = str(node_id or "")
    if role not in _ROLES or not node_id:
        raise ValueError("model must select one of three roles and a concrete Plan node")
    root = L.project_dir(project)
    with CLI.project_control_lock(root):
        rows = CLI._project_rows(project)
        report = CLI._do_supervise_unlocked(
            project, apply=False, token_budget=token_budget,
            trigger="watchdog_role_validation",
        )
        if not _eligible(root, report, rows):
            raise ValueError("project is no longer eligible for an idle recovery")
        plan = ResearchPlanGraph(root).snapshot()
        node = plan["nodes"].get(node_id)
        if node is None or node.get("state") != "ACTIVE":
            raise ValueError("selected Plan node is not active")
        done = set(ProgressLedger(root).completed_nodes())
        if node_id in done or any(dep not in done for dep in node.get("depends_on") or []):
            raise ValueError("selected node is completed or has unfinished dependencies")
        ledger = ProgressLedger(root)
        blocked = [
            a for a in ledger.snapshot(refresh_stale=True)
            if a.get("node_id") == node_id and a.get("state") == "BLOCKED"
            and not ledger._is_plan_stale(a)
        ]
        if len(blocked) > 1:
            raise ValueError("ambiguous blocked Assignments on selected Plan node")

        if not blocked:
            # No free-form synthetic tasks. A ready, frozen Plan node can still
            # be reoffered to the designated role through the standard binder.
            if node_id not in report.get("frontier", []):
                raise ValueError("selected node is not on the ready Plan frontier")
            spec = A._task_spec(root, node_id)
            if spec.get("status") != "ready" or spec.get("role") != role:
                raise ValueError("no frozen Plan task is assigned to the selected role")
            if role == "experimenter":
                contract = ledger._active_implementation_contract(node_id)
                if (not contract
                    or str(contract.get("gate_status") or contract.get("status")) != "APPROVE"
                    or contract.get("task_sha256") != ProgressLedger.task_sha256(spec["task"])):
                    raise ValueError("Experimenter has no exact approved Implementation Contract")
            result = A.dispatch_explicit(
                project, role, spec["task"], node_id,
                rows=rows, assign=CLI.do_assign, start=CLI._start_one,
                role_of=CLI._read_worker_role,
                action="WATCHDOG_READY_PLAN_TASK",
            )
            if result.get("result") != "assigned":
                raise ValueError("ready-node dispatch rejected: " + str(result))
            return result

        source = blocked[0]
        source_id = str(source["assignment_id"])
        source_role = str(source.get("role") or "").lower()
        handoffs = HandoffRequests(ledger)
        pending = [
            h for h in handoffs.all()
            if h.get("subject_type") == "ASSIGNMENT"
            and h.get("subject_id") == source_id
            and h.get("status") in {"OPEN", "ASSIGNED"}
        ]
        # A pending scientific handoff toward the same model-selected role
        # already constitutes lawful work. Reuse it; never duplicate it.
        matching = [
            h for h in pending
            if h.get("target_role") == role and h.get("kind") != "ESCALATE"
        ]
        if matching:
            if len(matching) != 1 or len(pending) != 1:
                raise ValueError("multiple unresolved routes prevent unique dispatch")
            result = A._dispatch_handoff(
                project, matching[0], rows=rows, reserved=set(),
                assign=CLI.do_assign, start=CLI._start_one,
                role_of=CLI._read_worker_role,
            )
            if result.get("result") not in {"assigned", "existing"}:
                raise ValueError("pending handoff could not dispatch: " + str(result))
            return {"action": "WATCHDOG_EXISTING_HANDOFF",
                    "source_assignment_id": source_id, "node_id": node_id, **result}
        if any(h.get("kind") != "ESCALATE" or h.get("target_role") != "main"
               or h.get("status") != "OPEN" for h in pending):
            raise ValueError("unrelated unresolved Handoff prevents automatic rerouting")
        if len(pending) > 1:
            raise ValueError("multiple pending main escalations prevent a unique recovery")

        if role == source_role:
            # Retry only the unchanged, versioned task of the same scientific
            # role; a model cannot author an expanded task or change protocols.
            spec = A._task_spec(root, node_id)
            if (spec.get("status") != "ready" or spec.get("role") != role
                or ProgressLedger.task_sha256(spec["task"]) != source.get("task_sha256")):
                raise ValueError("frozen task differs from blocked source Assignment")
            wl = L.WorkerLayout(L.worker_dir(project, source["worker"]))
            row = next((r for r in rows if r.get("worker") == source["worker"]), None)
            pause = CLI._pause_reason(wl)
            if (not row or row.get("alive")
                or pause not in {None, "review:assignment_needs_review",
                                 "review:assignment_blocked"}):
                raise ValueError("source Worker is active or has an unrelated pause")
            if role == "experimenter":
                contract = ledger._active_implementation_contract(node_id)
                if (not contract
                    or str(contract.get("gate_status") or contract.get("status")) != "APPROVE"
                    or contract.get("task_sha256") != source.get("task_sha256")):
                    raise ValueError("retrying Experimenter requires an approved unchanged contract")
            if pending:
                handoffs.cancel(pending[0]["handoff_id"], actor="supervisor",
                                reason=_CANCEL_REASON)
            if pause:
                wl.pause.unlink()
                rows = CLI._project_rows(project)
            result = A.dispatch_explicit(
                project, role, spec["task"], node_id,
                rows=rows, assign=CLI.do_assign, start=CLI._start_one,
                role_of=CLI._read_worker_role,
                action="WATCHDOG_RETRY_FROZEN_TASK",
                retry_worker=source["worker"], retry_node=node_id,
                retry_outcome=source.get("outcome") or "BLOCKED",
            )
            if result.get("result") != "assigned":
                raise ValueError("frozen task retry failed: " + str(result))
            return {**result, "source_assignment_id": source_id}

        # Different-role routing uses the existing role-governed Handoff
        # protocol. An Experimenter clarification is read-only: experiment_run
        # already refuses execution under a CLARIFY Assignment.
        if role == "reviewer" and source_role != "reviewer":
            kind = "REVIEW"
            instruction = ("Independently diagnose the precise unresolved scientific "
                           "or engineering blocker. Do not approve an Implementation "
                           "Contract, complete this Plan node, or run experiments.")
        elif role == "researcher" and source_role in {"reviewer", "experimenter"}:
            kind = "RETHINK"
            instruction = ("Investigate and answer the bounded problem using current "
                           "evidence. Do not approve science or run new experiments.")
        elif role == "experimenter" and source_role == "reviewer":
            kind = "CLARIFY"
            instruction = ("Answer the Reviewer's precise missing-evidence question "
                           "from existing reports. Do not run or modify experiments.")
        else:
            raise ValueError(
                f"no authorized {role} handoff from blocked {source_role} node {node_id}"
            )

        request = (
            "# Supervisor idle recovery role handoff\n\n"
            f"Source Assignment: {source_id}\n"
            f"Node: {node_id}; originating role: {source_role}\n"
            f"Diagnostic rationale (not an authorization): {str(reason or '')[:500]}\n\n"
            f"Source terminal result:\n{str(source.get('result') or '')[:2000]}\n\n"
            f"{instruction}\n"
            "Use the existing tool permissions and report an explicit, bounded answer. "
            "End COMPLETE when this question has been addressed.\n"
        )
        if pending:
            handoffs.cancel(pending[0]["handoff_id"], actor="supervisor",
                            reason=_CANCEL_REASON)
        h = handoffs.open(
            kind=kind, plan_version=plan["plan_version"],
            node_id=node_id, subject_type="ASSIGNMENT",
            subject_id=source_id, source_assignment_id=source_id,
            requested_by="supervisor", source_role=source_role,
            target_role=(role if kind == "CLARIFY" else ""),
            reason=f"Watchdog delegated a bounded {role} answer",
            request=request,
        )
        result = A._dispatch_handoff(
            project, h, rows=rows, reserved=set(),
            assign=CLI.do_assign, start=CLI._start_one,
            role_of=CLI._read_worker_role,
        )
        if result.get("result") not in {"assigned", "existing", "no_idle_worker"}:
            raise ValueError("role handoff dispatch failed: " + str(result))
        return {
            "action": "WATCHDOG_ROLE_HANDOFF",
            "source_assignment_id": source_id,
            "node_id": node_id,
            "role": role,
            "handoff_id": h["handoff_id"],
            "result": ("queued" if result.get("result") == "no_idle_worker"
                       else result.get("result")),
            "dispatch": result,
        }
