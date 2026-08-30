"""File-first scientific evidence records.

This module deliberately stores only the fixed provenance needed by later
verification. It does not schedule experiments, judge claims, copy large output
trees, or call an LLM.

Layout under one project::

    evidence/protocols/<protocol_id>.json   immutable frozen experiment plans
    evidence/records/<evidence_id>.json     immutable records of what actually ran

Artifact files stay where the experiment produced them; evidence records point to
those files by path + SHA-256 + size so later verification can detect mutation
without duplicating large data.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._util import utc_now


def _content_id(payload: Dict[str, Any]) -> str:
    canon = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()[:16]


def protocol_identity(protocol: Dict[str, Any]) -> Dict[str, Any]:
    """Return the content-addressed identity of old and reducer-aware protocols."""
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
    # Omit this field for historical protocols so their existing ids remain valid.
    if "metric_reducer" in protocol:
        identity["metric_reducer"] = protocol.get("metric_reducer")
    return identity


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _stable_record(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Ignore creation timestamps when checking one content-addressed retry."""
    return {
        key: value
        for key, value in payload.items()
        if key not in {"frozen_at", "created_at"}
    }


class EvidenceStore:
    """Project-shared immutable protocol/evidence records."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.dir = self.root / "evidence"
        self.protocols_dir = self.dir / "protocols"
        self.records_dir = self.dir / "records"

    def _protocol_path(self, protocol_id: str) -> Path:
        return self.protocols_dir / f"{protocol_id}.json"

    def _record_path(self, evidence_id: str) -> Path:
        return self.records_dir / f"{evidence_id}.json"

    @staticmethod
    def _write_once(path: Path, payload: Dict[str, Any]) -> None:
        """Crash-atomically create or recover one immutable JSON record."""
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(f".{path.name}.lock")
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                existing = _read_json(path)
                if existing is not None:
                    if _stable_record(existing) == _stable_record(payload):
                        return
                    raise ValueError(f"immutable evidence record conflict: {path}")

                text = json.dumps(
                    payload, ensure_ascii=False, indent=2, sort_keys=True
                ) + "\n"
                fd, tmp_name = tempfile.mkstemp(
                    prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(text)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp_name, path)
                finally:
                    try:
                        os.unlink(tmp_name)
                    except FileNotFoundError:
                        pass
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    # --------------------------------------------------------------- protocol
    def freeze_protocol(
        self,
        *,
        question: str,
        hypothesis: str,
        matrix: Any,
        primary_metrics: List[str],
        decision_rule: str,
        author: str,
        parent_protocol_id: Optional[str] = None,
        post_hoc: bool = False,
        revision_reason: str = "",
        metric_reducer: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Write one immutable frozen protocol and return its content id.

        A changed plan is a new protocol, never an overwrite. Revisions must point
        to an existing parent and explain why they changed. ``post_hoc`` is an
        explicit provenance statement: A2-4a records it; later workflow code will
        decide it automatically when results have already been observed.

        Runnable protocols may additionally pin one metric reducer by the same
        path/SHA-256/size reference shape used for artifacts plus a ``function``
        name. The field is omitted entirely for historical protocols so their
        content ids remain backward-compatible.
        """
        if not question.strip() or not hypothesis.strip() or not decision_rule.strip():
            raise ValueError("question, hypothesis, and decision_rule are required")
        metrics = [str(x).strip() for x in primary_metrics if str(x).strip()]
        if not metrics:
            raise ValueError("at least one primary metric is required")
        if parent_protocol_id:
            if self.get_protocol(parent_protocol_id) is None:
                raise ValueError(f"unknown parent_protocol_id: {parent_protocol_id}")
            if not revision_reason.strip():
                raise ValueError("a protocol revision requires revision_reason")
        elif post_hoc:
            raise ValueError("post_hoc protocol requires parent_protocol_id")

        reducer = None
        if metric_reducer is not None:
            reducer = dict(metric_reducer)
            if not str(reducer.get("path") or ""):
                raise ValueError("metric_reducer requires path")
            if not str(reducer.get("sha256") or ""):
                raise ValueError("metric_reducer requires sha256")
            if not isinstance(reducer.get("size"), int):
                raise ValueError("metric_reducer requires integer size")
            reducer["function"] = str(reducer.get("function") or "reduce_metrics")

        identity = {
            "question": question.strip(),
            "hypothesis": hypothesis.strip(),
            "matrix": matrix,
            "primary_metrics": metrics,
            "decision_rule": decision_rule.strip(),
            "author": author,
            "parent_protocol_id": parent_protocol_id,
            "post_hoc": bool(post_hoc),
            "revision_reason": revision_reason.strip(),
        }
        if reducer is not None:
            identity["metric_reducer"] = reducer
        protocol_id = _content_id(identity)
        payload = {
            "protocol_id": protocol_id,
            **identity,
            "status": "frozen",
            "frozen_at": utc_now(),
        }
        self._write_once(self._protocol_path(protocol_id), payload)
        return protocol_id

    def get_protocol(self, protocol_id: str) -> Optional[Dict[str, Any]]:
        return _read_json(self._protocol_path(protocol_id))

    # ---------------------------------------------------------------- record
    def add_record(
        self,
        *,
        protocol_id: str,
        author: str,
        code_commit: str = "",
        data_ref: str = "",
        config_ref: str = "",
        matrix_coverage: Any = None,
        primary_metrics: Optional[Dict[str, Any]] = None,
        failures: Optional[List[Any]] = None,
        deviations: Optional[List[Any]] = None,
        artifacts: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Record what actually ran. This is provenance, not a scientific verdict."""
        if self.get_protocol(protocol_id) is None:
            raise ValueError(f"unknown protocol_id: {protocol_id}")
        identity = {
            "protocol_id": protocol_id,
            "author": author,
            "code_commit": code_commit,
            "data_ref": data_ref,
            "config_ref": config_ref,
            "matrix_coverage": matrix_coverage,
            "primary_metrics": primary_metrics or {},
            "failures": failures or [],
            "deviations": deviations or [],
            "artifacts": artifacts or [],
        }
        evidence_id = _content_id(identity)
        payload = {
            "evidence_id": evidence_id,
            **identity,
            "created_at": utc_now(),
        }
        self._write_once(self._record_path(evidence_id), payload)
        return evidence_id

    def get_record(self, evidence_id: str) -> Optional[Dict[str, Any]]:
        return _read_json(self._record_path(evidence_id))

    # -------------------------------------------------------------- artifact
    def artifact_ref(self, path: Path) -> Dict[str, Any]:
        """Return a deterministic reference to an existing artifact; copy nothing."""
        p = Path(path)
        if not p.is_file():
            raise ValueError(f"artifact is not a file: {p}")
        digest = hashlib.sha256()
        with p.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        resolved = p.resolve()
        try:
            stored_path = str(resolved.relative_to(self.root.resolve()))
        except ValueError:
            stored_path = str(resolved)
        return {
            "path": stored_path,
            "sha256": digest.hexdigest(),
            "size": p.stat().st_size,
        }
