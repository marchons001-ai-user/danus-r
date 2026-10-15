"""Durable, conservative global-idle detector for autonomous research projects.

Detection is observation-only. A restricted Luna/medium suggestion may invoke
only an existing fail-closed recovery entrypoint; it never edits scientific state.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from danus.core._util import append_jsonl, read_jsonl, utc_now
from danus.execution import layout as L

_IDLE_SECONDS = 45.0
# Version the decision protocol, not just the incident. Existing incidents
# recorded by older routers may be re-triaged once by the current policy.
_DECISION_POLICY = "three-role-route-v1"


def _events_path(root: Path) -> Path:
    return root / "supervisor" / "deadlocks.jsonl"


def _history(root: Path) -> List[Dict[str, Any]]:
    return read_jsonl(_events_path(root))


def _fingerprint(report: Dict[str, Any], rows: List[Dict[str, Any]]) -> str:
    """Only scientific/control state, never heartbeat, wall-clock, or token counters."""
    snapshot = {
        "plan_version": report.get("plan_version"),
        "frontier": report.get("frontier", []),
        "completed_nodes": report.get("completed_nodes", []),
        "pending_handoffs": report.get("pending_handoffs", []),
        "unresolved_implementation_contracts": report.get("unresolved_implementation_contracts", []),
        "decisions": report.get("decisions", []),
        "workers": [
            {k: row.get(k) for k in (
                "worker", "assignment_id", "plan_node_id", "progress_state",
                "progress_outcome", "pause_reason",
            )}
            for row in sorted(rows, key=lambda r: str(r.get("worker")))
        ],
    }
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def _eligible(root: Path, report: Dict[str, Any], rows: List[Dict[str, Any]]) -> bool:
    budget = report.get("budget", {})
    if (report.get("project_state") != "RUNNING" or report.get("budget_stop")
        or budget.get("tokens", {}).get("exceeded") is True
        or budget.get("time", {}).get("exceeded")
        or (root / L.PAUSE_FILE).exists()
        or not rows or any(r.get("alive") for r in rows)):
        return False
    # Operator pauses are deliberate. Supervisor-owned review pauses can stall.
    return not any(
        r.get("pause_reason") and not str(r["pause_reason"]).startswith("review:")
        for r in rows
    )


def _packet(report: Dict[str, Any], rows: List[Dict[str, Any]], *, root: Optional[Path] = None) -> Dict[str, Any]:
    """A bounded diagnostic summary, not permission to mutate the research."""
    assignment_notes = {}
    handoff_notes = {}
    if root is not None:
        from danus.core import ProgressLedger
        from danus.core.handoff import HandoffRequests

        ledger = ProgressLedger(root)
        assignment_notes = {
            a["assignment_id"]: {
                "role": a.get("role"), "node_id": a.get("node_id"),
                "state": a.get("state"), "outcome": a.get("outcome"),
                "result": str(a.get("result") or "")[:1000],
            }
            for a in ledger.snapshot(refresh_stale=False)
            if a.get("assignment_id")
        }
        handoff_notes = {
            h["handoff_id"]: {
                "kind": h.get("kind"), "status": h.get("status"),
                "target_role": h.get("target_role"),
                "subject_id": h.get("subject_id"),
                "reason": str(h.get("reason") or "")[:400],
                "request": str(h.get("request") or "")[:1000],
            }
            for h in HandoffRequests(ledger).all()
            if h.get("status") in {"OPEN", "ASSIGNED"}
        }
    # Past dispatches are decision context, not merely postmortem logs.
    prior_attempts = []
    if root is not None:
        stage_version = report.get("plan_version")
        stage_completed = tuple(report.get("completed_nodes", []))
        prior_attempts = [
            {
                "at": e.get("at"), "target_role": e.get("target_role"),
                "node_id": e.get("node_id"), "result": e.get("result"),
                "assignment_id": e.get("assignment_id"),
                "handoff_id": e.get("handoff_id"),
            }
            for e in _history(root)
            if e.get("event") == "DEADLOCK_ACTION"
            and e.get("plan_version") == stage_version
            and tuple(e.get("completed_nodes", [])) == stage_completed
        ][-12:]
    return {
        "current_assignments": assignment_notes,
        "active_handoff_details": handoff_notes,
        "prior_recovery_attempts": prior_attempts,
        "plan_version": report.get("plan_version"),
        "frontier": report.get("frontier", []),
        "completed_nodes": report.get("completed_nodes", []),
        "pending_handoffs": report.get("pending_handoffs", []),
        "unresolved_implementation_contracts": report.get("unresolved_implementation_contracts", []),
        "decisions": report.get("decisions", [])[:12],
        "workers": [
            {k: r.get(k) for k in (
                "worker", "assignment_id", "plan_node_id", "progress_state",
                "progress_outcome", "pause_reason",
            )}
            for r in rows
        ],
        "budget": {
            "used": report.get("budget", {}).get("tokens", {}).get("used"),
            "limit": report.get("budget", {}).get("tokens", {}).get("limit"),
        },
    }


def _latest_open(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    open_events = {}
    for e in history:
        ident = str(e.get("deadlock_id") or "")
        if e.get("event") == "DEADLOCK_OPEN":
            open_events[ident] = e
        elif e.get("event") == "DEADLOCK_CLOSED":
            open_events.pop(ident, None)
    return list(open_events.values())[-1] if open_events else None


def _write(root: Path, event: Dict[str, Any]) -> None:
    append_jsonl(_events_path(root), {**event, "at": utc_now()})


def _model_proposal(packet: Dict[str, Any]) -> Dict[str, str]:
    """Luna Medium chooses one of the three scientific roles; no deadlock taxonomy."""
    from .supervisor_llm import _run_luna

    prompt = (
        "You are the Danus-R restricted Supervisor deadlock advisor. All "
        "scientific workers have been idle beyond the configured watchdog "
        "threshold while the project remains RUNNING. Choose which of the three scientific "
        "roles should act next. Do not classify the deadlock into pre-defined "
        "types; use this project's actual Plan, Assignments and Handoffs. "
        "Return ONLY one JSON object with exactly three keys: target_role "
        "(researcher, reviewer, or experimenter), node_id (one current Plan "
        "node), rationale (a specific explanation of what the role should "
        "resolve). You must select exactly one of the three roles. "
        "Your selection is NOT permission to bypass existing Plan dependencies, "
        "implementation approval, evidence integrity or role authorization. "
        "Examine prior_recovery_attempts and current_assignments: if the same "
        "role has already restarted this node in the same scientific stage "
        "and returned BLOCKED, choose a DIFFERENT role to resolve its actual "
        "prerequisite. Dispatch success is NOT scientific progress. "
        "If your selected route cannot legally execute, a deterministic "
        "control-plane validator will record its specific rejection; do not "
        "invent governance approvals. The workflow is not complete merely "
        "because you selected a role. SNAPSHOT:\\n"
        + json.dumps(packet, ensure_ascii=False, sort_keys=True)
    )
    response = _run_luna(prompt, effort="medium")
    if response.get("status") != "completed" or int(response.get("tool_calls") or 0):
        raise ValueError("restricted model unavailable or attempted a tool")
    try:
        reply = json.loads(str(response.get("reply") or ""))
    except (ValueError, TypeError) as exc:
        raise ValueError("restricted model returned invalid JSON") from exc
    if not isinstance(reply, dict):
        raise ValueError("restricted model returned a non-object")
    role = str(reply.get("target_role") or "").lower()
    if role not in {"researcher", "reviewer", "experimenter"}:
        raise ValueError("restricted model did not select one of the three roles")
    node = str(reply.get("node_id") or "")
    if not node or node not in {
        str(e.get("node_id") or "") for e in packet.get("current_assignments", {}).values()
    } | set(str(n) for n in packet.get("frontier", [])):
        raise ValueError("model node is not an active assignment or ready frontier node")
    return {
        "target_role": role,
        "node_id": node,
        "rationale": str(reply.get("rationale") or "")[:600],
    }


class DeadlockWatch:
    """In-memory configurable idle clock backed by a durable incident log."""

    def __init__(self, project: str, *, idle_seconds: Optional[float] = None) -> None:
        self.project = project
        self.root = L.project_dir(project)
        if idle_seconds is None:
            idle_seconds = float(os.environ.get(
                "DANUS_DEADLOCK_IDLE_SECONDS", str(_IDLE_SECONDS),
            ))
        if not (0.0 < idle_seconds < float("inf")):
            raise ValueError("deadlock idle threshold must be a finite positive number")
        self.idle_seconds = idle_seconds
        self._since: Optional[float] = None
        self._signature: Optional[str] = None
        self._inactive_checked = False

    def observe(
        self, report: Dict[str, Any], rows: List[Dict[str, Any]], *,
        token_budget: int = 0, now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        from . import cli as CLI

        tick = time.monotonic() if now is None else now
        signature = _fingerprint(report, rows) if _eligible(self.root, report, rows) else None
        if signature != self._signature:
            if self._signature is not None:
                self._close_inactive()
            self._signature, self._since = signature, tick
            self._inactive_checked = False
        if signature is None or self._since is None:
            if not self._inactive_checked:
                self._close_inactive()
                self._inactive_checked = True
            return None
        if tick - self._since < self.idle_seconds:
            return None

        # Atomic incident deduplication and last-second eligibility validation.
        with CLI.project_control_lock(self.root):
            actual_rows = CLI._project_rows(self.project)
            actual = CLI._do_supervise_unlocked(
                self.project, apply=False, token_budget=token_budget,
                trigger="deadlock_snapshot",
            )
            if not _eligible(self.root, actual, actual_rows):
                return None
            actual_sig = _fingerprint(actual, actual_rows)
            if actual_sig != signature:
                self._signature, self._since = actual_sig, tick
                return None
            history = _history(self.root)
            existing = _latest_open(history)
            if existing is not None and existing.get("signature") == signature:
                # A prior generation may have returned WAIT_FOR_OPERATOR.
                # Deduplicate each *policy version*, not every historical
                # suggestion across software upgrades.
                if any(e.get("deadlock_id") == existing["deadlock_id"]
                       and e.get("event") == "DEADLOCK_SUGGESTION"
                       and e.get("policy_version") == _DECISION_POLICY
                       for e in history):
                    return None
                event = {**existing, "snapshot": _packet(actual, actual_rows, root=self.root)}
                ident = existing["deadlock_id"]
            else:
                if existing is not None:
                    _write(self.root, {"event": "DEADLOCK_CLOSED",
                                       "deadlock_id": existing["deadlock_id"],
                                       "reason": "scientific_state_changed"})
                ident = hashlib.sha256(
                    (self.project + ":" + signature + ":" + utc_now()).encode()
                ).hexdigest()[:16]
                event = {
                    "event": "DEADLOCK_OPEN",
                    "deadlock_id": ident,
                    "signature": signature,
                    "idle_seconds": round(tick - self._since, 1),
                    "capture": "automatic",
                    "snapshot": _packet(actual, actual_rows, root=self.root),
                }
                _write(self.root, event)

        # Inference runs outside project-control locks. The model cannot
        # modify project state; any selected role passes deterministic gates.
        try:
            suggestion = _model_proposal(event["snapshot"])
        except Exception as exc:
            suggestion = {
                "target_role": "", "node_id": "",
                "rationale": f"model triage failed: {type(exc).__name__}: {exc}"[:600],
            }
        _write(self.root, {"event": "DEADLOCK_SUGGESTION", "deadlock_id": ident,
                           **suggestion, "model": "gpt-5.6-luna", "effort": "medium",
                           "policy_version": _DECISION_POLICY})
        try:
            role = suggestion["target_role"]
            node = suggestion["node_id"]
            if not role or not node:
                raise ValueError("no valid three-role model decision")

            # A recovered Assignment that again leaves the same scientific
            # stage idle is a failed recovery, even if dispatch succeeded.
            # Reject a second identical role/node choice before any mutation.
            completed = tuple(event["snapshot"].get("completed_nodes", []))
            plan_version = event["snapshot"].get("plan_version")
            repeated = [
                e for e in _history(self.root)
                if e.get("event") == "DEADLOCK_ACTION"
                and e.get("target_role") == role
                and e.get("node_id") == node
                and e.get("plan_version") == plan_version
                and tuple(e.get("completed_nodes", [])) == completed
                and e.get("result") == "assigned"
            ]
            if repeated:
                raise ValueError(
                    "role/node was already dispatched with no new Plan version "
                    "or completed-node progress; select a different scientific role"
                )

            from .idle_recovery import dispatch_role_choice
            routed = dispatch_role_choice(
                self.project, target_role=role, node_id=node,
                reason=suggestion["rationale"], token_budget=token_budget,
            )
            _write(self.root, {
                "event": "DEADLOCK_ACTION", "deadlock_id": ident,
                "target_role": role, "node_id": node,
                "result": routed["result"],
                "assignment_id": routed.get("assignment_id"),
                "handoff_id": routed.get("handoff_id"),
                "plan_version": plan_version, "completed_nodes": list(completed),
            })
        except (ValueError, OSError, SystemExit, PermissionError) as exc:
            _write(self.root, {
                "event": "DEADLOCK_ACTION", "deadlock_id": ident,
                "target_role": suggestion.get("target_role", ""),
                "node_id": suggestion.get("node_id", ""),
                "result": "rejected", "error": str(exc)[:600],
            })
        return event

    def _close_inactive(self) -> None:
        from . import cli as CLI

        with CLI.project_control_lock(self.root):
            existing = _latest_open(_history(self.root))
            if existing is not None:
                _write(self.root, {
                    "event": "DEADLOCK_CLOSED", "deadlock_id": existing["deadlock_id"],
                    "reason": "workers_restarted_or_project_state_changed",
                })


def capture_current_deadlock(project: str, *, token_budget: int = 0) -> Dict[str, Any]:
    """Explicit operator capture for a known incident before activating recovery."""
    from . import cli as CLI

    root = L.project_dir(project)
    with CLI.project_control_lock(root):
        rows = CLI._project_rows(project)
        report = CLI._do_supervise_unlocked(
            project, apply=False, token_budget=token_budget,
            trigger="deadlock_manual_capture",
        )
        if not _eligible(root, report, rows):
            raise ValueError("capture requires RUNNING, idle workers, no operator pause or budget stop")
        signature = _fingerprint(report, rows)
        previous = _latest_open(_history(root))
        if previous and previous.get("signature") == signature:
            return {**previous, "replayed": True}
        if previous:
            _write(root, {"event": "DEADLOCK_CLOSED",
                          "deadlock_id": previous["deadlock_id"],
                          "reason": "scientific_state_changed"})
        ident = hashlib.sha256(
            (project + ":" + signature + ":" + utc_now()).encode()
        ).hexdigest()[:16]
        event = {
            "event": "DEADLOCK_OPEN", "deadlock_id": ident,
            "signature": signature, "idle_seconds": None,
            "capture": "operator", "snapshot": _packet(report, rows, root=root),
        }
        _write(root, event)
        return event
