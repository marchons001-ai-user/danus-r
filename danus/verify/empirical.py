"""Deterministic checks for empirical scientific evidence.

These checks run before any semantic LLM verifier. They answer only questions a
program can decide reliably: record identity, protocol lineage, artifact
existence/hash/size, and primary-metric recomputation when the frozen protocol
pins a reducer. Scientific interpretation remains a later semantic audit.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List

from danus.core.evidence import EvidenceStore, _content_id
from danus.core.metrics import metrics_equal, reduce_metrics


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _protocol_identity(protocol: Dict[str, Any]) -> Dict[str, Any]:
    identity = {
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
    # Historical protocols did not have this field; omit it from their identity
    # so existing content ids remain valid.
    if "metric_reducer" in protocol:
        identity["metric_reducer"] = protocol.get("metric_reducer")
    return identity


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

    def load_protocol_chain(protocol_id: str) -> None:
        """Validate and collect a protocol plus every declared revision ancestor."""
        current = protocol_id
        seen = set()
        while current and current not in seen:
            seen.add(current)
            protocol = store.get_protocol(current)
            check(
                f"protocol:{current}:exists",
                protocol is not None,
                "protocol exists" if protocol is not None else "missing or unreadable protocol",
            )
            if protocol is None:
                return
            protocols[current] = protocol
            identity_ok = (
                protocol.get("protocol_id") == current
                and _content_id(_protocol_identity(protocol)) == current
            )
            check(
                f"protocol:{current}:identity",
                identity_ok,
                "content-addressed protocol id matches protocol contents",
            )
            check(
                f"protocol:{current}:frozen",
                protocol.get("status") == "frozen",
                f"protocol status is {protocol.get('status')!r}; expected 'frozen'",
            )
            parent = str(protocol.get("parent_protocol_id") or "")
            if parent:
                check(
                    f"protocol:{current}:revision_reason",
                    bool(str(protocol.get("revision_reason") or "").strip()),
                    "protocol revision records a revision_reason",
                )
            if bool(protocol.get("post_hoc")):
                check(
                    f"protocol:{current}:post_hoc_lineage",
                    bool(parent),
                    "post-hoc protocol names the parent whose observed result motivated the revision",
                )
            current = parent
        if current:
            check(
                f"protocol:{protocol_id}:lineage_cycle",
                False,
                f"protocol lineage cycles at {current}",
            )

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
        if protocol_id:
            load_protocol_chain(protocol_id)

        check(
            f"evidence:{evidence_id}:matrix_coverage",
            record.get("matrix_coverage") is not None,
            "matrix_coverage is explicitly recorded",
        )

        artifacts = record.get("artifacts", [])
        artifacts_ok = isinstance(artifacts, list)
        if not artifacts_ok:
            check(f"evidence:{evidence_id}:artifacts", False, "artifacts must be a list")
            continue
        for index, artifact in enumerate(artifacts):
            prefix = f"evidence:{evidence_id}:artifact:{index}"
            if not isinstance(artifact, dict):
                artifacts_ok = False
                check(prefix, False, "artifact reference must be an object")
                continue
            raw_path = str(artifact.get("path") or "")
            expected_hash = str(artifact.get("sha256") or "")
            expected_size = artifact.get("size")
            if not raw_path or not expected_hash or not isinstance(expected_size, int):
                artifacts_ok = False
                check(prefix, False, "artifact reference requires path, sha256, and integer size")
                continue
            path = Path(raw_path)
            if not path.is_absolute():
                path = root / path
            if not path.is_file():
                artifacts_ok = False
                check(prefix, False, f"artifact missing: {path}")
                continue
            actual_size = path.stat().st_size
            if actual_size != expected_size:
                artifacts_ok = False
                check(
                    prefix,
                    False,
                    f"artifact size changed: expected {expected_size}, got {actual_size}",
                )
                continue
            actual_hash = _sha256(path)
            hash_ok = actual_hash == expected_hash
            artifacts_ok = artifacts_ok and hash_ok
            check(
                prefix,
                hash_ok,
                "artifact SHA-256 matches manifest" if hash_ok else "artifact SHA-256 changed",
            )

        protocol = protocols.get(protocol_id)
        protocol_ok = bool(
            protocol
            and protocol.get("protocol_id") == protocol_id
            and protocol.get("status") == "frozen"
            and _content_id(_protocol_identity(protocol)) == protocol_id
        )
        if (
            record_id_ok
            and artifacts_ok
            and protocol_ok
            and isinstance(protocol, dict)
            and isinstance(protocol.get("metric_reducer"), dict)
        ):
            try:
                recomputed = reduce_metrics(root, protocol, artifacts)
            except Exception as exc:
                check(
                    f"evidence:{evidence_id}:primary_metrics_recompute",
                    False,
                    f"cannot recompute frozen primary metrics: {exc}",
                )
            else:
                equal = metrics_equal(record.get("primary_metrics"), recomputed)
                check(
                    f"evidence:{evidence_id}:primary_metrics_recompute",
                    equal,
                    "recorded primary_metrics match raw-artifact recomputation"
                    if equal
                    else f"recorded primary_metrics differ from recomputed {recomputed}",
                )

    return {
        "passed": not errors,
        "checks": checks,
        "errors": errors,
        "protocols": list(protocols.values()),
        "evidence_records": records,
    }
