"""Durable, conservative global-idle detector for autonomous research projects.

Detection is observation-only. A restricted Luna/medium suggestion may invoke
only an existing fail-closed recovery entrypoint; it never edits scientific state.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from danus.core._util import append_jsonl, read_jsonl, utc_now
from danus.execution import layout as L

_IDLE_SECONDS = 180.0
_ALLOWED = {"RECOVER_PREMATURE_PROPOSAL", "WAIT_FOR_OPERATOR"}


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
    return {
        "current_assignments": assignment_notes,
        "active_handoff_details": handoff_notes,
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
    """One model call; parse a tiny allowlist. No tools or autonomous code edits."""
    from .supervisor_llm import _run_luna

    prompt = (
        "You are a restricted Danus-R deadlock triage router (not a scientific "
        "decision-maker). All scientific workers have been idle at least 180s, "
        "but the project remains RUNNING. Read this bounded snapshot and choose "
        "only one JSON object with keys action, target_role, node_id, rationale. "
        "Allowed actions: RECOVER_PREMATURE_PROPOSAL only when a researcher "
        "NEEDS_REVIEW was escalated to main after saving a Research Proposal "
        "and a downstream Reviewer is already planned; WAIT_FOR_OPERATOR in "
        "all other cases. target_role must be researcher for recovery and empty "
        "for WAIT_FOR_OPERATOR. Do not assume you may override approvals, "
        "Plan version, budget, manual pauses, or any escalation. An independent "
        "deterministic validator decides whether to execute your suggestion. "
        "Return JSON only. SNAPSHOT:\n"
        + json.dumps(packet, ensure_ascii=False, sort_keys=True)
    )
    response = _run_luna(prompt, effort="medium")
    if response.get("status") != "completed" or int(response.get("tool_calls") or 0):
        return {"action": "WAIT_FOR_OPERATOR", "target_role": "", "node_id": "",
                "rationale": "restricted model failed or attempted a tool"}
    try:
        reply = json.loads(str(response.get("reply") or ""))
    except (ValueError, TypeError):
        reply = {}
    if not isinstance(reply, dict):
        reply = {}
    action = str(reply.get("action") or "")
    if action not in _ALLOWED:
        action = "WAIT_FOR_OPERATOR"
    role = str(reply.get("target_role") or "")
    node = str(reply.get("node_id") or "")
    if action == "RECOVER_PREMATURE_PROPOSAL" and (role != "researcher" or not node):
        action = "WAIT_FOR_OPERATOR"
    return {
        "action": action,
        "target_role": role if action != "WAIT_FOR_OPERATOR" else "",
        "node_id": node if action != "WAIT_FOR_OPERATOR" else "",
        "rationale": str(reply.get("rationale") or "")[:600],
    }


class DeadlockWatch:
    """In-memory 180-second clock backed by a durable, deduplicated incident log."""

    def __init__(self, project: str, *, idle_seconds: float = _IDLE_SECONDS) -> None:
        self.project = project
        self.root = L.project_dir(project)
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
            if existing is not None:
                if existing.get("signature") == signature:
                    return None
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

        # Model inference must run outside project-control locks.
        try:
            suggestion = _model_proposal(event["snapshot"])
        except Exception as exc:
            suggestion = {
                "action": "WAIT_FOR_OPERATOR", "target_role": "", "node_id": "",
                "rationale": f"restricted model call failed: {type(exc).__name__}: {exc}"[:600],
            }
        _write(self.root, {"event": "DEADLOCK_SUGGESTION", "deadlock_id": ident,
                           **suggestion, "model": "gpt-5.6-luna", "effort": "medium"})
        if suggestion["action"] == "RECOVER_PREMATURE_PROPOSAL":
            try:
                # A model-supplied node must identify the source of a blocked
                # Researcher Assignment before invoking even a guarded recovery.
                source_matches = [
                    a for a in event["snapshot"].get("current_assignments", {}).values()
                    if a.get("role") == "researcher"
                    and a.get("node_id") == suggestion["node_id"]
                    and a.get("state") == "BLOCKED"
                    and a.get("outcome") == "NEEDS_REVIEW"
                ]
                if len(source_matches) != 1:
                    raise ValueError("model-selected node has no unique blocked Researcher")
                from .autonomy import recover_premature_proposal_review
                result = recover_premature_proposal_review(
                    self.project, token_budget=token_budget,
                )
                if (result.get("result") != "assigned"
                    or result.get("node_id") != suggestion["node_id"]):
                    raise ValueError("validated recovery did not dispatch the proposed node")
                _write(self.root, {"event": "DEADLOCK_ACTION", "deadlock_id": ident,
                                   "action": suggestion["action"], "result": "assigned",
                                   "node_id": result["node_id"],
                                   "assignment_id": result.get("assignment_id")})
            except (ValueError, OSError, SystemExit) as exc:
                _write(self.root, {"event": "DEADLOCK_ACTION", "deadlock_id": ident,
                                   "action": suggestion["action"], "result": "rejected",
                                   "error": str(exc)[:600]})
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
