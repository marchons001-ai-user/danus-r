"""Append-only runtime progress for Research Plan assignments and nodes.

The Research Plan stores desired scientific direction. This ledger stores mutable
execution state without revising that plan. Assignments bind a worker task to one
plan node and the exact plan version under which it was issued. Node completion is
also progress state: only the Supervisor/control plane should close or reopen a
plan node after deciding that its scientific objective is actually satisfied.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._util import append_jsonl, read_jsonl, utc_now
from .plan import ResearchPlanGraph


class ProgressLedger:
    """Project-shared append-only assignment/progress ledger."""

    STATES = ("ASSIGNED", "RUNNING", "BLOCKED", "DONE", "STALE")
    UPDATE_STATES = ("RUNNING", "BLOCKED", "DONE")
    NODE_STATES = ("OPEN", "DONE")
    TERMINAL_OUTCOMES = ("COMPLETE", "BLOCKED", "NEEDS_REVIEW")
    RUNTIME_EVENTS = ("STARTED", "EXITED")
    IMPLEMENTATION_VERDICTS = ("APPROVE", "REVISE", "REJECT")
    _TERMINAL = {"DONE", "STALE"}

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / "progress" / "ledger.jsonl"

    @staticmethod
    def task_sha256(task: str) -> str:
        return hashlib.sha256(str(task).encode("utf-8")).hexdigest()

    @staticmethod
    def implementation_task(
        *,
        goal: str,
        must_preserve_invariants: List[str],
        forbidden_behaviors: List[str],
        acceptance_conditions: List[str],
    ) -> str:
        """Render the exact implementation task that receives semantic review."""
        goal = ProgressLedger._required(goal, "goal")
        preserve = ProgressLedger._required_list(
            must_preserve_invariants, "must_preserve_invariants"
        )
        forbidden = ProgressLedger._required_list(
            forbidden_behaviors, "forbidden_behaviors"
        )
        acceptance = ProgressLedger._required_list(
            acceptance_conditions, "acceptance_conditions"
        )
        return (
            "# Implementation Contract\n\n"
            f"Goal:\n{goal}\n\n"
            "Must preserve:\n"
            + "".join(f"- {item}\n" for item in preserve)
            + "\nForbidden:\n"
            + "".join(f"- {item}\n" for item in forbidden)
            + "\nAcceptance:\n"
            + "".join(f"- {item}\n" for item in acceptance)
        )

    def submit_implementation_contract(
        self,
        *,
        proposer: str,
        node_id: str,
        plan_version: int,
        goal: str,
        must_preserve_invariants: List[str],
        forbidden_behaviors: List[str],
        acceptance_conditions: List[str],
    ) -> Dict[str, Any]:
        """Record one material implementation contract without assigning code work."""
        proposer = self._required(proposer, "proposer")
        node_id = self._required(node_id, "node_id")
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

        preserve = self._required_list(
            must_preserve_invariants, "must_preserve_invariants"
        )
        forbidden = self._required_list(forbidden_behaviors, "forbidden_behaviors")
        acceptance = self._required_list(
            acceptance_conditions, "acceptance_conditions"
        )
        task = self.implementation_task(
            goal=goal,
            must_preserve_invariants=preserve,
            forbidden_behaviors=forbidden,
            acceptance_conditions=acceptance,
        )
        submitted_at = utc_now()
        contract_id = self._content_id(
            {
                "node_id": node_id,
                "plan_version": version,
                "task_sha256": self.task_sha256(task),
                "proposer": proposer,
                "submitted_at": submitted_at,
            }
        )
        event = {
            "event": "IMPLEMENTATION_CONTRACT",
            "contract_id": contract_id,
            "node_id": node_id,
            "plan_version": version,
            "task_sha256": self.task_sha256(task),
            "task": task,
            "goal": self._required(goal, "goal"),
            "must_preserve_invariants": preserve,
            "forbidden_behaviors": forbidden,
            "acceptance_conditions": acceptance,
            "proposer": proposer,
            "at": submitted_at,
        }
        append_jsonl(self.path, event)
        return self.implementation_contract(contract_id) or event

    def review_implementation_contract(
        self,
        contract_id: str,
        *,
        reviewer: str,
        verdict: str,
        rationale: str,
    ) -> Dict[str, Any]:
        """Record the Reviewer's one-shot semantic verdict for a contract."""
        contract_id = self._required(contract_id, "contract_id")
        reviewer = self._required(reviewer, "reviewer")
        verdict = str(verdict).strip().upper()
        if verdict not in self.IMPLEMENTATION_VERDICTS:
            raise ValueError(f"verdict must be one of {self.IMPLEMENTATION_VERDICTS}")
        rationale = self._required(rationale, "rationale")
        current = self.implementation_contract(contract_id)
        if current is None:
            raise ValueError(f"unknown implementation contract: {contract_id}")
        if current["status"] != "PROPOSED":
            raise ValueError(
                f"implementation contract {contract_id} is {current['status']}; expected PROPOSED"
            )
        append_jsonl(
            self.path,
            {
                "event": "IMPLEMENTATION_REVIEW",
                "contract_id": contract_id,
                "reviewer": reviewer,
                "verdict": verdict,
                "rationale": rationale,
                "at": utc_now(),
            },
        )
        return self.implementation_contract(contract_id) or current

    def implementation_contract(self, contract_id: str) -> Optional[Dict[str, Any]]:
        records, _ = self._replay_implementation_contracts()
        record = records.get(str(contract_id).strip())
        return self._implementation_contract_view(record) if record else None

    def pending_implementation_contracts(self) -> List[Dict[str, Any]]:
        """Return the latest unconsumed semantic gate per plan node."""
        records, order = self._replay_implementation_contracts()
        latest_by_node: Dict[str, str] = {}
        for contract_id in order:
            record = records.get(contract_id)
            if record:
                latest_by_node[record["node_id"]] = contract_id
        out: List[Dict[str, Any]] = []
        for node_id in sorted(latest_by_node):
            record = records[latest_by_node[node_id]]
            view = self._implementation_contract_view(record)
            if view["status"] != "CONSUMED":
                out.append(view)
        return out

    def _active_implementation_contract(self, node_id: str) -> Optional[Dict[str, Any]]:
        for record in self.pending_implementation_contracts():
            if record["node_id"] == node_id:
                return record
        return None

    def assign(
        self,
        *,
        worker: str,
        role: str,
        node_id: str,
        plan_version: int,
        task: str,
    ) -> Dict[str, Any]:
        """Atomically bind one worker task to one ACTIVE Plan node/role lane."""
        worker = self._required(worker, "worker")
        role = self._required(role, "role")
        node_id = self._required(node_id, "node_id")
        if not str(task).strip():
            raise ValueError("task is required")

        lock_path = self.root / "progress" / ".assign.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
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

                implementation_contract = (
                    self._active_implementation_contract(node_id)
                    if role.lower() == "experimenter"
                    else None
                )
                if implementation_contract is not None:
                    status = implementation_contract["status"]
                    if status != "APPROVE":
                        raise ValueError(
                            f"implementation contract {implementation_contract['contract_id']} "
                            f"requires APPROVE; current status is {status}"
                        )
                    if self.task_sha256(task) != implementation_contract["task_sha256"]:
                        raise ValueError("assigned task does not match approved implementation contract")

                assignments, _ = self._replay_assignments()
                for record in assignments.values():
                    if record["state"] in self._TERMINAL:
                        continue
                    if (
                        record["node_id"] == node_id
                        and str(record.get("role") or "").lower() == role.lower()
                        and record["worker"] != worker
                    ):
                        raise ValueError(
                            f"plan node {node_id} role {role} already has active assignment "
                            f"{record['assignment_id']} on worker {record['worker']}"
                        )

                # A worker may be deliberately rebound. Serialize that transition
                # and stale every surviving nonterminal assignment from any legacy
                # race before creating the new one.
                for record in assignments.values():
                    if record["worker"] == worker and record["state"] not in self._TERMINAL:
                        self._append_state(
                            record,
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
                if implementation_contract is not None:
                    identity["implementation_contract_id"] = implementation_contract["contract_id"]
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
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def update(
        self,
        *,
        worker: str,
        state: str,
        actor: str,
        note: str = "",
        outcome: str = "",
    ) -> Dict[str, Any]:
        """Update one worker's current assignment without touching Research Plan.

        ``outcome`` is optional runtime provenance. COMPLETE maps to DONE while
        BLOCKED/NEEDS_REVIEW map to BLOCKED; the exact semantic outcome remains in
        the ledger so the Supervisor can distinguish a scientific review request
        from an external blocker without adding another assignment state.
        """
        worker = self._required(worker, "worker")
        state = str(state).strip().upper()
        if state not in self.UPDATE_STATES:
            raise ValueError(f"state must be one of {self.UPDATE_STATES}")
        actor = self._required(actor, "actor")
        outcome = str(outcome or "").strip().upper()
        if outcome:
            if outcome not in self.TERMINAL_OUTCOMES:
                raise ValueError(f"outcome must be one of {self.TERMINAL_OUTCOMES}")
            expected = "DONE" if outcome == "COMPLETE" else "BLOCKED"
            if state != expected:
                raise ValueError(f"outcome {outcome} requires state {expected}")

        current = self.current(worker, refresh_stale=True)
        if current is None:
            raise ValueError(f"worker {worker} has no assignment")
        if current["state"] == "STALE":
            raise ValueError(f"assignment {current['assignment_id']} is STALE")
        if current["state"] == "DONE":
            if state == "DONE":
                return current
            raise ValueError(f"assignment {current['assignment_id']} is already DONE")
        if current["state"] == state and (
            not outcome or current.get("outcome") == outcome
        ):
            return current

        self._append_state(
            current,
            state=state,
            actor=actor,
            note=str(note).strip(),
            outcome=outcome,
        )
        return self.current(worker, refresh_stale=False) or current

    def runtime_event(
        self,
        *,
        worker: str,
        kind: str,
        note: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Append operational worker lifecycle provenance without changing state."""
        worker = self._required(worker, "worker")
        kind = str(kind).strip().upper()
        if kind not in self.RUNTIME_EVENTS:
            raise ValueError(f"runtime event must be one of {self.RUNTIME_EVENTS}")
        current = self.current(worker, refresh_stale=True)
        event: Dict[str, Any] = {
            "event": "RUNTIME",
            "kind": kind,
            "worker": worker,
            "assignment_id": current.get("assignment_id") if current else None,
            "actor": "runtime",
            "note": str(note).strip(),
            "at": utc_now(),
        }
        if details:
            event["details"] = dict(details)
        append_jsonl(self.path, event)
        return event

    def set_node_state(
        self,
        node_id: str,
        *,
        state: str,
        actor: str,
        note: str = "",
    ) -> Dict[str, Any]:
        """Supervisor-only semantic closure/reopen of one plan node.

        This is progress state, not a Research Plan revision. The event is bound to
        the current plan version; if the node later changes or is retired, the old
        completion no longer satisfies dependencies.
        """
        node_id = self._required(node_id, "node_id")
        state = str(state).strip().upper()
        if state not in self.NODE_STATES:
            raise ValueError(f"node state must be one of {self.NODE_STATES}")
        actor = self._required(actor, "actor")

        plan = ResearchPlanGraph(self.root)
        current = plan.snapshot()
        node = current["nodes"].get(node_id)
        if node is None:
            raise ValueError(f"unknown plan node: {node_id}")
        if node["state"] != "ACTIVE":
            raise ValueError(f"plan node {node_id} is not ACTIVE")

        existing = self.node_states().get(node_id)
        if existing and existing["state"] == state:
            return existing
        event = {
            "event": "NODE_STATE",
            "node_id": node_id,
            "plan_version": current["plan_version"],
            "state": state,
            "actor": actor,
            "note": str(note).strip(),
            "at": utc_now(),
        }
        append_jsonl(self.path, event)
        return event

    def node_states(self) -> Dict[str, Dict[str, Any]]:
        """Return latest valid OPEN/DONE event per node under the current Plan."""
        latest: Dict[str, Dict[str, Any]] = {}
        for event in read_jsonl(self.path):
            if event.get("event") != "NODE_STATE":
                continue
            node_id = str(event.get("node_id") or "")
            state = str(event.get("state") or "").upper()
            if node_id and state in self.NODE_STATES:
                latest[node_id] = dict(event)

        plan = ResearchPlanGraph(self.root)
        current = plan.snapshot()
        valid: Dict[str, Dict[str, Any]] = {}
        for node_id, event in latest.items():
            try:
                version = int(event.get("plan_version"))
                old = plan.revision(version)
            except (TypeError, ValueError):
                continue
            old_node = old["nodes"].get(node_id)
            new_node = current["nodes"].get(node_id)
            if (
                old_node is not None
                and new_node is not None
                and new_node["state"] == "ACTIVE"
                and old_node == new_node
            ):
                valid[node_id] = event
        return valid

    def completed_nodes(self) -> List[str]:
        return sorted(
            node_id
            for node_id, event in self.node_states().items()
            if event["state"] == "DONE"
        )

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

    def assignments(self) -> List[Dict[str, Any]]:
        """Latest state of every assignment, including historical workers/tasks."""
        assignments, _ = self._replay_assignments()
        return list(assignments.values())

    def _implementation_contract_view(self, record: Dict[str, Any]) -> Dict[str, Any]:
        view = dict(record)
        if view.get("consumed_by_assignment_id"):
            view["status"] = "CONSUMED"
        elif self._implementation_contract_stale(view):
            view["status"] = "STALE"
        else:
            view["status"] = str(view.get("verdict") or "PROPOSED")
        return view

    def _implementation_contract_stale(self, record: Dict[str, Any]) -> bool:
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

    def _replay_implementation_contracts(
        self,
    ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
        records: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for event in read_jsonl(self.path):
            kind = event.get("event")
            contract_id = str(event.get("contract_id") or "")
            if kind == "IMPLEMENTATION_CONTRACT":
                if not contract_id:
                    continue
                record = dict(event)
                record.pop("event", None)
                record["verdict"] = ""
                records[contract_id] = record
                order.append(contract_id)
            elif kind == "IMPLEMENTATION_REVIEW" and contract_id in records:
                record = records[contract_id]
                record["verdict"] = str(event.get("verdict") or "").upper()
                record["reviewer"] = str(event.get("reviewer") or "")
                record["rationale"] = str(event.get("rationale") or "")
                record["reviewed_at"] = event.get("at")
            elif kind == "ASSIGNED":
                linked = str(event.get("implementation_contract_id") or "")
                if linked in records:
                    records[linked]["consumed_by_assignment_id"] = event.get("assignment_id")
                    records[linked]["consumed_at"] = event.get("at")
        return records, order

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
        outcome: str = "",
    ) -> None:
        event: Dict[str, Any] = {
            "event": "STATE",
            "assignment_id": record["assignment_id"],
            "worker": record["worker"],
            "state": state,
            "actor": actor,
            "note": note,
            "at": utc_now(),
        }
        if outcome:
            event["outcome"] = outcome
        append_jsonl(self.path, event)

    def _replay_assignments(self) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
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
                implementation_contract_id = str(
                    event.get("implementation_contract_id") or ""
                )
                if implementation_contract_id:
                    record["implementation_contract_id"] = implementation_contract_id
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
                outcome = str(event.get("outcome") or "").upper()
                if outcome:
                    record["outcome"] = outcome
                else:
                    record.pop("outcome", None)
                record["updated_at"] = event.get("at")
                record["actor"] = event.get("actor")
                record["note"] = str(event.get("note") or "")
        return assignments, latest

    def _current_by_worker(self) -> Dict[str, Dict[str, Any]]:
        assignments, latest = self._replay_assignments()
        return {worker: assignments[aid] for worker, aid in latest.items() if aid in assignments}

    @staticmethod
    def _required(value: Any, field: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError(f"{field} is required")
        return text

    @staticmethod
    def _required_list(values: List[str], field: str) -> List[str]:
        out = [str(value).strip() for value in (values or []) if str(value).strip()]
        if not out:
            raise ValueError(f"{field} requires at least one item")
        return out

    @staticmethod
    def _content_id(payload: Dict[str, Any]) -> str:
        canon = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canon).hexdigest()[:16]
