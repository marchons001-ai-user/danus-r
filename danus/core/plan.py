"""Versioned, progressively expanded project research-plan graph.

The plan stays coarse. Scientific roles may propose/review structural changes, but
only the control plane commits them. Every committed snapshot is stored immutably
under ``plan/revisions``; ``current.json`` must match its committed revision.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._util import utc_now
from .evidence import EvidenceStore


class ResearchPlanGraph:
    """One project-shared research plan with reviewed, versioned mutations."""

    SCHEMA_VERSION = 2
    MAX_DEPTH = 5
    MAX_TITLE_CHARS = 35
    MAX_SUMMARY_CHARS = 100
    PLAN_STATES = ("ACTIVE", "CANCELLED", "SUPERSEDED", "MERGED")
    _NODE_FIELDS = {"title", "summary", "parent", "depends_on", "state"}
    _TOP_FIELDS = {
        "schema_version", "plan_version", "committed_change_id", "root_id", "nodes"
    }
    REVIEW_VERDICTS = ("APPROVE", "REVISE", "REJECT")
    _CHANGE_STATUSES = {"PROPOSED", "APPROVED", "REVISE", "REJECTED", "STALE", "COMMITTED"}
    _OPS = {"add_node", "set_dependencies", "set_state"}

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.dir = self.root / "plan"
        self.path = self.dir / "current.json"
        self.revisions_dir = self.dir / "revisions"
        self.changes_dir = self.dir / "changes"

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        node_id: str,
        title: str,
        summary: str = "",
    ) -> "ResearchPlanGraph":
        graph = cls(root)
        if graph.path.exists():
            raise FileExistsError(f"research plan already exists: {graph.path}")
        nid = graph._clean_id(node_id)
        payload = {
            "schema_version": cls.SCHEMA_VERSION,
            "plan_version": 1,
            "committed_change_id": None,
            "root_id": nid,
            "nodes": {
                nid: {
                    "title": graph._clean_text(
                        title, "title", cls.MAX_TITLE_CHARS, required=True
                    ),
                    "summary": graph._clean_text(
                        summary, "summary", cls.MAX_SUMMARY_CHARS
                    ),
                    "parent": None,
                    "depends_on": [],
                    "state": "ACTIVE",
                }
            },
        }
        graph._validate(payload)
        graph._write_revision(payload)
        graph._write_current(payload)
        return graph

    def snapshot(self) -> Dict[str, Any]:
        """Return current plan and reject edits that bypass revision history."""
        payload = self._read_current()
        if payload.get("schema_version") == 1:
            payload = self._upgrade_v1(payload)
        self._validate(payload)
        revision = self._read_json(self._revision_path(payload["plan_version"]))
        if revision is None:
            raise ValueError(f"missing committed plan revision {payload['plan_version']}")
        if revision != payload:
            raise ValueError("current research plan diverges from committed revision")
        return payload

    def revision(self, plan_version: int) -> Dict[str, Any]:
        payload = self._read_json(self._revision_path(int(plan_version)))
        if payload is None:
            raise ValueError(f"unknown plan_version: {plan_version}")
        self._validate(payload)
        return payload

    def _required_post_hoc_refs(self, proposer: str) -> List[str]:
        """Evidence that the proposer's current scientific handoff may not drop."""
        from .handoff import HandoffRequests
        from .progress import ProgressLedger

        ledger = ProgressLedger(self.root)
        assignment = ledger.current(proposer, refresh_stale=False)
        if not assignment:
            return []
        handoff_id = str(assignment.get("handoff_id") or "")
        if not handoff_id:
            return []
        handoff = HandoffRequests(ledger).get(handoff_id)
        if not handoff:
            return []

        kind = str(handoff.get("kind") or "")
        if (
            kind in {"INTERPRET", "RETHINK"}
            and handoff.get("subject_type") == "ASSIGNMENT"
        ):
            return self._clean_refs(list(handoff.get("evidence_refs") or []))
        if (
            kind in {"REVISION", "RETHINK"}
            and handoff.get("subject_type") == "PLAN_CHANGE"
        ):
            prior = self.get_change(str(handoff.get("subject_id") or ""))
            if prior.get("post_hoc"):
                return self._clean_refs(list(prior.get("evidence_refs") or []))
        return []

    def node_compatible(self, node_id: str, base_version: int, current=None) -> bool:
        """Keep authority only across uninterrupted equivalent node revisions."""
        current = current if current is not None else self.snapshot()
        try:
            base = int(base_version)
            if base < 1 or base > current["plan_version"]:
                return False
            base_nodes = self.revision(base)["nodes"]
            required = set()
            pending = [node_id]
            while pending:
                nid = pending.pop()
                if nid in required:
                    continue
                node = base_nodes[nid]
                if node.get("state") != "ACTIVE":
                    return False
                required.add(nid)
                pending.extend(node["depends_on"])
                if node["parent"]:
                    pending.append(node["parent"])
            for version in range(base, current["plan_version"] + 1):
                nodes = self.revision(version)["nodes"]
                if any(nodes.get(nid) != base_nodes[nid] for nid in required):
                    return False
            if any(current["nodes"].get(nid) != base_nodes[nid] for nid in required):
                return False
        except (KeyError, TypeError, ValueError):
            return False
        return True

    def propose_change(
        self,
        *,
        base_plan_version: int,
        proposer: str,
        reason: str,
        operations: List[Dict[str, Any]],
        post_hoc: bool = False,
        evidence_refs: Optional[List[str]] = None,
    ) -> str:
        """Create a change request without changing the active plan."""
        with self._commit_lock():
            current = self.snapshot()
            base = int(base_plan_version)
            if base != current["plan_version"]:
                raise ValueError(
                    f"stale base_plan_version: {base}; current is {current['plan_version']}"
                )
            proposer = self._required(proposer, "proposer")
            reason = self._required(reason, "reason")
            refs = self._clean_refs(evidence_refs or [])

            required_refs = self._required_post_hoc_refs(proposer)
            if required_refs:
                if not reason.startswith("[proposal:"):
                    raise ValueError(
                        "evidence-bound interpretation must use the autonomous plan proposal handoff"
                    )
                if not post_hoc:
                    raise ValueError(
                        "evidence-bound plan change requires post_hoc=true"
                    )
                missing_required = [ref for ref in required_refs if ref not in refs]
                if missing_required:
                    raise ValueError(
                        "post_hoc plan change must preserve triggering evidence_refs: "
                        + ", ".join(missing_required)
                    )

            if post_hoc:
                if not refs:
                    raise ValueError("post_hoc plan change requires evidence_refs")
                store = EvidenceStore(self.root)
                missing = [ref for ref in refs if store.get_record(ref) is None]
                if missing:
                    raise ValueError(
                        "post_hoc plan change has unknown evidence_refs: " + ", ".join(missing)
                    )
            ops = self._normalize_operations(operations)

            candidate = copy.deepcopy(current)
            self._apply_operations(candidate, ops)
            self._validate(candidate)
            if candidate["nodes"] == current["nodes"]:
                raise ValueError("plan change has no structural effect")

            identity = {
                "base_plan_version": base,
                "proposer": proposer,
                "reason": reason,
                "operations": ops,
                "post_hoc": bool(post_hoc),
                "evidence_refs": refs,
            }
            change_id = self._content_id(identity)
            path = self._change_path(change_id)
            existing = self._read_json(path)
            if existing is not None:
                self._validate_change_identity(existing, change_id)
                return change_id

            self._write_json(
                path,
                {
                    "change_id": change_id,
                    **identity,
                    "status": "PROPOSED",
                    "created_at": utc_now(),
                    "reviewer": None,
                    "review_verdict": None,
                    "review_rationale": "",
                    "reviewed_at": None,
                    "committer": None,
                    "committed_at": None,
                    "committed_version": None,
                },
            )
            return change_id

    def get_change(self, change_id: str) -> Dict[str, Any]:
        cid = self._clean_id(change_id)
        record = self._read_json(self._change_path(cid))
        if record is None:
            raise ValueError(f"unknown change_id: {cid}")
        self._validate_change_identity(record, cid)
        if record.get("status") not in self._CHANGE_STATUSES:
            raise ValueError(f"invalid change status for {cid}")
        return record

    @contextmanager
    def _commit_lock(self):
        with (self.dir / ".commit.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def committed_version(self, record: Dict[str, Any], current=None) -> Optional[int]:
        current = current if current is not None else self.snapshot()
        version = int(record["base_plan_version"]) + 1
        if version > current["plan_version"]:
            return None
        revision = self.revision(version)
        if revision.get("committed_change_id") != record["change_id"]:
            return None
        candidate = copy.deepcopy(self.revision(version - 1))
        self._apply_operations(candidate, record["operations"])
        candidate.update(plan_version=version, committed_change_id=record["change_id"])
        if candidate != revision:
            raise ValueError("committed change does not match its immutable revision")
        return version

    def _recover_commit(self, record: Dict[str, Any], current, committer: str) -> Optional[int]:
        version = self.committed_version(record, current)
        if version is not None and (record["status"] != "COMMITTED" or record.get("committed_version") != version):
            record.update(status="COMMITTED", committed_version=version,
                          committer=self._required(committer, "committer"), committed_at=utc_now())
            self._write_json(self._change_path(record["change_id"]), record)
        return version

    def pending_changes(self) -> List[Dict[str, Any]]:
        """Return current-base proposals/approvals and retire stale pending records."""
        with self._commit_lock():
            current = self.snapshot()
            current_version = current["plan_version"]
            pending: List[Dict[str, Any]] = []
            if not self.changes_dir.is_dir():
                return pending
            for path in sorted(self.changes_dir.glob("*.json")):
                record = self.get_change(path.stem)
                if self._recover_commit(record, current, "supervisor") is not None:
                    continue
                if record["status"] not in {"PROPOSED", "APPROVED"}:
                    continue
                if record["base_plan_version"] != current_version:
                    record["status"] = "STALE"
                    self._write_json(path, record)
                    continue
                pending.append(record)
            prepared_id = None
            if self._revision_path(current_version + 1).is_file():
                prepared_id = self.revision(current_version + 1)["committed_change_id"]
            pending.sort(key=lambda item: (
                item["change_id"] != prepared_id,
                str(item.get("created_at") or ""), item["change_id"],
            ))
            return pending

    def _recover_prepared_revision(self, current) -> Dict[str, Any]:
        """Finish a validated revision left before current.json publication, under both locks."""
        path = self._revision_path(current["plan_version"] + 1)
        if not path.is_file():
            return current
        prepared = self.revision(current["plan_version"] + 1)
        record = self.get_change(prepared["committed_change_id"])
        if (record["status"] != "APPROVED"
            or record["base_plan_version"] != current["plan_version"]
            or self.committed_version(record, prepared) != prepared["plan_version"]):
            raise ValueError("prepared plan revision lacks matching approved change")
        self._require_completed_review(record, prepared=True)
        self._write_current(prepared)
        self._recover_commit(record, prepared, "supervisor")
        return prepared

    def recover_governance(self) -> None:
        """Publish prepared work before routing, and require fresh stale reviews."""
        from .handoff import HandoffRequests
        from .progress import ProgressLedger

        ledger = ProgressLedger(self.root)
        with ledger.assignment_lock(), self._commit_lock():
            current = self._recover_prepared_revision(self.snapshot())
            assignments = {a["assignment_id"]: a for a in ledger.assignments()}
            for path in sorted(self.changes_dir.glob("*.json")):
                record = self.get_change(path.stem)
                if self._recover_commit(record, current, "supervisor") is not None:
                    continue
                if record["base_plan_version"] != current["plan_version"]:
                    if record["status"] in {"PROPOSED", "APPROVED"}:
                        record["status"] = "STALE"
                        self._write_json(path, record)
                    continue
                assignment = assignments.get(record.get("review_assignment_id"))
                if assignment is None and record["status"] == "PROPOSED":
                    reviews = [h for h in HandoffRequests(ledger).all()
                               if h["kind"] == "REVIEW" and h["subject_type"] == "PLAN_CHANGE"
                               and h["subject_id"] == record["change_id"]
                               and h["status"] in {"ASSIGNED", "RESOLVED"}]
                    if reviews:
                        review = max(reviews, key=lambda h: (h["created_at"], h["handoff_id"]))
                        assignment = assignments.get(review.get("assigned_assignment_id"))
                if (record["status"] not in {"PROPOSED", "APPROVED", "REVISE", "REJECTED"}
                    or not assignment
                    or not (assignment["state"] == "STALE" or ledger._is_plan_stale(assignment))):
                    continue
                record.update(status="PROPOSED", reviewer=None, review_verdict=None,
                              review_rationale="", reviewed_at=None,
                              review_epoch=int(record.get("review_epoch", 0)) + 1)
                record.pop("review_assignment_id", None)
                self._write_json(path, record)

    def _require_completed_review(self, record, *, prepared: bool = False) -> None:
        review_assignment_id = record.get("review_assignment_id")
        if not review_assignment_id:
            return
        from .handoff import HandoffRequests
        from .progress import ProgressLedger

        ledger = ProgressLedger(self.root)
        assignment = next((a for a in ledger.assignments()
                           if a["assignment_id"] == review_assignment_id), None)
        if not prepared and (not assignment or assignment["state"] != "DONE"
                             or assignment.get("outcome") != "COMPLETE"
                             or ledger._is_plan_stale(assignment)):
            raise ValueError("managed plan approval requires current completed review authority")
        reviews = [item for item in HandoffRequests(ledger).all()
                   if item.get("kind") == "REVIEW" and item.get("subject_type") == "PLAN_CHANGE"
                   and item.get("subject_id") == record["change_id"]]
        if not any(item.get("status") == "RESOLVED"
                   and item.get("resolution_assignment_id") == review_assignment_id for item in reviews):
            raise ValueError("managed plan approval requires its completed REVIEW handoff")

    def review_change(
        self,
        change_id: str,
        *,
        reviewer: str,
        rationale: str,
        verdict: Optional[str] = None,
        approve: Optional[bool] = None,
        review_assignment_id: str = "",
    ) -> Dict[str, Any]:
        """Record one Reviewer verdict without collapsing REVISE into REJECT.

        ``approve`` is retained only for old callers: ``True`` maps to APPROVE and
        ``False`` maps to REVISE. New callers must use ``verdict``.
        """
        with self._commit_lock():
            record = self.get_change(change_id)
            if record["status"] != "PROPOSED":
                raise ValueError(f"change {change_id} is not awaiting review")
            current = self.snapshot()
            if record["base_plan_version"] != current["plan_version"]:
                record["status"] = "STALE"
                self._write_json(self._change_path(change_id), record)
                return record

            if verdict is None or not str(verdict).strip():
                if approve is None:
                    raise ValueError("plan review requires verdict APPROVE, REVISE, or REJECT")
                normalized = "APPROVE" if bool(approve) else "REVISE"
            else:
                normalized = str(verdict).strip().upper()
                if normalized not in self.REVIEW_VERDICTS:
                    raise ValueError(f"verdict must be one of {self.REVIEW_VERDICTS}")
                if approve is not None and bool(approve) != (normalized == "APPROVE"):
                    raise ValueError("approve compatibility flag conflicts with explicit verdict")

            record["reviewer"] = self._required(reviewer, "reviewer")
            record["review_verdict"] = normalized
            record["review_rationale"] = self._required(rationale, "rationale")
            record["reviewed_at"] = utc_now()
            if review_assignment_id:
                record["review_assignment_id"] = review_assignment_id
            record["status"] = {
                "APPROVE": "APPROVED",
                "REVISE": "REVISE",
                "REJECT": "REJECTED",
            }[normalized]
            self._write_json(self._change_path(change_id), record)
            return record

    def commit_change(self, change_id: str, *, committer: str) -> int:
        """Commit one Reviewer-approved request as exactly one new plan revision."""
        from .progress import ProgressLedger

        committer = self._required(committer, "committer")
        with ProgressLedger(self.root).assignment_lock(), self._commit_lock():
            record = self.get_change(change_id)
            current = self._recover_prepared_revision(self.snapshot())

            committed = self._recover_commit(record, current, committer)
            if committed is not None:
                return committed

            if record["status"] != "APPROVED":
                raise ValueError(f"change {change_id} is not approved")
            self._require_completed_review(record)
            if record["base_plan_version"] != current["plan_version"]:
                record["status"] = "STALE"
                self._write_json(self._change_path(change_id), record)
                raise ValueError(
                    f"stale change {change_id}: base {record['base_plan_version']}, "
                    f"current {current['plan_version']}"
                )

            candidate = copy.deepcopy(current)
            self._apply_operations(candidate, record["operations"])
            candidate["plan_version"] = current["plan_version"] + 1
            candidate["committed_change_id"] = change_id
            self._validate(candidate)
            self._write_revision(candidate)
            self._write_current(candidate)

            record["status"] = "COMMITTED"
            record["committer"] = self._required(committer, "committer")
            record["committed_at"] = utc_now()
            record["committed_version"] = candidate["plan_version"]
            self._write_json(self._change_path(change_id), record)
            return candidate["plan_version"]

    def _upgrade_v1(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """One-time migration for A2-6a plans created before revision history."""
        upgraded = copy.deepcopy(payload)
        for node in upgraded.get("nodes", {}).values():
            if isinstance(node, dict):
                node["state"] = "ACTIVE"
        upgraded["schema_version"] = self.SCHEMA_VERSION
        upgraded["plan_version"] = 1
        upgraded["committed_change_id"] = None
        self._validate(upgraded)
        self._write_revision(upgraded)
        self._write_current(upgraded)
        return upgraded

    def _apply_operations(
        self, payload: Dict[str, Any], operations: List[Dict[str, Any]]
    ) -> None:
        for op in operations:
            kind = op["op"]
            if kind == "add_node":
                nodes = payload["nodes"]
                nid = op["node_id"]
                parent = op["parent"]
                if nid in nodes:
                    raise ValueError(f"duplicate node_id: {nid}")
                if parent not in nodes:
                    raise ValueError(f"unknown parent: {parent}")
                nodes[nid] = {
                    "title": op["title"],
                    "summary": op["summary"],
                    "parent": parent,
                    "depends_on": list(op["depends_on"]),
                    "state": "ACTIVE",
                }
            elif kind == "set_dependencies":
                nid = op["node_id"]
                if nid not in payload["nodes"]:
                    raise ValueError(f"unknown node_id: {nid}")
                payload["nodes"][nid]["depends_on"] = list(op["depends_on"])
            else:
                nid = op["node_id"]
                if nid not in payload["nodes"]:
                    raise ValueError(f"unknown node_id: {nid}")
                payload["nodes"][nid]["state"] = op["state"]

    def _normalize_operations(
        self, operations: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        if not isinstance(operations, list) or not operations:
            raise ValueError("plan change requires at least one operation")
        out: List[Dict[str, Any]] = []
        for raw in operations:
            if not isinstance(raw, dict):
                raise ValueError("plan change operation must be an object")
            kind = str(raw.get("op", "")).strip()
            if kind not in self._OPS:
                raise ValueError(f"unsupported plan operation: {kind or '<empty>'}")
            if kind == "add_node":
                self._reject_extra(
                    raw, {"op", "node_id", "title", "summary", "parent", "depends_on"}, kind
                )
                out.append(
                    {
                        "op": kind,
                        "node_id": self._clean_id(raw.get("node_id", "")),
                        "title": self._clean_text(
                            raw.get("title", ""),
                            "title",
                            self.MAX_TITLE_CHARS,
                            required=True,
                        ),
                        "summary": self._clean_text(
                            raw.get("summary", ""), "summary", self.MAX_SUMMARY_CHARS
                        ),
                        "parent": self._clean_id(raw.get("parent", "")),
                        "depends_on": self._clean_dependencies(raw.get("depends_on", [])),
                    }
                )
            elif kind == "set_dependencies":
                self._reject_extra(raw, {"op", "node_id", "depends_on"}, kind)
                out.append(
                    {
                        "op": kind,
                        "node_id": self._clean_id(raw.get("node_id", "")),
                        "depends_on": self._clean_dependencies(raw.get("depends_on", [])),
                    }
                )
            else:
                self._reject_extra(raw, {"op", "node_id", "state"}, kind)
                state = str(raw.get("state", "")).strip().upper()
                if state not in self.PLAN_STATES:
                    raise ValueError(f"invalid plan state: {state}")
                out.append(
                    {
                        "op": kind,
                        "node_id": self._clean_id(raw.get("node_id", "")),
                        "state": state,
                    }
                )
        return out

    @staticmethod
    def _reject_extra(raw: Dict[str, Any], allowed: set, kind: str) -> None:
        extra = set(raw) - allowed
        if extra:
            raise ValueError(f"unsupported fields for {kind}: {sorted(extra)}")

    @staticmethod
    def _content_id(payload: Dict[str, Any]) -> str:
        canon = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canon).hexdigest()[:16]

    @staticmethod
    def _required(value: Any, field: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError(f"{field} is required")
        return text

    @staticmethod
    def _clean_id(value: Any) -> str:
        value = str(value).strip()
        if not value:
            raise ValueError("node_id is required")
        return value

    @staticmethod
    def _clean_text(
        value: Any, field: str, limit: int, *, required: bool = False
    ) -> str:
        text = str(value).strip()
        if required and not text:
            raise ValueError(f"{field} is required")
        if len(text) > limit:
            raise ValueError(f"{field} exceeds {limit} characters")
        return text

    def _clean_dependencies(self, values: Any) -> List[str]:
        if not isinstance(values, list):
            raise ValueError("depends_on must be a list")
        out: List[str] = []
        seen = set()
        for value in values:
            dep = self._clean_id(value)
            if dep not in seen:
                seen.add(dep)
                out.append(dep)
        return out

    @staticmethod
    def _clean_refs(values: List[str]) -> List[str]:
        return list(dict.fromkeys(str(x).strip() for x in values if str(x).strip()))

    def _read_current(self) -> Dict[str, Any]:
        payload = self._read_json(self.path)
        if payload is None:
            raise FileNotFoundError(f"research plan does not exist: {self.path}")
        return payload

    @staticmethod
    def _read_json(path: Path) -> Optional[Dict[str, Any]]:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"JSON root must be an object: {path}")
        return payload

    def _write_current(self, payload: Dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(self._json_text(payload), encoding="utf-8")
        tmp.replace(self.path)

    def _write_revision(self, payload: Dict[str, Any]) -> None:
        path = self._revision_path(payload["plan_version"])
        existing = self._read_json(path)
        if existing is not None:
            if existing != payload:
                raise ValueError(f"plan revision {payload['plan_version']} is immutable")
            return
        self._write_json(path, payload)

    def _write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self._json_text(payload), encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _json_text(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def _revision_path(self, plan_version: int) -> Path:
        return self.revisions_dir / f"{int(plan_version):04d}.json"

    def _change_path(self, change_id: str) -> Path:
        return self.changes_dir / f"{change_id}.json"

    def _validate_change_identity(self, record: Dict[str, Any], change_id: str) -> None:
        identity = {
            "base_plan_version": record.get("base_plan_version"),
            "proposer": record.get("proposer"),
            "reason": record.get("reason"),
            "operations": record.get("operations"),
            "post_hoc": bool(record.get("post_hoc")),
            "evidence_refs": record.get("evidence_refs", []),
        }
        if record.get("change_id") != change_id or self._content_id(identity) != change_id:
            raise ValueError(f"plan change identity mismatch: {change_id}")

    def _validate(self, payload: Dict[str, Any]) -> None:
        if set(payload) != self._TOP_FIELDS:
            raise ValueError("unsupported research plan top-level fields")
        if payload.get("schema_version") != self.SCHEMA_VERSION:
            raise ValueError("unsupported research plan schema_version")
        version = payload.get("plan_version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("research plan requires positive integer plan_version")
        committed_change_id = payload.get("committed_change_id")
        if committed_change_id is not None and (
            not isinstance(committed_change_id, str) or not committed_change_id.strip()
        ):
            raise ValueError("invalid committed_change_id")

        root_id = payload.get("root_id")
        nodes = payload.get("nodes")
        if not isinstance(root_id, str) or not root_id.strip():
            raise ValueError("research plan requires root_id")
        if not isinstance(nodes, dict) or root_id not in nodes:
            raise ValueError("research plan requires a valid root node")

        for nid, node in nodes.items():
            if not isinstance(nid, str) or not nid.strip() or not isinstance(node, dict):
                raise ValueError("invalid research plan node")
            if set(node) != self._NODE_FIELDS:
                raise ValueError(f"unsupported fields on node {nid}")
            self._clean_text(
                node.get("title", ""), "title", self.MAX_TITLE_CHARS, required=True
            )
            self._clean_text(
                node.get("summary", ""), "summary", self.MAX_SUMMARY_CHARS
            )
            if node.get("state") not in self.PLAN_STATES:
                raise ValueError(f"invalid plan state for node {nid}: {node.get('state')}")
            parent = node.get("parent")
            if nid == root_id:
                if parent is not None:
                    raise ValueError("root node cannot have a parent")
            elif not isinstance(parent, str) or parent not in nodes:
                raise ValueError(f"unknown parent for node {nid}: {parent}")

            deps = node.get("depends_on")
            if not isinstance(deps, list):
                raise ValueError(f"depends_on must be a list for node {nid}")
            if len(deps) != len(set(deps)):
                raise ValueError(f"duplicate dependencies for node {nid}")
            for dep in deps:
                if not isinstance(dep, str) or dep not in nodes:
                    raise ValueError(f"unknown dependency for node {nid}: {dep}")
                if dep == nid:
                    raise ValueError(f"node cannot depend on itself: {nid}")

        self._validate_depths(nodes, root_id)
        self._validate_dependency_dag(nodes)

    def _validate_depths(
        self, nodes: Dict[str, Dict[str, Any]], root_id: str
    ) -> None:
        for nid in nodes:
            depth = 1
            current = nid
            seen = set()
            while current != root_id:
                if current in seen:
                    raise ValueError(f"parent cycle at node {nid}")
                seen.add(current)
                parent = nodes[current]["parent"]
                if not isinstance(parent, str) or parent not in nodes:
                    raise ValueError(f"invalid parent chain for node {nid}")
                current = parent
                depth += 1
                if depth > self.MAX_DEPTH:
                    raise ValueError(f"node {nid} exceeds maximum depth {self.MAX_DEPTH}")

    @staticmethod
    def _validate_dependency_dag(nodes: Dict[str, Dict[str, Any]]) -> None:
        state: Dict[str, int] = {}

        def visit(nid: str) -> None:
            mark = state.get(nid, 0)
            if mark == 1:
                raise ValueError(f"dependency cycle at node {nid}")
            if mark == 2:
                return
            state[nid] = 1
            for dep in nodes[nid]["depends_on"]:
                visit(dep)
            state[nid] = 2

        for nid in nodes:
            visit(nid)
