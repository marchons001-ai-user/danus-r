"""Small deterministic gate for V8 experiment evidence.

A completed Worker is NOT a validated experiment. Only these host-side checks
can write a validation verdict. Domain-specific checks must be supported by an
approved executable verifier; a model's own PASS flags are not evidence.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from pathlib import Path

KINDS = {"asset_integrity", "csv_rows", "csv_mean", "v7_evidence"}
_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def normalize_checks(raw):
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 16:
        raise ValueError("required_checks must be a list of at most 16 checks")
    checks, names = [], set()
    for item in raw:
        if not isinstance(item, dict) or not _ID.fullmatch(str(item.get("id", ""))):
            raise ValueError("each required check needs a stable lowercase id")
        name, kind = item["id"], item.get("kind")
        if name in names or kind not in KINDS:
            raise ValueError("duplicate check id or unsupported check kind")
        names.add(name)
        allowed = {
            "asset_integrity": {"id", "kind"},
            "csv_rows": {"id", "kind", "path", "expected_rows"},
            "csv_mean": {"id", "kind", "path", "column", "report_path",
                         "report_key", "abs_tolerance"},
            "v7_evidence": {"id", "kind", "evidence_ids", "scope", "protocol_id"},
        }[kind]
        if set(item) != allowed:
            raise ValueError(f"{name}: check needs exactly {sorted(allowed)}")
        if kind == "csv_rows":
            if not isinstance(item["expected_rows"], int) or isinstance(
                    item["expected_rows"], bool) or item["expected_rows"] < 1:
                raise ValueError("csv_rows requires positive expected_rows")
        if kind == "csv_mean":
            tolerance = item["abs_tolerance"]
            if not isinstance(tolerance, (int, float)) or isinstance(
                    tolerance, bool) or not math.isfinite(tolerance) or tolerance < 0:
                raise ValueError("csv_mean requires finite nonnegative tolerance")
        if kind == "v7_evidence":
            ids = item["evidence_ids"]
            if (not isinstance(ids, list) or not ids
                    or not all(isinstance(x, str) and re.fullmatch(r"[0-9a-f]{16}", x)
                               for x in ids)
                    or not isinstance(item["scope"], dict) or not item["scope"]
                    or not re.fullmatch(r"[0-9a-f]{16}", str(item["protocol_id"]))):
                raise ValueError("v7_evidence requires pinned record IDs, scope and protocol ID")
        for field in ("path", "report_path"):
            if field in item and (not isinstance(item[field], str)
                                  or not item[field].strip()
                                  or Path(item[field]).is_absolute()
                                  or ".." in Path(item[field]).parts):
                raise ValueError("check paths must be safe workspace-relative paths")
        for field in ("column", "report_key"):
            if field in item and (not isinstance(item[field], str)
                                  or not item[field].strip()):
                raise ValueError("csv_mean needs column and report_key")
        checks.append(dict(item))
    return checks


def run_gate(store, task_id, proposal, assets):
    """Read only frozen approved checks and freshly registered output bytes."""
    checks = proposal.get("required_checks") or []
    owned = {p: a for p, a in assets.items() if a["task_id"] == task_id}
    results = []
    if not checks:
        return {"status": "INCOMPLETE", "checks": [],
                "reason": "Approved proposal has no executable mandatory validation checks"}
    if not owned:
        return {"status": "INCOMPLETE", "checks": [],
                "reason": "Experiment registered no new output artifacts"}

    def source(path):
        if path not in owned:
            raise FileNotFoundError(f"required fresh artifact not registered: {path}")
        base = (store.root / "workspace").resolve()
        actual = (base / path).resolve()
        if not actual.is_relative_to(base) or not actual.is_file():
            raise FileNotFoundError(f"required artifact missing: {path}")
        expected = owned[path]
        data = actual.read_bytes()
        if (len(data) != expected["bytes"] or
                hashlib.sha256(data).hexdigest() != expected["sha256"]):
            raise ValueError(f"registered artifact changed: {path}")
        return actual

    for check in checks:
        name, kind = check["id"], check["kind"]
        try:
            if kind == "asset_integrity":
                # Every newly registered output must still match the executor
                # transaction's SHA-256 and byte size.
                for path in owned:
                    source(path)
                detail = f"checked {len(owned)} freshly registered output files"
            elif kind == "csv_rows":
                path = source(check["path"])
                with path.open(newline="", encoding="utf-8") as f:
                    count = sum(1 for _ in csv.DictReader(f))
                if count != check["expected_rows"]:
                    raise ValueError(f"row count {count} != {check['expected_rows']}")
                detail = f"{check['path']}: {count} data rows"
            elif kind == "csv_mean":
                data_path = source(check["path"])
                report_path = source(check["report_path"])
                with data_path.open(newline="", encoding="utf-8") as f:
                    values = [float(row[check["column"]]) for row in csv.DictReader(f)]
                if not values or not all(math.isfinite(v) for v in values):
                    raise ValueError("CSV mean requires finite nonempty numeric values")
                actual = math.fsum(values) / len(values)
                report = json.loads(report_path.read_text(encoding="utf-8"))
                claimed = report[check["report_key"]]
                if not isinstance(claimed, (int, float)) or not math.isfinite(claimed):
                    raise ValueError("report metric must be finite numeric")
                if abs(actual - claimed) > check["abs_tolerance"]:
                    raise ValueError(f"metric mismatch: recomputed {actual}, reported {claimed}")
                detail = f"{check['column']} independently recomputed over {len(values)} rows"
            elif kind == "v7_evidence":
                # Reuse V7's deterministic evidence checker, not its Plan DAG.
                # The approved V7 protocol and immutable records must already
                # exist. This does not certify bespoke scientific invariants.
                from danus.verify.empirical import check_empirical_evidence
                report = check_empirical_evidence(
                    store.root, scope=check["scope"], evidence_refs=check["evidence_ids"])
                if not report["passed"]:
                    raise ValueError("; ".join(report["errors"])[:1000])
                if any(rec.get("protocol_id") != check["protocol_id"]
                       for rec in report["evidence_records"]):
                    raise ValueError("V7 evidence does not match approved protocol identity")
                detail = f"V7 verified {len(report['evidence_records'])} pinned evidence records"
            results.append({"id": name, "kind": kind, "status": "PASSED", "detail": detail})
        except (FileNotFoundError, KeyError) as exc:
            results.append({"id": name, "kind": kind, "status": "INCOMPLETE",
                            "detail": f"{type(exc).__name__}: {exc}"[:1000]})
        except Exception as exc:
            results.append({"id": name, "kind": kind, "status": "FAILED",
                            "detail": f"{type(exc).__name__}: {exc}"[:1000]})
    if any(r["status"] == "FAILED" for r in results):
        status = "FAILED"
    elif any(r["status"] == "INCOMPLETE" for r in results):
        status = "INCOMPLETE"
    else:
        status = "PASSED"
    return {"status": status, "checks": results,
            "reason": "; ".join(f"{r['id']}: {r['detail']}" for r in results
                              if r["status"] != "PASSED")[:2000]}
