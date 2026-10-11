#!/usr/bin/env python3
"""Read-only audit of the saved Fly E1 pilot JSON and provenance.

The immutable Evidence Record supplies the only permitted raw-artifact path.
Hash and byte size are checked before parsing. This script never mutates Plan,
Handoff, Evidence, or experimental artifacts; only a new derived report is saved.
Candidate fields are reported, NOT claimed verified scientific outcomes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

from danus.core.evidence import EvidenceStore
from danus.execution import layout as L

CATEGORIES = {
    "hardware_environment": r"hardware|device|cuda|gpu|cpu|platform|torch|numpy|python|accelerator",
    "elapsed_time": r"elapsed|duration|wall.?time|run.?time|time.?spent|seconds|time_sec|total_time",
    "peak_memory": r"peak.?mem|peak.?rss|memory|ram|vram|allocated|resident",
    "rewiring_swaps": r"rewir|swap|swap.?success|successful.?swap",
    "cross_block_fraction": r"cross.?block|between.?block|cross.?fraction|inter.?block",
    "gru_updates": r"gru|optimizer.?step|train.?step|total.?update|update.?count",
    "per_cell_diagnostics": r"per.?cell|cell.?diagnos|cell.?result|cell.?metric|diagnostics|cells",
}
PATTERNS = {name: re.compile(pattern, re.IGNORECASE) for name, pattern in CATEGORIES.items()}
MAX_CANDIDATES = 30
MAX_DEPTH = 12
MAX_VISITS = 30000


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def field_inventory(data: object):
    matches: dict[str, list[dict]] = {category: [] for category in PATTERNS}
    samples = []
    visits = 0
    def explore(value, segments, depth):
        nonlocal visits
        visits += 1
        if visits > MAX_VISITS or depth > MAX_DEPTH:
            return
        path = "/" + "/".join(segments) if segments else "/"
        if isinstance(value, dict):
            summary = {"type": "object", "num_keys": len(value),
                       "keys": list(value)[:24]}
        elif isinstance(value, list):
            summary = {"type": "array", "length": len(value)}
        else:
            # Preserve malformed/non-finite source diagnostics as strings in
            # the derived audit instead of crashing or silently rounding them.
            printable = (
                repr(value) if isinstance(value, float) and not math.isfinite(value)
                else value
            )
            summary = {"type": type(value).__name__, "value": printable}
            if isinstance(value, str) and len(value) > 200:
                summary["value"] = value[:200] + "..."
        # Matching is based on field *names*, not on guessed values.
        # Preserve the parent path for an indexed nested diagnostic record.
        field_names = " / ".join(str(x) for x in segments if not str(x).isdecimal())
        for name, pat in PATTERNS.items():
            if pat.search(field_names) and len(matches[name]) < MAX_CANDIDATES:
                matches[name].append({"path": path, **summary})
        if len(samples) < 20:
            samples.append({"path": path, **summary})
        if isinstance(value, dict):
            for k, child in value.items():
                explore(child, segments + [str(k)], depth + 1)
                if visits >= MAX_VISITS:
                    break
        elif isinstance(value, list):
            for i, child in enumerate(value[:12]):
                explore(child, segments + [str(i)], depth + 1)
                if visits >= MAX_VISITS:
                    break
    explore(data, [], 0)
    return matches, samples, {"visited": visits, "max_visits": MAX_VISITS,
                              "truncated_by_scan_limit": visits >= MAX_VISITS}


def audit(project: str, protocol_id: str, evidence_id: str) -> Path:
    root = L.project_dir(project).resolve(strict=True)
    store = EvidenceStore(root)
    record = store.get_record(evidence_id)
    protocol = store.get_protocol(protocol_id)
    if not record or not protocol or record.get("protocol_id") != protocol_id:
        raise ValueError("Evidence/Protocol IDs do not match immutable records")
    references = [
        ref for ref in record.get("artifacts", [])
        if isinstance(ref, dict) and Path(str(ref.get("path") or "")).name == "pilot.json"
    ]
    if len(references) != 1:
        raise ValueError(f"expected one Evidence-referenced pilot.json, found {len(references)}")
    ref = references[0]
    raw_path = Path(str(ref["path"]))
    if raw_path.is_absolute() or ".." in raw_path.parts:
        raise PermissionError("absolute/escaping artifact path forbidden")
    raw = (root / raw_path).resolve(strict=True)
    allowed = (root / "experiment_runtime" / protocol_id / "artifacts").resolve(strict=True)
    if not raw.is_relative_to(allowed) or not raw.is_file():
        raise PermissionError("pilot.json lies outside the frozen experiment output")
    actual_size = raw.stat().st_size
    actual_sha = sha256_file(raw)
    if actual_size != ref.get("size") or actual_sha != ref.get("sha256"):
        raise ValueError("pilot.json differs from its immutable Evidence SHA-256/size")
    parsed = json.loads(raw.read_text(encoding="utf-8"))
    matches, samples, coverage = field_inventory(parsed)
    provenance = {
        name: {"recorded": bool(record.get(name)), "value": record.get(name)}
        for name in ("code_commit", "config_ref", "data_ref")
    }
    report = {
        "kind": "read_only_fly_e1_diagnostics",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project": project, "protocol_id": protocol_id, "evidence_id": evidence_id,
        "pilot_file": str(raw_path), "pilot_sha256": actual_sha,
        "pilot_size_bytes": actual_size,
        "top_level_keys": list(parsed) if isinstance(parsed, dict) else [],
        "immutable_primary_metrics": record.get("primary_metrics", {}),
        "matrix_coverage": record.get("matrix_coverage", {}),
        "record_provenance": provenance,
        "candidate_diagnostic_fields": matches,
        "unmatched_categories": [k for k, rows in matches.items() if not rows],
        "sample_structure": samples, "scan": coverage,
        "limitations": [
            "Keyword-matched JSON paths are inspection candidates, not proof that "
            "required diagnostic quantities were measured correctly.",
            "Missing provenance values cannot be invented from a successful metric check.",
            "Historical OPEN ESCALATEs are NOT modified by this audit.",
        ],
    }
    workspace = L.scientific_workspace(root)
    out_dir = workspace / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"E1_pilot_diagnostics_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    with dest.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    print("AUDIT:", dest)
    print("PILOT HASH VALID:", actual_sha, "bytes:", actual_size)
    print("TOP LEVEL KEYS:", ", ".join(report["top_level_keys"]))
    for category, rows in matches.items():
        print(f"\n[{category}] {len(rows)} candidate entries")
        for item in rows[:8]:
            view = item.get("value", f"<{item['type']}>")
            print(f"  {item['path']}: {str(view)[:180]}")
    print("\nMISSING KEYWORD CATEGORIES:", report["unmatched_categories"])
    print("PROVENANCE:", {k: v["recorded"] for k, v in provenance.items()})
    print("IMPORTANT: Candidate fields require human verification; no request auto-closed.")
    return dest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("protocol_id")
    parser.add_argument("evidence_id")
    args = parser.parse_args()
    audit(args.project, args.protocol_id, args.evidence_id)
