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


def _checker_receipt(store, task_id, proposal, owned, source):
    """Corroborate an approved --check run using HOST-owned Codex tool events.

    A worker-authored run_log can disprove success, but cannot prove execution.
    This gate confirms the entrypoint was actually invoked and exited zero;
    scientific independence and checker semantics still require Reviewer audit.
    """
    plan = proposal.get("content", "")
    if "--check" not in plan or "run_log.json" not in plan:
        return None
    result = {"id": "checker_execution", "kind": "checker_execution"}
    def outcome(status, detail):
        return {**result, "status": status, "detail": detail}

    candidates = re.findall(r"\bpython(?:3)?\s+(?:-B\s+)?([\w./-]+/run\.py)\b", plan)
    scripts = {p for p in candidates if p in owned}
    if len(scripts) != 1:
        return outcome("INCOMPLETE", "checker contract has no unique registered new entrypoint")
    script = scripts.pop()
    logs = [p for p in owned if p.endswith("/run_log.json")]
    if len(logs) != 1:
        return outcome("INCOMPLETE", "checker protocol requires one fresh registered run_log.json")
    try:
        runlog = json.loads(source(logs[0]).read_text(encoding="utf-8"))
    except (ValueError, OSError, UnicodeError) as exc:
        return outcome("FAILED", f"run_log.json cannot be read: {exc}"[:1000])

    exit_markers, failures = [], []
    def scan(obj, context=""):
        if isinstance(obj, dict):
            for key, value in obj.items():
                name = str(key).lower()
                full = context + "." + name
                if (any(part in full for part in ("check", "replay", "parent"))
                        and ("exit" in name or "returncode" in name
                             or "return_code" in name)):
                    exit_markers.append(full)
                    if type(value) is not int or value != 0:
                        failures.append(f"{full}={value!r}")
                if (any(part in full for part in ("check", "replay", "parent"))
                        and "status" in name and isinstance(value, str)
                        and value.upper() in ("FAILED", "INCOMPLETE", "ERROR")):
                    failures.append(f"{full}={value!r}")
                scan(value, full)
        elif isinstance(obj, list):
            for value in obj:
                scan(value, context)
    scan(runlog)
    if failures:
        return outcome("FAILED", "checker/parent reported non-success: " + ", ".join(failures)[:800])
    if not exit_markers:
        return outcome("INCOMPLETE", "run_log.json lacks numeric checker/parent exit evidence")

    # Codex --json output belongs to the host log directory, NOT to the
    # Experimenter's writable workspace. Do not accept claimed counts alone.
    traces = sorted((store.root / "logs").glob(task_id + "-*.jsonl"))
    if len(traces) != 1:
        return outcome("INCOMPLETE", "missing or ambiguous host-owned Codex execution trace")
    observed = []
    with traces[0].open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") if event.get("type") == "item.completed" else None
            if not isinstance(item, dict) or item.get("type") != "command_execution":
                continue
            cmd = item.get("command", "")
            if (isinstance(cmd, str) and script in cmd
                    and re.search(r"\bpython(?:3)?\b", cmd)):
                observed.append(item.get("exit_code"))
    if not observed or any(type(code) is not int for code in observed):
        return outcome("INCOMPLETE", "host trace has no completed entrypoint command with exit code")
    if any(code != 0 for code in observed):
        return outcome("FAILED", f"host-observed entrypoint exit codes: {observed}")
    return outcome("PASSED", "host observed approved entrypoint exit 0; "
                   "run log reports checker/parent exit 0; semantics require Reviewer")


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
    receipt = _checker_receipt(store, task_id, proposal, owned, source)
    if receipt is not None:
        results.append(receipt)
    if any(r["status"] == "FAILED" for r in results):
        status = "FAILED"
    elif any(r["status"] == "INCOMPLETE" for r in results):
        status = "INCOMPLETE"
    else:
        status = "PASSED"
    return {"status": status, "checks": results,
            "reason": "; ".join(f"{r['id']}: {r['detail']}" for r in results
                              if r["status"] != "PASSED")[:2000]}
