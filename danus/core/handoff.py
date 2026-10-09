"""Durable role-to-role handoff requests on the shared progress ledger."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._util import append_jsonl, read_jsonl, utc_now
from .plan import ResearchPlanGraph
from .progress import ProgressLedger


class HandoffRequests:
    """Append-only HandoffRequest state stored in ``ProgressLedger.path``."""

    KINDS = ("REVIEW", "REVISION", "RETHINK", "INTERPRET", "ESCALATE", "CLARIFY")
    SUBJECT_TYPES = ("PLAN_CHANGE", "IMPLEMENTATION_CONTRACT", "ASSIGNMENT", "PLAN_NODE")
    STATUSES = ("OPEN", "ASSIGNED", "RESOLVED", "STALE", "CANCELLED")
    TARGET_BY_KIND = {
        "REVIEW": "reviewer",
        "REVISION": "researcher",
        "RETHINK": "researcher",
        "INTERPRET": "researcher",
        "ESCALATE": "main",
    }
    _TERMINAL = {"RESOLVED", "STALE", "CANCELLED"}

    def __init__(self, progress: ProgressLedger) -> None:
        self.progress = progress
        self.root = progress.root
        self.path = progress.path

    def open(
        self,
        *,
        kind: str,
        plan_version: int,
        node_id: str,
        subject_type: str,
        subject_id: str,
        requested_by: str,
        source_role: str,
        request: str,
        reason: str,
        source_assignment_id: str = "",
        parent_handoff_id: str = "",
        evidence_refs: Optional[List[str]] = None,
        target_role: str = "",
    ) -> Dict[str, Any]:
        kind = str(kind).strip().upper()
        if kind not in self.KINDS:
            raise ValueError(f"kind must be one of {self.KINDS}")
        subject_type = str(subject_type).strip().upper()
        if subject_type not in self.SUBJECT_TYPES:
            raise ValueError(f"subject_type must be one of {self.SUBJECT_TYPES}")

        node_id = self.progress._required(node_id, "node_id")
        subject_id = self.progress._required(subject_id, "subject_id")
        requested_by = self.progress._required(requested_by, "requested_by")
        source_role = self.progress._required(source_role, "source_role").lower()
        request = self.progress._required(request, "request") + "\n"
        reason = self.progress._required(reason, "reason")
        source_assignment_id = str(source_assignment_id or "").strip()
        parent_handoff_id = str(parent_handoff_id or "").strip()
        refs = list(dict.fromkeys(
            str(value).strip() for value in (evidence_refs or []) if str(value).strip()
        ))

        plan = ResearchPlanGraph(self.root).snapshot()
        version = int(plan_version)
        if version != plan["plan_version"]:
            raise ValueError(
                f"stale plan_version: {version}; current is {plan['plan_version']}"
            )
        node = plan["nodes"].get(node_id)
        if node is None:
            raise ValueError(f"unknown plan node: {node_id}")
        if node["state"] != "ACTIVE":
            raise ValueError(f"plan node {node_id} is not ACTIVE")
        if parent_handoff_id and self.get(parent_handoff_id) is None:
            raise ValueError(f"unknown parent handoff: {parent_handoff_id}")

        if kind == "CLARIFY":
            target_role = str(target_role or "").strip().lower()
            if (source_role != "reviewer"
                or subject_type != "ASSIGNMENT"
                or target_role not in {"researcher", "experimenter"}
                or not subject or subject.get("role") != "reviewer"
                or subject.get("assignment_id") != source_assignment_id
                or subject.get("state") not in {"ASSIGNED", "RUNNING"}):
                raise ValueError("CLARIFY requires an active Reviewer source and a scientific target role")
        else:
            if target_role:
                raise ValueError("target_role is permitted only for CLARIFY")
            target_role = self.TARGET_BY_KIND[kind]
        request_sha256 = self.progress.task_sha256(request)
        identity = {
            "kind": kind,
            "plan_version": version,
            "node_id": node_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "source_assignment_id": source_assignment_id,
            "parent_handoff_id": parent_handoff_id,
            "requested_by": requested_by,
            "source_role": source_role,
            "target_role": target_role,
            "request_sha256": request_sha256,
            "reason": reason,
        }
        if refs:
            identity["evidence_refs"] = refs
        subject = self._subject(identity)
        if kind == "REVISION" and subject_type == "IMPLEMENTATION_CONTRACT" and subject and subject.get("status") == "STALE":
            identity["reauthorization"] = True
            identity["node_epoch"] = self.progress.reopen_epoch(node_id)
        if subject_type in {"PLAN_CHANGE", "ASSIGNMENT"} and subject and subject.get("review_epoch"):
            identity["review_epoch"] = subject["review_epoch"]
        if (subject_type == "ASSIGNMENT" and subject and kind in {"INTERPRET", "RETHINK", "ESCALATE", "CLARIFY"}
            and not parent_handoff_id and subject.get("state") in {"ASSIGNED", "RUNNING"}):
            identity["review_epoch"] = int(subject.get("review_epoch", 0)) + 1
        handoff_id = self.progress._content_id(identity)
        existing = self.get(handoff_id)
        if existing is not None:
            return existing

        now = utc_now()
        append_jsonl(
            self.path,
            {
                "event": "HANDOFF_OPENED",
                "handoff_id": handoff_id,
                **identity,
                "request": request,
                "at": now,
            },
        )
        return self.get(handoff_id) or {}

    def get(self, handoff_id: str) -> Optional[Dict[str, Any]]:
        record = self._replay().get(str(handoff_id).strip())
        return self._view(record) if record else None

    def all(self) -> List[Dict[str, Any]]:
        records = self._replay()
        return [self._view(records[key]) for key in sorted(records)]

    def _subject(self, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if record["subject_type"] == "ASSIGNMENT":
            return next((a for a in self.progress.assignments() if a["assignment_id"] == record["subject_id"]), None)
        if record["subject_type"] == "IMPLEMENTATION_CONTRACT":
            return self.progress.implementation_contract(record["subject_id"])
        if record["subject_type"] == "PLAN_CHANGE":
            graph = ResearchPlanGraph(self.root)
            if graph._change_path(record["subject_id"]).is_file():
                return graph.get_change(record["subject_id"])
        return None

    def _view(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Subject revocation takes precedence over handoff retry state."""
        subject = self._subject(record)
        if record.get("reauthorization"):
            if (not ResearchPlanGraph(self.root).node_compatible(record["node_id"], record["plan_version"])
                or record.get("node_epoch") != self.progress.reopen_epoch(record["node_id"])):
                return {**record, "status": "STALE"}
            return record
        if subject is None:
            if (record["subject_type"] in {"PLAN_CHANGE", "IMPLEMENTATION_CONTRACT"}
                and record["status"] in {"OPEN", "ASSIGNED"}):
                return {**record, "status": "STALE"}
            return record
        old_epoch = (record["subject_type"] in {"PLAN_CHANGE", "ASSIGNMENT"}
                     and int(record.get("review_epoch", 0)) != int(subject.get("review_epoch", 0)))
        if (record["subject_type"] == "ASSIGNMENT" and record["kind"] in {"INTERPRET", "RETHINK", "ESCALATE", "CLARIFY"}
            and not record.get("parent_handoff_id") and subject.get("state") in {"ASSIGNED", "RUNNING", "DONE"}
            and int(record.get("review_epoch", 0)) == int(subject.get("review_epoch", 0)) + 1):
            old_epoch = False
        if old_epoch or (subject.get("status") == "STALE" and record["status"] in {"OPEN", "ASSIGNED"}):
            return {**record, "status": "STALE"}
        return record

    def pending(self) -> List[Dict[str, Any]]:
        return [
            record
            for record in self.all()
            if record["status"] in {"OPEN", "ASSIGNED"}
        ]

    def reopen(
        self,
        handoff_id: str,
        *,
        assignment_id: str,
        actor: str,
        reason: str,
    ) -> Dict[str, Any]:
        current = self._require(handoff_id)
        assignment_id = self.progress._required(assignment_id, "assignment_id")
        actor = self.progress._required(actor, "actor")
        reason = self.progress._required(reason, "reason")
        if current["status"] == "OPEN":
            return current
        self._expect(current, "ASSIGNED")
        if current.get("assigned_assignment_id") != assignment_id:
            raise ValueError("reopen assignment does not match current handoff assignment")
        self._bound_assignment(current["handoff_id"], assignment_id)
        append_jsonl(
            self.path,
            {
                "event": "HANDOFF_REOPENED",
                "handoff_id": current["handoff_id"],
                "assignment_id": assignment_id,
                "actor": actor,
                "reason": reason,
                "at": utc_now(),
            },
        )
        return self._require(handoff_id)

    def resolve(
        self,
        handoff_id: str,
        *,
        assignment_id: str,
        actor: str,
        summary: str,
    ) -> Dict[str, Any]:
        current = self._require(handoff_id)
        assignment_id = self.progress._required(assignment_id, "assignment_id")
        actor = self.progress._required(actor, "actor")
        summary = self.progress._required(summary, "summary")
        if current["status"] == "RESOLVED":
            if current.get("resolution_assignment_id") == assignment_id:
                return current
            raise ValueError(f"handoff {handoff_id} is already RESOLVED")
        self._expect(current, "ASSIGNED")
        if current.get("assigned_assignment_id") != assignment_id:
            raise ValueError("resolution assignment does not match current handoff assignment")
        self._bound_assignment(current["handoff_id"], assignment_id)
        append_jsonl(
            self.path,
            {
                "event": "HANDOFF_RESOLVED",
                "handoff_id": current["handoff_id"],
                "assignment_id": assignment_id,
                "actor": actor,
                "summary": summary,
                "at": utc_now(),
            },
        )
        return self._require(handoff_id)

    def stale(self, handoff_id: str, *, actor: str = "system", reason: str) -> Dict[str, Any]:
        return self._terminate(handoff_id, status="STALE", actor=actor, reason=reason)

    def cancel(self, handoff_id: str, *, actor: str, reason: str) -> Dict[str, Any]:
        return self._terminate(handoff_id, status="CANCELLED", actor=actor, reason=reason)

    def _terminate(self, handoff_id: str, *, status: str, actor: str, reason: str) -> Dict[str, Any]:
        current = self._require(handoff_id)
        actor = self.progress._required(actor, "actor")
        reason = self.progress._required(reason, "reason")
        if current["status"] == status:
            return current
        if current["status"] in self._TERMINAL:
            raise ValueError(f"handoff {handoff_id} is already {current['status']}")
        append_jsonl(
            self.path,
            {
                "event": f"HANDOFF_{status}",
                "handoff_id": current["handoff_id"],
                "actor": actor,
                "reason": reason,
                "at": utc_now(),
            },
        )
        return self._require(handoff_id)

    def _bound_assignment(self, handoff_id: str, assignment_id: str) -> Dict[str, Any]:
        for record in self.progress.assignments():
            if record["assignment_id"] != assignment_id:
                continue
            if record.get("handoff_id") != handoff_id:
                raise ValueError(f"assignment {assignment_id} is not bound to handoff {handoff_id}")
            return record
        raise ValueError(f"unknown assignment: {assignment_id}")

    def _require(self, handoff_id: str) -> Dict[str, Any]:
        handoff_id = self.progress._required(handoff_id, "handoff_id")
        current = self.get(handoff_id)
        if current is None:
            raise ValueError(f"unknown handoff: {handoff_id}")
        return current

    @staticmethod
    def _expect(current: Dict[str, Any], expected: str) -> None:
        if current["status"] != expected:
            raise ValueError(
                f"handoff {current['handoff_id']} is {current['status']}; expected {expected}"
            )

    def _replay(self) -> Dict[str, Dict[str, Any]]:
        records: Dict[str, Dict[str, Any]] = {}
        for event in read_jsonl(self.path):
            kind = str(event.get("event") or "")
            handoff_id = str(event.get("handoff_id") or "")
            if kind == "HANDOFF_OPENED":
                if not handoff_id or handoff_id in records:
                    continue
                record = dict(event)
                record.pop("event", None)
                record["status"] = "OPEN"
                record["attempt"] = 0
                record["created_at"] = event.get("at")
                record["updated_at"] = event.get("at")
                records[handoff_id] = record
                continue

            current = records.get(handoff_id)
            if current is None:
                continue
            if kind == "ASSIGNED" and current["status"] == "OPEN":
                current["status"] = "ASSIGNED"
                current["attempt"] = int(current["attempt"]) + 1
                current["assigned_assignment_id"] = str(event.get("assignment_id") or "")
                current["assigned_at"] = event.get("at")
            elif (
                kind == "HANDOFF_REOPENED"
                and current["status"] == "ASSIGNED"
                and current.get("assigned_assignment_id") == str(event.get("assignment_id") or "")
            ):
                current["status"] = "OPEN"
                current["assigned_assignment_id"] = None
                current["reopened_at"] = event.get("at")
                current["reopen_reason"] = str(event.get("reason") or "")
            elif (
                kind == "HANDOFF_RESOLVED"
                and current["status"] == "ASSIGNED"
                and current.get("assigned_assignment_id") == str(event.get("assignment_id") or "")
            ):
                current["status"] = "RESOLVED"
                current["resolution_assignment_id"] = str(event.get("assignment_id") or "")
                current["resolution_summary"] = str(event.get("summary") or "")
                current["resolved_at"] = event.get("at")
            elif kind in {"HANDOFF_STALE", "HANDOFF_CANCELLED"} and current["status"] in {"OPEN", "ASSIGNED"}:
                current["status"] = "STALE" if kind == "HANDOFF_STALE" else "CANCELLED"
                current["terminal_reason"] = str(event.get("reason") or "")
                current["terminal_at"] = event.get("at")
            else:
                continue
            current["updated_at"] = event.get("at")
            current["actor"] = event.get("actor")
        return records
