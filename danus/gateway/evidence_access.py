"""Read-only Evidence authority derived from an actual INTERPRET Handoff.

The scientific brief is created before a dynamic protocol exists; it cannot
authorize the later E1 protocol. A resolved/active control-plane Handoff whose
immutable evidence_refs include this Evidence is the correct narrower grant.
This module does not expose arbitrary filesystem reads.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from danus.core import EvidenceStore, ProgressLedger
from danus.core.evidence import _content_id, protocol_identity
from danus.core.handoff import HandoffRequests

_ID = re.compile(r"[0-9a-f]{16}\Z")
_MAX_JSON_BYTES = 1024 * 1024
_MAX_RESULT_BYTES = 16384


def authorized_interpret_evidence(
    root: Path, ledger: ProgressLedger, assignment: Dict[str, Any], evidence_id: str,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Authorize exactly an assigned Researcher's INTERPRET Evidence, fail closed."""
    if not isinstance(evidence_id, str) or not _ID.fullmatch(evidence_id):
        raise ValueError("invalid Evidence ID")
    if assignment.get("role") != "researcher" or assignment.get("state") not in {"ASSIGNED", "RUNNING"}:
        raise PermissionError("Evidence interpretation requires an active Researcher")
    handoff_id = str(assignment.get("handoff_id") or "")
    handoff = HandoffRequests(ledger).get(handoff_id) if handoff_id else None
    if (
        not handoff or handoff.get("kind") != "INTERPRET"
        or handoff.get("status") != "ASSIGNED"
        or handoff.get("target_role") != "researcher"
        or handoff.get("assigned_assignment_id") != assignment.get("assignment_id")
        or handoff.get("subject_type") != "ASSIGNMENT"
        or handoff.get("source_role") != "experimenter"
        or handoff.get("source_assignment_id") != handoff.get("subject_id")
        or evidence_id not in handoff.get("evidence_refs", [])
        or handoff.get("plan_version") != assignment.get("plan_version")
    ):
        raise PermissionError("Evidence is not bound to the current INTERPRET handoff")
    source = next(
        (a for a in ledger.assignments() if a["assignment_id"] == handoff["subject_id"]),
        None,
    )
    if (
        not source or source.get("role") != "experimenter"
        or source.get("state") != "DONE" or source.get("outcome") != "COMPLETE"
    ):
        raise PermissionError("INTERPRET source must be the completed Experimenter")
    if source.get("node_id") != handoff.get("node_id"):
        # Planned interpretation is an exact downstream Plan assignment. The
        # original same-node handoff remains valid for legacy/unplanned routes.
        from danus.orchestration.autonomy import _interpret_plan_target
        target = _interpret_plan_target(root, str(source.get("node_id") or ""))
        if not target or target != handoff.get("node_id") or target != assignment.get("node_id"):
            raise PermissionError("INTERPRET handoff is not bound to the planned Researcher")
    store = EvidenceStore(root)
    record = store.get_record(evidence_id)
    if not record or record.get("evidence_id") != evidence_id:
        raise ValueError("Evidence Record not found")
    protocol_id = str(record.get("protocol_id") or "")
    protocol = store.get_protocol(protocol_id) if _ID.fullmatch(protocol_id) else None
    if (
        not protocol or protocol.get("protocol_id") != protocol_id
        or protocol.get("status") != "frozen"
        or _content_id(protocol_identity(protocol)) != protocol_id
        or record.get("author") != source.get("worker")
    ):
        raise PermissionError("Evidence is not a valid product of its Experimenter source")
    source_id = protocol.get("execution_assignment_id")
    if source_id:
        if source_id != source["assignment_id"]:
            raise PermissionError("dynamic protocol belongs to another Experimenter assignment")
    elif protocol_id not in str(source.get("task") or ""):
        raise PermissionError("legacy protocol was not bound to the source task")
    return record, protocol


def select_verified_json_artifact(
    root: Path,
    protocol_id: str,
    record: Dict[str, Any],
    artifact_index: int,
    json_pointer: str,
) -> Dict[str, Any]:
    """Read a bounded JSON subtree, after verifying the exact immutable ref."""
    if type(artifact_index) is not int:
        raise ValueError("artifact_index must be an integer")
    refs = record.get("artifacts") or []
    if not isinstance(refs, list) or not 0 <= artifact_index < len(refs):
        raise ValueError("artifact_index is outside the Evidence artifact list")
    ref = refs[artifact_index]
    if not isinstance(ref, dict):
        raise ValueError("malformed raw artifact reference")
    name = str(ref.get("path") or "")
    if not name or Path(name).is_absolute() or ".." in Path(name).parts:
        raise PermissionError("raw artifact reference must be a project-relative path")
    base = (root / "experiment_runtime" / protocol_id / "artifacts").resolve()
    target = (root / name).resolve(strict=True)
    if not target.is_relative_to(base) or not target.is_file():
        raise PermissionError("raw artifact is outside the immutable experiment output area")
    if target.suffix != ".json":
        raise ValueError("only bounded JSON inspection is supported; binary artifacts remain sealed")
    size = target.stat().st_size
    if size != ref.get("size") or size > _MAX_JSON_BYTES:
        raise ValueError("raw JSON size differs from Evidence or exceeds inspection limit")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != ref.get("sha256"):
        raise PermissionError("raw JSON hash differs from immutable Evidence")
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(json_pointer, str) or (json_pointer and not json_pointer.startswith("/")):
        raise ValueError("json_pointer must be empty or begin with /")
    value = data
    for token in json_pointer.split("/")[1:] if json_pointer else []:
        if re.search(r"~(?![01])", token):
            raise ValueError("invalid JSON pointer escaping")
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            if key not in value:
                raise KeyError(f"JSON field absent: {key}")
            value = value[key]
        elif isinstance(value, list):
            if not key.isdecimal() or int(key) >= len(value):
                raise KeyError("JSON array index absent")
            value = value[int(key)]
        else:
            raise KeyError("JSON pointer descends into a scalar")
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > _MAX_RESULT_BYTES:
        raise ValueError("JSON result too large; request a narrower json_pointer")
    return {
        "artifact_index": artifact_index, "path": name,
        "sha256": digest, "json_pointer": json_pointer, "value": value,
    }
