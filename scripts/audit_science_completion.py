#!/usr/bin/env python3
"""Read-only scientific completion audit; write a separate derived report.

Does NOT modify the Plan, ProgressLedger, Handoffs, protocol or Evidence.
Raw artifacts are never overwritten. Optional metric recomputation invokes the
official sandboxed reducer, not a training run.

Usage:
  python scripts/audit_science_completion.py PROJECT PROTOCOL_ID EVIDENCE_ID --recompute-metrics
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.evidence import EvidenceStore
from danus.core.handoff import HandoffRequests
from danus.core.metrics import metrics_equal, reduce_metrics
from danus.execution import layout as L
from danus.orchestration.control import reconcile


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_version() -> dict:
    def run(*args):
        try:
            return subprocess.check_output(
                ["git", *args], text=True, stderr=subprocess.DEVNULL
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return ""
    return {"head": run("rev-parse", "HEAD"),
            "branch": run("branch", "--show-current"),
            "dirty": bool(run("status", "--porcelain"))}


def inspect(project: str, protocol_id: str, evidence_id: str, *, recompute: bool):
    root = L.project_dir(project).resolve(strict=True)
    workspace = L.scientific_workspace(root)
    plan = ResearchPlanGraph(root).snapshot()
    progress = ProgressLedger(root)
    store = EvidenceStore(root)
    errors = []

    def check(ok: bool, note: str) -> None:
        if not ok:
            errors.append(note)

    protocol = store.get_protocol(protocol_id)
    record = store.get_record(evidence_id)
    if not protocol or not record:
        raise ValueError("requested immutable protocol or Evidence Record not found")

    check(protocol.get("protocol_id") == protocol_id, "protocol identity mismatch")
    check(protocol.get("status") == "frozen", "protocol is not frozen")
    check(record.get("evidence_id") == evidence_id, "evidence identity mismatch")
    check(record.get("protocol_id") == protocol_id, "evidence references another protocol")
    primary = record.get("primary_metrics") or {}
    declared = protocol.get("primary_metrics") or []
    check(isinstance(primary, dict) and set(primary) == set(declared),
          "primary metric names differ from frozen protocol")
    for key, value in primary.items():
        check(type(value) in (int, float) and math.isfinite(value),
              "invalid primary metric " + str(key))

    raw_refs = record.get("artifacts") or []
    verified = []
    for i, ref in enumerate(raw_refs):
        name = str(ref.get("path") or "")
        try:
            path = Path(name)
            path = (path if path.is_absolute() else root / path).resolve(strict=True)
            if not path.is_relative_to(root):
                raise ValueError("artifact outside controlled project root")
            actual = {"path": name, "size": path.stat().st_size, "sha256": _sha256(path)}
            good = (actual["size"] == ref.get("size")
                    and actual["sha256"] == ref.get("sha256"))
            check(good, f"artifact {i} hash/size mismatch: {name}")
            verified.append({**actual, "valid": good})
        except (OSError, ValueError) as exc:
            check(False, f"artifact {i} unreadable: {name}: {exc}")
            verified.append({"path": name, "valid": False, "error": str(exc)})
    check(bool(raw_refs), "record has no raw artifacts")

    checkpoint_path = root / "experiment_runtime" / protocol_id / "checkpoint.json"
    checkpoint = {}
    if checkpoint_path.is_file():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            check(False, f"checkpoint unreadable: {exc}")
    else:
        check(False, "checkpoint missing")
    check(checkpoint.get("evidence_id") == evidence_id, "checkpoint evidence does not match")
    coverage = record.get("matrix_coverage") or {}
    completed = checkpoint.get("completed")
    if isinstance(completed, dict):
        check(len(completed) == coverage.get("completed"), "checkpoint and Evidence coverage mismatch")

    report_md = workspace / ".danus" / "experiment_reports" / f"{evidence_id}.md"
    check(report_md.is_file(), "shared Evidence Packet markdown missing")
    reviewer = [a for a in progress.assignments()
                if a.get("node_id") == "V3" and a.get("role") == "reviewer"]
    reviewer.sort(key=lambda a: str(a.get("assignment_id") or ""))
    reviewer = [a for a in reviewer if a.get("state") == "DONE"]
    review = reviewer[-1] if reviewer else {}
    check(bool(review), "V3 completed Reviewer assignment not found")

    handoffs = [
        {"id": h["handoff_id"], "kind": h["kind"], "subject": h.get("subject_id"),
         "target": h.get("target_role"), "status": h.get("status"),
         "request": h.get("request"), "reason": h.get("reason")}
        for h in HandoffRequests(progress).all()
        if h.get("status") in {"OPEN", "ASSIGNED"}
    ]
    state = reconcile(root, [])
    check(state.get("scientific_complete") is True, "scientific Plan is not complete")
    checked_metrics = None
    if recompute and not errors:
        try:
            checked_metrics = reduce_metrics(root, protocol, raw_refs)
            check(metrics_equal(primary, checked_metrics),
                  "recomputed metrics differ from immutable Evidence")
        except Exception as exc:
            check(False, f"metric recomputation error: {type(exc).__name__}: {exc}")
    elif recompute and errors:
        check(False, "recomputation skipped because integrity checks already failed")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit = {
        "audit_time_utc": stamp,
        "project": project, "plan_version": plan.get("plan_version"),
        "project_state": state["project_state"],
        "scientific_complete": state["scientific_complete"],
        "completed_nodes": sorted(progress.completed_nodes()),
        "pending_main_escalations": state["pending_main_escalations"],
        "pending_handoffs": handoffs,
        "protocol_id": protocol_id, "bundle_id": protocol.get("bundle_id"),
        "evidence_id": evidence_id, "metric_names": declared,
        "primary_metrics": primary, "recomputed_metrics": checked_metrics,
        "matrix_coverage": coverage, "failures": record.get("failures"),
        "deviations": record.get("deviations"), "artifact_checks": verified,
        "evidence_packet": str(report_md) if report_md.is_file() else None,
        "evidence_packet_sha256": _sha256(report_md) if report_md.is_file() else None,
        "v3_reviewer_assignment_id": review.get("assignment_id"),
        "v3_reviewer_outcome": review.get("outcome"),
        "v3_reviewer_result": review.get("result"),
        "git": _git_version(), "errors": errors,
        "note": "Derived audit only; original scientific and governance records are immutable.",
    }
    # A separate, short report filename outside protected control state.
    out = workspace / "reports"
    out.mkdir(parents=True, exist_ok=True)
    stem = f"science_completion_audit_{stamp}"
    json_path = out / f"{stem}.json"
    md_path = out / f"{stem}.md"
    lines = [
        "# Danus-R completed-science evidence audit", "",
        f"Time UTC: {stamp}",
        f"Project: {project} | Plan v{plan['plan_version']} | state: {state['project_state']}",
        f"Frozen protocol: \`{protocol_id}\` | Evidence: \`{evidence_id}\`",
        f"Evidence Packet: {report_md}", "",
        "## Actual recorded metrics", "",
        "\`\`\`json", json.dumps(primary, indent=2, ensure_ascii=False),
        "\`\`\`", "",
        f"Independent recomputation: {json.dumps(checked_metrics, ensure_ascii=False)}",
        f"Coverage: {json.dumps(coverage, ensure_ascii=False)}",
        f"Raw artifact files: {len(verified)}; hash pass: {sum(bool(v.get('valid')) for v in verified)}",
        f"Errors: {json.dumps(errors, ensure_ascii=False)}", "",
        "## Reviewer V3 recorded decision", "",
        str(review.get("result") or "No completed V3 review"), "",
        "## Outstanding operator requests (kept OPEN)", "",
    ]
    for h in handoffs:
        lines.append(f"- {h['id']} | {h['kind']} | {h['status']} | {h['target']}: {h['reason']}")
    lines += ["", "Derived report only; negative results and unresolved Main requests are not erased.", ""]
    # Never overwrite an existing audit from another invocation.
    with json_path.open("x", encoding="utf-8") as fh:
        json.dump(audit, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    with md_path.open("x", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("AUDIT JSON:", json_path)
    print("AUDIT MD:", md_path)
    print("PROJECT STATE:", state["project_state"], "COMPLETED:", state["scientific_complete"])
    print("EVIDENCE:", evidence_id, "PROTOCOL:", protocol_id)
    print("RECORDED METRICS:", json.dumps(primary, ensure_ascii=False))
    print("RECOMPUTED METRICS:", json.dumps(checked_metrics, ensure_ascii=False))
    print("ARTIFACT HASHES:", sum(bool(v.get("valid")) for v in verified), "/", len(verified))
    print("V3 REVIEW RESULT:", str(review.get("result") or "")[:2500])
    print("OPEN HANDOFFS:", len(handoffs))
    print("AUDIT ERRORS:", json.dumps(errors, ensure_ascii=False))
    return not errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("protocol_id")
    parser.add_argument("evidence_id")
    parser.add_argument("--recompute-metrics", action="store_true")
    args = parser.parse_args()
    return 0 if inspect(args.project, args.protocol_id, args.evidence_id,
                        recompute=args.recompute_metrics) else 2


if __name__ == "__main__":
    sys.exit(main())
