"""Versioned Worker Result / Next Intent protocol.

An intent is a *request* backed by the current Assignment, never an
authorization to run a worker, approve a contract, or bypass Plan dependencies.
Gateway writes here under existing control+assignment locks. Supervisor only
considers terminal, matching and non-stale intents for queue ordering.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from danus.core._util import append_jsonl, read_jsonl, utc_now
from danus.core import ProgressLedger

KINDS = frozenset({
    "REVIEW_PLAN", "REVIEW_CONTRACT", "RUN_EXPERIMENT", "RUN_NODE",
    "REVISE", "INTERPRET_EVIDENCE", "WAIT", "FINISH",
})
OUTCOMES = frozenset({"COMPLETE", "BLOCKED", "NEEDS_REVIEW"})
_MAX_REFS = 16
_ID = re.compile(r"^[a-zA-Z0-9_.:-]{1,128}$")


def _validate(
    assignment: Dict[str, Any],
    outcome: str,
    next_intent: str,
    subject_id: str,
    artifact_refs: List[str],
) -> Dict[str, Any]:
    outcome = str(outcome).strip().upper()
    kind = str(next_intent).strip().upper()
    subject_id = str(subject_id or "").strip()
    if outcome not in OUTCOMES:
        raise ValueError("invalid Worker Result outcome")
    if kind not in KINDS:
        raise ValueError("invalid Worker Result next_intent")
    if not isinstance(artifact_refs, list) or len(artifact_refs) > _MAX_REFS:
        raise ValueError("invalid Worker Result artifact_refs")
    if any(not isinstance(ref, str) or not _ID.fullmatch(ref) for ref in artifact_refs):
        raise ValueError("invalid Worker Result artifact reference")
    if kind in {"REVIEW_PLAN", "REVIEW_CONTRACT", "RUN_EXPERIMENT", "RUN_NODE"}:
        if not _ID.fullmatch(subject_id):
            raise ValueError("next_intent requires a safe subject_id")
    elif subject_id and not _ID.fullmatch(subject_id):
        raise ValueError("invalid next_intent subject_id")
    if kind == "FINISH" and outcome != "COMPLETE":
        raise ValueError("FINISH requires COMPLETE")
    return {
        "schema_version": 1, "event_type": "WORKER_RESULT",
        "assignment_id": assignment["assignment_id"],
        "plan_version": int(assignment["plan_version"]),
        "worker": assignment["worker"],
        "role": assignment["role"],
        "node_id": assignment["node_id"],
        "outcome": outcome,
        "next_intent": {"kind": kind, "subject_id": subject_id},
        "artifact_refs": list(dict.fromkeys(artifact_refs)),
    }


def _path(root: Path) -> Path:
    return Path(root) / "progress" / "worker_intents.jsonl"


def submit(
    root: Path, assignment: Dict[str, Any], *,
    outcome: str, next_intent: str, subject_id: str = "",
    artifact_refs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Write under the *caller's* existing control/assignment locks.

    Exactly one immutable record per Assignment; retries must be identical.
    This function never starts workers or changes the Progress Ledger.
    """
    record = _validate(assignment, outcome, next_intent, subject_id, artifact_refs or [])
    path = _path(root)
    for previous in read_jsonl(path):
        if previous.get("assignment_id") != record["assignment_id"]:
            continue
        existing = {key: previous.get(key) for key in record}
        if existing != record:
            raise ValueError("conflicting Worker Result for the same Assignment")
        return previous
    event = {**record, "at": utc_now()}
    append_jsonl(path, event)
    return event



def ensure_terminal_result(root: Path, assignment_id: str, outcome: str) -> Dict[str, Any]:
    """Trusted runtime bridge: each terminal Assignment gets an immutable JSON result.

    Existing model-supplied Next Intent wins; if missing, synthesize WAIT, not
    a guessed role or authorization. Called *after* ledger.update() succeeded.
    Lock ordering matches Gateway write paths, and a rebound worker is ignored.
    """
    from danus.orchestration.cli import project_control_lock

    root = Path(root)
    with project_control_lock(root):
        ledger = ProgressLedger(root)
        with ledger.assignment_lock():
            current = next((
                a for a in ledger.snapshot(refresh_stale=True)
                if a.get("assignment_id") == assignment_id
            ), None)
            if (
                current is None
                or current.get("state") not in {"DONE", "BLOCKED"}
                or current.get("outcome") != outcome
                or ledger._is_plan_stale(current)
            ):
                raise ValueError("terminal Worker Result does not match current Assignment")
            for previous in read_jsonl(_path(root)):
                if previous.get("assignment_id") != assignment_id:
                    continue
                if previous.get("outcome") != outcome:
                    raise ValueError("Worker Result outcome conflicts with durable terminal state")
                return previous
            return submit(root, current, outcome=outcome, next_intent="WAIT")



def terminal_intents(root: Path) -> List[Dict[str, Any]]:
    """Read advisory events whose *exact* Assignment is terminal and current.

    No event may advance Plan, Contract or Handoff state by itself.
    """
    ledger = ProgressLedger(root)
    assignments = {a["assignment_id"]: a for a in ledger.assignments()}
    current = {a["assignment_id"] for a in ledger.snapshot(refresh_stale=True)}
    accepted = []
    for item in read_jsonl(_path(root)):
        a = assignments.get(str(item.get("assignment_id") or ""))
        if (
            a is None
            or a["assignment_id"] not in current
            or a.get("state") != "DONE"
            or a.get("outcome") != item.get("outcome")
            or any(a.get(k) != item.get(k)
                   for k in ("worker", "role", "node_id", "plan_version"))
            or ledger._is_plan_stale(a)
        ):
            continue
        accepted.append(item)
    return accepted


def preference(root: Path, kind: str, subject_id: str) -> int:
    """Sort hint only. A hint can never create an otherwise invalid candidate."""
    if not subject_id:
        return 1
    for record in reversed(terminal_intents(root)):
        next_one = record["next_intent"]
        if (next_one.get("kind"), next_one.get("subject_id")) == (kind, subject_id):
            return 0
    return 1
