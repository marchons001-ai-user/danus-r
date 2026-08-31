"""Deterministic frozen-protocol Experiment Runtime with safe-point checkpointing.

A runnable protocol uses ``matrix.units``. Each unit is one smallest restartable
work item::

    {"id": "case-1", "argv": ["python", "run.py", "case-1"],
     "artifacts": ["runs/case-1.json"], "inputs": [{...content ref...}]}

The runtime never invents or expands this matrix. A unit is checkpointed only after
its command succeeds and every declared artifact has been hashed. On restart,
completed units are skipped after their artifact references are revalidated. The
shared metric reducer in ``danus.core.metrics`` computes final primary metrics and
the immutable Evidence Record.

For a workspace-bound project, control state remains under the Danus project root
while unit commands and relative scientific paths resolve from the bound workspace.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from danus.core.evidence import EvidenceStore, _content_id, protocol_identity
from danus.core.metrics import metrics_equal, reduce_metrics
from danus.core._util import append_jsonl, utc_now
from danus.execution import layout as L

_UNIT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _runtime_dir(root: Path, protocol_id: str) -> Path:
    return Path(root) / "experiment_runtime" / protocol_id


def _checkpoint_path(root: Path, protocol_id: str) -> Path:
    return _runtime_dir(root, protocol_id) / "checkpoint.json"


def _status_path(root: Path, protocol_id: str) -> Path:
    return _runtime_dir(root, protocol_id) / "status.json"


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _write_status(
    root: Path,
    protocol_id: str,
    state: str,
    completed_units: int,
    total_units: int,
    **details: Any,
) -> None:
    path = _status_path(root, protocol_id)
    previous: Dict[str, Any] = {}
    if path.is_file():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old = None
        if isinstance(old, dict):
            previous = old

    now = utc_now()
    payload: Dict[str, Any] = {
        "protocol_id": protocol_id,
        "state": state,
        "completed_units": completed_units,
        "total_units": total_units,
        "updated_at": now,
    }
    payload.update(details)
    _write_json(path, payload)

    kind = None
    if state == "running" and payload.get("current_unit"):
        if (
            previous.get("current_unit") != payload.get("current_unit")
            or previous.get("attempt") != payload.get("attempt")
        ):
            kind = "UNIT_STARTED"
    elif state == "running" and previous.get("state") == "running" and previous.get("current_unit"):
        kind = "UNIT_COMPLETED"
    elif state == "failed":
        kind = "UNIT_FAILED"
    elif state == "finalizing" and previous.get("state") != "finalizing":
        kind = "FINALIZING"
    elif state == "complete" and previous.get("state") != "complete":
        kind = "COMPLETE"

    if kind:
        append_jsonl(
            _runtime_dir(root, protocol_id) / "events.jsonl",
            {
                "event": kind,
                "protocol_id": protocol_id,
                "state": state,
                "completed_units": completed_units,
                "total_units": total_units,
                "unit_id": payload.get("current_unit") or previous.get("current_unit"),
                "attempt": payload.get("attempt") or previous.get("attempt"),
                "returncode": payload.get("returncode"),
                "evidence_id": payload.get("evidence_id"),
                "at": now,
            },
        )


def _load_checkpoint(root: Path, protocol_id: str) -> Dict[str, Any]:
    path = _checkpoint_path(root, protocol_id)
    if not path.is_file():
        return {
            "schema_version": 1,
            "protocol_id": protocol_id,
            "completed": {},
            "failures": [],
            "evidence_id": None,
        }
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid experiment checkpoint: {path}") from exc
    if not isinstance(obj, dict) or obj.get("schema_version") != 1:
        raise ValueError("unsupported experiment checkpoint schema")
    if obj.get("protocol_id") != protocol_id:
        raise ValueError("experiment checkpoint protocol_id mismatch")
    if not isinstance(obj.get("completed"), dict):
        raise ValueError("experiment checkpoint completed must be an object")
    if not isinstance(obj.get("failures", []), list):
        raise ValueError("experiment checkpoint failures must be a list")
    obj.setdefault("failures", [])
    return obj


def _content_ref(raw: Any, unit_id: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"unit {unit_id} input reference must be an object")
    path = str(raw.get("path") or "")
    digest = str(raw.get("sha256") or "")
    size = raw.get("size")
    if not path or not digest or not isinstance(size, int):
        raise ValueError(f"unit {unit_id} input reference requires path/sha256/size")
    return {"path": path, "sha256": digest, "size": size}


def _units(protocol: Dict[str, Any]) -> List[Dict[str, Any]]:
    matrix = protocol.get("matrix")
    if not isinstance(matrix, dict) or not isinstance(matrix.get("units"), list):
        raise ValueError("runnable protocol requires matrix.units")
    out: List[Dict[str, Any]] = []
    seen = set()
    for raw in matrix["units"]:
        if not isinstance(raw, dict):
            raise ValueError("each matrix unit must be an object")
        unit_id = str(raw.get("id") or "")
        argv = raw.get("argv")
        artifacts = raw.get("artifacts")
        inputs = raw.get("inputs", [])
        if not _UNIT_ID_RE.match(unit_id):
            raise ValueError(f"invalid experiment unit id: {unit_id!r}")
        if unit_id in seen:
            raise ValueError(f"duplicate experiment unit id: {unit_id}")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise ValueError(f"unit {unit_id} requires non-empty string argv")
        if not isinstance(artifacts, list) or not artifacts or not all(
            isinstance(x, str) and x for x in artifacts
        ):
            raise ValueError(f"unit {unit_id} requires declared artifact paths")
        if not isinstance(inputs, list):
            raise ValueError(f"unit {unit_id} inputs must be a list")
        seen.add(unit_id)
        out.append({
            "id": unit_id,
            "argv": list(argv),
            "artifacts": list(artifacts),
            "inputs": [_content_ref(ref, unit_id) for ref in inputs],
        })
    if not out:
        raise ValueError("runnable protocol requires at least one matrix unit")
    return out


def _resolve(root: Path, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else Path(root) / path


def _refs_match(store: EvidenceStore, refs: Any, *, path_root: Path | None = None) -> bool:
    if not isinstance(refs, list) or not refs:
        return False
    base = Path(path_root) if path_root is not None else store.root
    for ref in refs:
        if not isinstance(ref, dict) or not str(ref.get("path") or ""):
            return False
        path = _resolve(base, str(ref["path"]))
        try:
            current = store.artifact_ref(path)
        except ValueError:
            return False
        if current.get("sha256") != ref.get("sha256") or current.get("size") != ref.get("size"):
            return False
    return True


def _validate_inputs(store: EvidenceStore, unit: Dict[str, Any], workspace: Path) -> None:
    refs = unit.get("inputs") or []
    if refs and not _refs_match(store, refs, path_root=workspace):
        raise ValueError(f"frozen unit input changed: {unit['id']}")


def _is_within(path: Path, root: Path) -> bool:
    path = path.resolve()
    root = root.resolve()
    return path == root or (root.is_dir() and root in path.parents)


def _validate_artifact_paths(workspace: Path, readonly: List[Path], unit: Dict[str, Any]) -> None:
    for raw in unit["artifacts"]:
        path = _resolve(workspace, raw).resolve()
        if any(_is_within(path, root) for root in readonly):
            raise ValueError(f"unit {unit['id']} artifact is inside read-only input: {path}")


def _git_commit(root: Path) -> str:
    """Best-effort provenance only; execution never depends on git being present."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True,
            text=True, check=False,
        )
    except OSError:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def run_experiment(project_dir: Path, protocol_id: str, *, author: str = "experimenter") -> Dict[str, Any]:
    """Execute exactly one frozen protocol; one protocol has one live executor."""
    root = Path(project_dir).resolve()
    runtime_dir = _runtime_dir(root, protocol_id)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock = (runtime_dir / ".lock").open("w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"experiment protocol already running: {protocol_id}") from exc
        return _run_experiment(root, protocol_id, author=author)
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            lock.close()


def _run_experiment(project_dir: Path, protocol_id: str, *, author: str) -> Dict[str, Any]:
    root = Path(project_dir).resolve()
    workspace = L.scientific_workspace(root)
    if not workspace.is_dir():
        raise ValueError(f"scientific workspace missing: {workspace}")
    readonly = L.readonly_inputs(root)
    store = EvidenceStore(root)
    protocol = store.get_protocol(protocol_id)
    if protocol is None:
        raise ValueError(f"unknown protocol_id: {protocol_id}")
    if (
        protocol.get("protocol_id") != protocol_id
        or _content_id(protocol_identity(protocol)) != protocol_id
    ):
        raise ValueError("frozen protocol identity mismatch")
    if protocol.get("status") != "frozen":
        raise ValueError("Experiment Runtime accepts frozen protocols only")
    if not isinstance(protocol.get("metric_reducer"), dict):
        raise ValueError("runnable protocol requires metric_reducer")

    units = _units(protocol)
    for unit in units:
        _validate_inputs(store, unit, workspace)
        _validate_artifact_paths(workspace, readonly, unit)

    checkpoint = _load_checkpoint(root, protocol_id)
    completed = checkpoint["completed"]

    # A checkpoint is trusted only while every already-completed raw artifact still
    # matches the hash/size captured at its safe point. Never silently rerun or
    # overwrite a changed completed result.
    unit_ids = {unit["id"] for unit in units}
    unknown = set(completed) - unit_ids
    if unknown:
        raise ValueError(f"checkpoint contains units outside frozen matrix: {sorted(unknown)}")
    for unit_id, refs in completed.items():
        if not _refs_match(store, refs):
            raise ValueError(f"completed unit artifact changed: {unit_id}")

    evidence_id = checkpoint.get("evidence_id")
    if evidence_id:
        record = store.get_record(str(evidence_id))
        if record is None or record.get("protocol_id") != protocol_id:
            raise ValueError("checkpoint references missing or mismatched Evidence Record")
        artifacts = [ref for unit in units for ref in completed.get(unit["id"], [])]
        if record.get("artifacts") != artifacts:
            raise ValueError("completed Evidence artifacts do not match checkpoint")
        recomputed = reduce_metrics(root, protocol, artifacts)
        if not metrics_equal(record.get("primary_metrics"), recomputed):
            raise ValueError("completed Evidence primary_metrics do not match raw artifacts")
        _write_status(
            root, protocol_id, "complete", len(completed), len(units),
            current_unit=None, attempt=None, unit_started_at=None,
            returncode=0, evidence_id=str(evidence_id),
        )
        return {
            "protocol_id": protocol_id,
            "evidence_id": str(evidence_id),
            "primary_metrics": recomputed,
            "completed_units": len(completed),
        }

    logs_dir = _runtime_dir(root, protocol_id) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    for unit in units:
        unit_id = unit["id"]
        if unit_id in completed:
            continue
        _validate_inputs(store, unit, workspace)
        attempts = sum(
            1 for failure in checkpoint["failures"]
            if isinstance(failure, dict) and failure.get("unit_id") == unit_id
        ) + 1
        log_path = logs_dir / f"{unit_id}.attempt{attempts}.log"
        unit_started_at = utc_now()
        _write_status(
            root, protocol_id, "running", len(completed), len(units),
            current_unit=unit_id,
            attempt=attempts,
            unit_started_at=unit_started_at,
            returncode=None,
            evidence_id=None,
            log=str(log_path.relative_to(root)),
        )
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.run(
                unit["argv"],
                cwd=str(workspace),
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        # Do not accept a safe point if its frozen code/config/input changed while
        # the unit was executing; the output may no longer represent this protocol.
        _validate_inputs(store, unit, workspace)
        if proc.returncode != 0:
            checkpoint["failures"].append({
                "unit_id": unit_id,
                "attempt": attempts,
                "returncode": int(proc.returncode),
                "log": str(log_path.relative_to(root)),
            })
            checkpoint["updated_at"] = utc_now()
            _write_json(_checkpoint_path(root, protocol_id), checkpoint)
            _write_status(
                root, protocol_id, "failed", len(completed), len(units),
                current_unit=unit_id,
                attempt=attempts,
                unit_started_at=unit_started_at,
                returncode=int(proc.returncode),
                evidence_id=None,
                log=str(log_path.relative_to(root)),
            )
            raise RuntimeError(f"experiment unit {unit_id} failed with rc={proc.returncode}")

        refs = []
        for raw_path in unit["artifacts"]:
            path = _resolve(workspace, raw_path)
            refs.append(store.artifact_ref(path))
        completed[unit_id] = refs
        checkpoint["updated_at"] = utc_now()
        _write_json(_checkpoint_path(root, protocol_id), checkpoint)
        _write_status(
            root, protocol_id, "running", len(completed), len(units),
            current_unit=None,
            attempt=None,
            unit_started_at=None,
            returncode=0,
            evidence_id=None,
            log=None,
        )

    _write_status(
        root, protocol_id, "finalizing", len(completed), len(units),
        current_unit=None, attempt=None, unit_started_at=None,
        returncode=0, evidence_id=None, log=None,
    )
    artifacts: List[Dict[str, Any]] = []
    for unit in units:
        artifacts.extend(completed[unit["id"]])
    primary_metrics = reduce_metrics(root, protocol, artifacts)
    evidence_id = store.add_record(
        protocol_id=protocol_id,
        author=author,
        code_commit=_git_commit(workspace),
        matrix_coverage={
            "planned": len(units),
            "completed": len(completed),
            "units": [unit["id"] for unit in units],
        },
        primary_metrics=primary_metrics,
        failures=list(checkpoint["failures"]),
        deviations=[],
        artifacts=artifacts,
    )
    checkpoint["evidence_id"] = evidence_id
    checkpoint["updated_at"] = utc_now()
    _write_json(_checkpoint_path(root, protocol_id), checkpoint)
    _write_status(
        root, protocol_id, "complete", len(completed), len(units),
        current_unit=None, attempt=None, unit_started_at=None,
        returncode=0, evidence_id=evidence_id, log=None,
    )
    return {
        "protocol_id": protocol_id,
        "evidence_id": evidence_id,
        "primary_metrics": primary_metrics,
        "completed_units": len(completed),
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one frozen Danus-R experiment protocol")
    parser.add_argument("project_dir")
    parser.add_argument("protocol_id")
    parser.add_argument("--author", default="experimenter")
    args = parser.parse_args(argv)
    result = run_experiment(Path(args.project_dir), args.protocol_id, author=args.author)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())