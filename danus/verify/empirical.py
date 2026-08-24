"""Deterministic checks for empirical scientific evidence.

These checks run before any semantic LLM verifier. They answer only questions a
program can decide reliably: record identity, protocol lineage, artifact
existence/hash/size, and basic provenance completeness. Scientific interpretation
remains a later semantic audit.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List

from danus.core.evidence import EvidenceStore, _content_id


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _protocol_identity(protocol: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "question": protocol.get("question", ""),
        "hypothesis": protocol.get("hypothesis", ""),
        "matrix": protocol.get("matrix"),
        "primary_metrics": protocol.get("primary_metrics", []),
        "decision_rule": protocol.get("decision_rule", ""),
        "author": protocol.get("author", ""),
        "parent_protocol_id": protocol.get("parent_protocol_id"),
        "post_hoc": bool(protocol.get("post_hoc", False)),
        "revision_reason": protocol.get("revision_reason", ""),
    }


def _record_identity(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "protocol_id": record.get("protocol_id", ""),
        "author": record.get("author", ""),
        "code_commit": record.get("code_commit", ""),
        "data_ref": record.get("data_ref", ""),
        "config_ref": record.get("config_ref", ""),
        "matrix_coverage": record.get("matrix_coverage"),
        "primary_metrics": record.get("primary_metrics", {}),
        "failures": record.get("failures", []),
        "deviations": record.get("deviations", []),
        "artifacts": record.get("artifacts", []),
    }


def check_empirical_evidence(
    project_dir: Path,
    *,
    scope: Dict[str, Any],
    evidence_refs: List[str],
) -> Dict[str, Any]:
    """Validate empirical provenance without using an LLM.

    Returns a bundle suitable for a later semantic verifier. ``passed=False``
    means the semantic verifier must not run.
    """
    root = Path(project_dir)
    checks: List[Dict[str, Any]] = []
    errors: List[str] = []
    protocols: Dict[str, Dict[str, Any]] = {}
    records: List[Dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})
        if not passed:
            errors.append(f"{name}: {detail}")

    check("project_dir", root.is_dir(), f"project directory: {root}")
    check("scope", bool(scope), "candidate empirical fact requires non-empty scope")
    refs = list(dict.fromkeys(str(x).strip() for x in evidence_refs if str(x).strip()))
    check("evidence_refs", bool(refs), "candidate empirical fact requires evidence_refs")
    if errors:
        return {
            "passed": False,
            "checks": checks,
            "errors": errors,
            "protocols": [],
            "evidence_records": [],
        }

    store = EvidenceStore(root)
    for evidence_id in refs:
        record = store.get_record(evidence_id)
        check(
            f"evidence:{evidence_id}:exists",
            record is not None,
            "evidence record exists" if record is not None else "missing or unreadable evidence record",
        )
        if record is None:
            continue

        record_id_ok = (
            record.get("evidence_id") == evidence_id
            and _content_id(_record_identity(record)) == evidence_id
        )
        check(
            f"evidence:{evidence_id}:identity",
            record_id_ok,
            "content-addressed evidence id matches record contents",
        )
        records.append(record)

        protocol_id = str(record.get("protocol_id") or "")
        check(
            f"evidence:{evidence_id}:protocol_ref",
            bool(protocol_id),
            "evidence record names a protocol_id",
        )
        if not protocol_id:
            continue

        protocol = store.get_protocol(protocol_id)
        check(
            f"protocol:{protocol_id}:exists",
            protocol is not None,
            "protocol exists" if protocol is not None else "missing or unreadable protocol",
        )
        if protocol is None:
            continue
        protocols[protocol_id] = protocol

        protocol_id_ok = (
            protocol.get("protocol_id") == protocol_id
            and _content_id(_protocol_identity(protocol)) == protocol_id
        )
        check(
            f"protocol:{protocol_id}:identity",
            protocol_id_ok,
            "content-addressed protocol id matches protocol contents",
        )
        check(
            f"protocol:{protocol_id}:frozen",
            protocol.get("status") == "frozen",
            f"protocol status is {protocol.get('status')!r}; expected 'frozen'",
        )

        parent = str(protocol.get("parent_protocol_id") or "")
        if parent:
            parent_exists = store.get_protocol(parent) is not None
            check(
                f"protocol:{protocol_id}:parent",
                parent_exists,
                f"parent protocol {parent} exists",
            )
            check(
                f"protocol:{protocol_id}:revision_reason",
                bool(str(protocol.get("revision_reason") or "").strip()),
                "protocol revision records a revision_reason",
            )
        if bool(protocol.get("post_hoc")):
            check(
                f"protocol:{protocol_id}:post_hoc_lineage",
                bool(parent),
                "post-hoc protocol names the parent whose observed result motivated the revision",
            )

        check(
            f"evidence:{evidence_id}:matrix_coverage",
            record.get("matrix_coverage") is not None,
            "matrix_coverage is explicitly recorded",
        )

        artifacts = record.get("artifacts", [])
        if not isinstance(artifacts, list):
            check(
                f"evidence:{evidence_id}:artifacts",
                False,
                "artifacts must be a list",
            )
            continue
        for index, artifact in enumerate(artifacts):
            prefix = f"evidence:{evidence_id}:artifact:{index}"
            if not isinstance(artifact, dict):
                check(prefix, False, "artifact reference must be an object")
                continue
            raw_path = str(artifact.get("path") or "")
            expected_hash = str(artifact.get("sha256") or "")
            expected_size = artifact.get("size")
            if not raw_path or not expected_hash or not isinstance(expected_size, int):
                check(prefix, False, "artifact reference requires path, sha256, and integer size")
                continue
            path = Path(raw_path)
            if not path.is_absolute():
                path = root / path
            if not path.is_file():
                check(prefix, False, f"artifact missing: {path}")
                continue
            actual_size = path.stat().st_size
            if actual_size != expected_size:
                check(
                    prefix,
                    False,
                    f"artifact size changed: expected {expected_size}, got {actual_size}",
                )
                continue
            actual_hash = _sha256(path)
            check(
                prefix,
                actual_hash == expected_hash,
                "artifact SHA-256 matches manifest" if actual_hash == expected_hash else "artifact SHA-256 changed",
            )

    return {
        "passed": not errors,
        "checks": checks,
        "errors": errors,
        "protocols": list(protocols.values()),
        "evidence_records": records,
    }
