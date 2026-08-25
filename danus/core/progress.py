"""Append-only runtime progress for Research Plan assignments.

The Research Plan stores desired scientific direction. This ledger stores mutable
execution state without revising that plan. Assignments bind a worker task to one
plan node and the exact plan version under which it was issued.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._util import append_jsonl, read_jsonl, utc_now
from .plan import ResearchPlanGraph


class ProgressLedger:
    """Project-shared append-only assignment/progress ledger."""

    STATES = ("ASSIGNED", "RUNNING", "BLOCKED", "DONE", "STALE")
    UPDATE_STATES = ("RUNNING", "BLOCKED", "DONE")
    _TERMINAL = {"DONE", "STALE"}

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / "progress" / "ledger.jsonl"

    @staticmethod
    def task_sha256(task: str) -> str:
        return hashlib.sha256(str(task).encode("utf-8")).hexdigest()

    def assign(
        self,
        *,
        worker: str,
        role: str,
        node_id: str,
        plan_version: int,
        task: str,
    ) -> Dict[str, Any]:
        """Bind one task to an ACTIVE node in the named current plan version."""
        worker = self._required(worker, "worker")
        role = self._required(role, "role")
        node_id = self._required(node_id, "node_id")
        if not str(task).strip():
            raise ValueError("task is required")

        plan = ResearchPlanGraph(self.root)
        current = plan.snapshot()
        version = int(plan_version)
        if version != current["plan_version"]:
            raise ValueError(
                f"stale plan_version: {version}; current is {current['plan_version']}"
            )
        node = current["nodes"].get(node_id)
        if node is None:
            raise ValueError(f"unknown plan node: {node_id}")
        if node["state"] != "ACTIVE":
            raise ValueError(f"plan node {node_id} is not ACTIVE")

        previous = self.current(worker, refresh_stale=True)
        if previous and previous["state"] not in self._TERMINAL:
            self._append_state(
                previous,
                state="STALE",
                actor="system",
                note="superseded by a newer assignment",
            )

        assigned_at = utc_now()
        task_hash = self.task_sha256(task)
        identity = {
            "worker": worker,
            "role": role,
            "node_id": node_id,
            "plan_version": version,
            "task_sha256": task_hash,
            "assigned_at": assigned_at,
        }
        assignment_id = self._content_id(identity)
        event = {
            "event": "ASSIGNED",
            "assignment_id": assignment_id,
            **identity,
            "state": "ASSIGNED",
            "actor": "supervisor",
            "note": "",
            "at": assigned_at,
        }
        append_jsonl(self.path, event)
        return self.current(worker, refresh_stale=False) or event

    def update(
        self,
        *,
        worker: str,
        state: str,
        actor: str,
        note: str = "",
    ) -> Dict[str, Any]:
        """Update one worker's current assignment without touching Research Plan."""
        worker = self._required(worker, "worker")
        state = str(state).strip().upper()
        if state not in self.UPDATE_STATES:
            raise ValueError(f"state must be one of {self.UPDATE_STATES}")
        actor = self._required(actor, "actor")

        current = self.current(worker, refresh_stale=True)
        if current is None:
            raise ValueError(f"worker {worker} has no assignment")
        if current["state"] == "STALE":
            raise ValueError(f"assignment {current['assignment_id']} is STALE")
        if current["state"] == "DONE":
            if state == "DONE":
                return current
            raise ValueError(f"assignment {current['assignment_id']} is already DONE")
        if current["state"] == state:
            return current

        self._append_state(current, state=state, actor=actor, note=str(note).strip())
        return self.current(worker, refresh_stale=False) or current

    def current(
        self, worker: str, *, refresh_stale: bool = True
    ) -> Optional[Dict[str, Any]]:
        worker = self._required(worker, "worker")
        current = self._current_by_worker().get(worker)
        if current and refresh_stale and current["state"] not in self._TERMINAL:
            if self._is_plan_stale(current):
                self._append_state(
                    current,
                    state="STALE",
                    actor="system",
                    note="bound plan node changed or was retired",
                )
                current = self._current_by_worker().get(worker)
        return current

    def snapshot(self, *, refresh_stale: bool = True) -> List[Dict[str, Any]]:
        current = self._current_by_worker()
        if refresh_stale:
            changed = False
            for record in list(current.values()):
                if record["state"] not in self._TERMINAL and self._is_plan_stale(record):
                    self._append_state(
                        record,
                        state="STALE",
                        actor="system",
                        note="bound plan node changed or was retired",
                    )
                    changed = True
            if changed:
                current = self._current_by_worker()
        return [current[k] for k in sorted(current)]

    def _is_plan_stale(self, record: Dict[str, Any]) -> bool:
        plan = ResearchPlanGraph(self.root)
        current = plan.snapshot()
        if current["plan_version"] == record["plan_version"]:
            return False
        try:
            old = plan.revision(record["plan_version"])
        except ValueError:
            return True
        old_node = old["nodes"].get(record["node_id"])
        new_node = current["nodes"].get(record["node_id"])
        if old_node is None or new_node is None or new_node["state"] != "ACTIVE":
            return True
        return old_node != new_node

    def _append_state(
        self,
        record: Dict[str, Any],
        *,
        state: str,
        actor: str,
        note: str,
    ) -> None:
        append_jsonl(
            self.path,
            {
                "event": "STATE",
                "assignment_id": record["assignment_id"],
                "worker": record["worker"],
                "state": state,
                "actor": actor,
                "note": note,
                "at": utc_now(),
            },
        )

    def _current_by_worker(self) -> Dict[str, Dict[str, Any]]:
        assignments: Dict[str, Dict[str, Any]] = {}
        latest: Dict[str, str] = {}
        for event in read_jsonl(self.path):
            kind = event.get("event")
            aid = str(event.get("assignment_id") or "")
            if kind == "ASSIGNED":
                if not aid:
                    continue
                record = {
                    "assignment_id": aid,
                    "worker": str(event.get("worker") or ""),
                    "role": str(event.get("role") or ""),
                    "node_id": str(event.get("node_id") or ""),
                    "plan_version": event.get("plan_version"),
                    "task_sha256": str(event.get("task_sha256") or ""),
                    "state": "ASSIGNED",
                    "assigned_at": event.get("assigned_at") or event.get("at"),
                    "updated_at": event.get("at"),
                    "actor": event.get("actor"),
                    "note": str(event.get("note") or ""),
                }
                if not record["worker"] or not record["node_id"]:
                    continue
                assignments[aid] = record
                latest[record["worker"]] = aid
            elif kind == "STATE" and aid in assignments:
                state = str(event.get("state") or "").upper()
                if state not in self.STATES:
                    continue
                record = assignments[aid]
                record["state"] = state
                record["updated_at"] = event.get("at")
                record["actor"] = event.get("actor")
                record["note"] = str(event.get("note") or "")
        return {worker: assignments[aid] for worker, aid in latest.items() if aid in assignments}

    @staticmethod
    def _required(value: Any, field: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError(f"{field} is required")
        return text

    @staticmethod
    def _content_id(payload: Dict[str, Any]) -> str:
        canon = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canon).hexdigest()[:16]
