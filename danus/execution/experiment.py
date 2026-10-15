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

Code, config and inputs resolve only from a verified frozen bundle. Each unit has
a writable artifact scratch directory; declared artifact argv values map there.
The scientific workspace is never exposed to unit commands.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import fcntl
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from danus.core.evidence import EvidenceStore, _content_id, protocol_identity
from danus.core.metrics import reduce_metrics, metrics_equal
from danus.core._util import append_jsonl, utc_now
from danus.core import bundle
from danus import sandbox

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


def _persist_evidence_report(root: Path, protocol_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Derive one reusable Evidence Packet from hash-checked frozen outputs.

    Numbers are copied from the artifact JSON or trusted reducer, never invented by
    an LLM. The report is a derived index, not independent empirical authority.
    """
    from danus.execution import layout as L
    from danus.execution.scaffold import atomic_write

    evidence_id = str(result["evidence_id"])
    store = EvidenceStore(root)
    record = store.get_record(evidence_id)
    protocol = store.get_protocol(protocol_id)
    if not record or not protocol or record.get("protocol_id") != protocol_id:
        raise ValueError("report requires the matching immutable evidence and protocol")
    checkpoint = _load_checkpoint(root, protocol_id)
    units = _units(protocol)
    completed = checkpoint.get("completed")
    if (not isinstance(completed, dict)
        or set(completed) != {u["id"] for u in units}
        or checkpoint.get("evidence_id") != evidence_id):
        raise ValueError("report checkpoint does not match evidence")
    refs = [r for u in units for r in completed[u["id"]]]
    if refs != record.get("artifacts") or not _refs_match(store, refs):
        raise ValueError("report raw artifacts do not match immutable evidence")
    allowed = (_runtime_dir(root, protocol_id) / "artifacts").resolve()
    cells: List[Dict[str, Any]] = []
    for unit in units:
        row: Dict[str, Any] = {"unit_id": unit["id"], "artifacts": []}
        for ref in completed[unit["id"]]:
            path = (root / str(ref["path"])).resolve(strict=True)
            if not path.is_relative_to(allowed):
                raise ValueError("report artifact escapes experiment runtime")
            scalars: Dict[str, Any] = {}
            if path.suffix == ".json" and path.stat().st_size <= 65536:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if isinstance(k, str) and len(k) <= 80 and (
                            type(v) in {int, float, bool} or
                            (isinstance(v, str) and len(v) <= 160)
                        ):
                            scalars[k] = v
            row["artifacts"].append({
                "ref": ref, "scalar_results": scalars,
            })
        cells.append(row)
    observed = [
        item["scalar_results"].get("improved")
        for row in cells for item in row["artifacts"]
        if type(item["scalar_results"].get("improved")) is bool
    ]
    comparison = (
        {"improved": sum(observed), "non_improving": len(observed) - sum(observed),
         "count_with_explicit_improved_flag": len(observed)}
        if observed else None
    )
    report = {
        "observed_comparison": comparison,
        "schema_version": 1,
        "evidence_id": evidence_id,
        "protocol_id": protocol_id,
        "question": protocol.get("question", ""),
        "hypothesis": protocol.get("hypothesis", ""),
        "decision_rule": protocol.get("decision_rule", ""),
        "primary_metrics": record.get("primary_metrics", {}),
        "matrix_coverage": record.get("matrix_coverage", {}),
        "failures": record.get("failures", []),
        "deviations": record.get("deviations", []),
        "cells": cells,
        "provenance_gaps": [
            name for name in ("code_commit", "config_ref", "data_ref")
            if not record.get(name)
        ],
        "epistemic_status": "OBSERVED; independent scientific verification is separate",
    }
    report_path = _runtime_dir(root, protocol_id) / "evidence_report.json"
    _write_json(report_path, report)
    lines = [
        "# Experiment Evidence Packet",
        "",
        f"Evidence ID: `{evidence_id}` | Frozen protocol: `{protocol_id}`",
        f"Question: {report['question']}",
        f"Pre-specified hypothesis: {report['hypothesis']}",
        f"Decision rule: {report['decision_rule']}",
        f"Coverage: {json.dumps(report['matrix_coverage'], ensure_ascii=False)}",
        f"Primary metrics: {json.dumps(report['primary_metrics'], ensure_ascii=False)}",
        f"Observed comparison (only explicit per-unit flags): {json.dumps(comparison)}",
        f"Failures: {json.dumps(report['failures'], ensure_ascii=False)}",
        f"Deviations: {json.dumps(report['deviations'], ensure_ascii=False)}",
        "",
        "## All frozen units (including negative and null outcomes)",
    ]
    for row in cells:
        for item in row["artifacts"]:
            lines.append(
                f"- {row['unit_id']}: "
                + json.dumps(item["scalar_results"], ensure_ascii=False, sort_keys=True)
                + f" [sha256={item['ref']['sha256']}]"
            )
    if report["provenance_gaps"]:
        lines.append("Missing provenance fields: " + ", ".join(report["provenance_gaps"]))
    lines.append(
        "This is a deterministic report of recorded observations, not a "
        "scientific interpretation or an independent confirmation."
    )
    md = "\n".join(lines) + "\n"
    workspace = L.scientific_workspace(root)
    shared_path = workspace / ".danus" / "experiment_reports" / f"{evidence_id}.md"
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(shared_path, md)
    return {
        "evidence_id": evidence_id,
        "report_path": str(shared_path),
        "report": {**report, "cells": cells[:20],
                   "omitted_cells_in_tool_response": max(0, len(cells) - 20)},
    }


def run_experiment(project_dir: Path, protocol_id: str, *, author: str = "experimenter", authorize=None) -> Dict[str, Any]:
    """Execute exactly one frozen protocol; one protocol has one live executor."""
    root = Path(project_dir).resolve()
    if not re.fullmatch(r"[0-9a-f]{16}", protocol_id):
        raise ValueError("invalid protocol_id")
    runtime_dir = _runtime_dir(root, protocol_id)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock = (runtime_dir / ".lock").open("w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"experiment protocol already running: {protocol_id}") from exc
        result = _run_experiment(root, protocol_id, author=author, lock_fd=lock.fileno(), authorize=authorize)
        with (authorize() if authorize else nullcontext()):
            packet = _persist_evidence_report(root, protocol_id, result)
        return {**result, **packet}
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            lock.close()


def _run_experiment(project_dir: Path, protocol_id: str, *, author: str, lock_fd: int, authorize=None) -> Dict[str, Any]:
    root = Path(project_dir).resolve()
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

    if not protocol.get("bundle_id"):
        raise ValueError("runnable protocol requires frozen execution bundle; freeze a new protocol")
    from danus.core import runtime
    runtime.validate(protocol)
    with bundle.snapshot(root, protocol["bundle_id"]) as (workspace, manifest):
        return _run_frozen(root, protocol_id, protocol, store, workspace, manifest, author, lock_fd, authorize)


def _run_frozen(root, protocol_id, protocol, store, workspace, manifest, author, lock_fd, authorize):
    from danus.core import runtime
    guard = authorize or nullcontext
    with guard():
        pass
    units = _units(protocol)
    names = {ref["path"] for ref in manifest["files"]}
    for unit in units:
        _validate_inputs(store, unit, workspace)
        for name in unit["artifacts"]:
            bundle.relative_path(name)
            if name in names:
                raise ValueError("artifact cannot replace a frozen execution file")
        # The interpreter is trusted runtime; every program file must be frozen.
        argv = unit["argv"]
        if argv[0] not in {"python", "python3", sys.executable} or len(argv) < 2 or argv[1] not in names:
            raise ValueError("unit argv must select Python and a declared frozen script")

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

    status_path = _status_path(root, protocol_id)
    if status_path.is_file():
        try:
            previous_status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous_status = None
        if isinstance(previous_status, dict) and previous_status.get("state") == "running":
            interrupted_unit = str(previous_status.get("current_unit") or "")
            if interrupted_unit and interrupted_unit not in completed:
                raise RuntimeError(
                    f"experiment unit {interrupted_unit} interrupted before safe point; "
                    "refusing automatic rerun"
                )

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
        with guard():
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
        with guard():
            _write_status(
                root, protocol_id, "running", len(completed), len(units),
                current_unit=unit_id,
                attempt=attempts,
                unit_started_at=unit_started_at,
                returncode=None,
                evidence_id=None,
                log=str(log_path.relative_to(root)),
            )
        scratch = _runtime_dir(root, protocol_id) / "artifacts" / unit_id
        scratch.mkdir(parents=True, exist_ok=True)
        scratch = sandbox._policy_path(scratch)
        outputs = {name: str(scratch / name) for name in unit["artifacts"]}
        argv = [sys.executable, "-B", str(workspace / unit["argv"][1]),
                *[outputs.get(arg, arg) for arg in unit["argv"][2:]]]
        runtime.validate(protocol)
        proc = sandbox.run_sandboxed(
            argv, cwd=workspace,
            readable_paths=[workspace, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()],
            writable_paths=[scratch], workspace_access="read", network=False, env={},
            pass_fds=(lock_fd,), authorize=authorize,
        )
        runtime.validate(protocol)
        log_path.write_text((proc.stdout or "") + (proc.stderr or ""), encoding="utf-8")
        if proc.returncode != 0:
            with guard():
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

        with guard():
            refs = []
            for raw_path in unit["artifacts"]:
                path = sandbox._policy_path(scratch / raw_path)
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

    with guard():
        _write_status(
            root, protocol_id, "finalizing", len(completed), len(units),
            current_unit=None, attempt=None, unit_started_at=None,
            returncode=0, evidence_id=None, log=None,
        )
    artifacts: List[Dict[str, Any]] = []
    for unit in units:
        artifacts.extend(completed[unit["id"]])
    primary_metrics = reduce_metrics(root, protocol, artifacts)
    with guard():
        evidence_id = store.add_record(
            protocol_id=protocol_id,
            author=author,
            code_commit="",
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
