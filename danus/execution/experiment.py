"""Deterministic frozen-protocol Experiment Runtime with safe-point checkpointing.

A runnable protocol uses ``matrix.units``. Each unit is one smallest restartable
work item::

    {"id": "case-1", "argv": ["python", "run.py", "case-1"],
     "artifacts": ["runs/case-1.json"]}

The runtime never invents or expands this matrix. A unit is checkpointed only after
its command succeeds and every declared artifact has been hashed. On restart,
completed units are skipped after their artifact references are revalidated. The
shared metric reducer in ``danus.core.metrics`` computes final primary metrics and
the immutable Evidence Record.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from danus.core.evidence import EvidenceStore
from danus.core.metrics import reduce_metrics
from danus.core._util import utc_now

_UNIT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _runtime_dir(root: Path, protocol_id: str) -> Path:
    return Path(root) / "experiment_runtime" / protocol_id


def _checkpoint_path(root: Path, protocol_id: str) -> Path:
    return _runtime_dir(root, protocol_id) / "checkpoint.json"


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _load_checkpoint(root: Path, protocol_id: str) -> Dict[str, Any]:
    path = _checkpoint_path(root, protocol_id)
    if not path.is_file():
        return {
            "schema_version": 1,
            "protocol_id": protocol_id,
            "completed": {},
            "last_failure": None,
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
    return obj


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
        seen.add(unit_id)
        out.append({"id": unit_id, "argv": list(argv), "artifacts": list(artifacts)})
    if not out:
        raise ValueError("runnable protocol requires at least one matrix unit")
    return out


def _resolve(root: Path, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else Path(root) / path


def _refs_match(store: EvidenceStore, refs: Any) -> bool:
    if not isinstance(refs, list) or not refs:
        return False
    for ref in refs:
        if not isinstance(ref, dict) or not str(ref.get("path") or ""):
            return False
        path = _resolve(store.root, str(ref["path"]))
        try:
            current = store.artifact_ref(path)
        except ValueError:
            return False
        if current != ref:
            return False
    return True


def run_experiment(project_dir: Path, protocol_id: str, *, author: str = "experimenter") -> Dict[str, Any]:
    """Execute exactly one frozen protocol and return its immutable Evidence id."""
    root = Path(project_dir).resolve()
    store = EvidenceStore(root)
    protocol = store.get_protocol(protocol_id)
    if protocol is None:
        raise ValueError(f"unknown protocol_id: {protocol_id}")
    if protocol.get("status") != "frozen":
        raise ValueError("Experiment Runtime accepts frozen protocols only")
    if not isinstance(protocol.get("metric_reducer"), dict):
        raise ValueError("runnable protocol requires metric_reducer")

    units = _units(protocol)
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
        return {
            "protocol_id": protocol_id,
            "evidence_id": str(evidence_id),
            "primary_metrics": record.get("primary_metrics", {}),
            "completed_units": len(completed),
        }

    runtime_dir = _runtime_dir(root, protocol_id)
    logs_dir = runtime_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    for unit in units:
        unit_id = unit["id"]
        if unit_id in completed:
            continue
        log_path = logs_dir / f"{unit_id}.log"
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.run(
                unit["argv"],
                cwd=str(root),
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if proc.returncode != 0:
            checkpoint["last_failure"] = {
                "unit_id": unit_id,
                "returncode": int(proc.returncode),
                "log": str(log_path.relative_to(root)),
            }
            checkpoint["updated_at"] = utc_now()
            _write_json(_checkpoint_path(root, protocol_id), checkpoint)
            raise RuntimeError(f"experiment unit {unit_id} failed with rc={proc.returncode}")

        refs = []
        for raw_path in unit["artifacts"]:
            path = _resolve(root, raw_path)
            refs.append(store.artifact_ref(path))
        completed[unit_id] = refs
        checkpoint["last_failure"] = None
        checkpoint["updated_at"] = utc_now()
        _write_json(_checkpoint_path(root, protocol_id), checkpoint)

    artifacts: List[Dict[str, Any]] = []
    for unit in units:
        artifacts.extend(completed[unit["id"]])
    primary_metrics = reduce_metrics(root, protocol, artifacts)
    evidence_id = store.add_record(
        protocol_id=protocol_id,
        author=author,
        matrix_coverage={
            "planned": len(units),
            "completed": len(completed),
            "units": [unit["id"] for unit in units],
        },
        primary_metrics=primary_metrics,
        failures=[],
        deviations=[],
        artifacts=artifacts,
    )
    checkpoint["evidence_id"] = evidence_id
    checkpoint["updated_at"] = utc_now()
    _write_json(_checkpoint_path(root, protocol_id), checkpoint)
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
