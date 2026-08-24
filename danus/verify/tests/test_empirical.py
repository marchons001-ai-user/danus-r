"""Offline tests for empirical evidence integrity checks. No LLM or network."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from danus.core import EvidenceStore
from danus.verify.empirical import check_empirical_evidence


def _fixture(root: Path):
    store = EvidenceStore(root)
    artifact = root / "runs" / "summary.csv"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("metric,value\nrmse,0.10\n", encoding="utf-8")
    protocol_id = store.freeze_protocol(
        question="Does the frozen model meet the RMSE target?",
        hypothesis="RMSE is below 0.12 on the frozen matrix.",
        matrix={"noise": [0.0, 0.1]},
        primary_metrics=["rmse"],
        decision_rule="accept the hypothesis iff aggregate RMSE < 0.12",
        author="reviewer",
    )
    evidence_id = store.add_record(
        protocol_id=protocol_id,
        author="experimenter",
        code_commit="abc123",
        data_ref="heldout-v1",
        config_ref="cfg-v1",
        matrix_coverage={"planned": 2, "completed": 2},
        primary_metrics={"rmse": 0.10},
        failures=[],
        deviations=[],
        artifacts=[store.artifact_ref(artifact)],
    )
    return store, artifact, protocol_id, evidence_id


def test_clean_bundle_passes():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _, _, _, evidence_id = _fixture(root)
        out = check_empirical_evidence(
            root,
            scope={"dataset": "heldout-v1", "matrix": "frozen"},
            evidence_refs=[evidence_id],
        )
        assert out["passed"] is True and out["errors"] == []
        assert len(out["protocols"]) == 1 and len(out["evidence_records"]) == 1
        assert all(c["passed"] for c in out["checks"])


def test_tampered_artifact_rejected():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _, artifact, _, evidence_id = _fixture(root)
        artifact.write_text("metric,value\nrmse,9.99\n", encoding="utf-8")
        out = check_empirical_evidence(
            root,
            scope={"dataset": "heldout-v1"},
            evidence_refs=[evidence_id],
        )
        assert out["passed"] is False
        assert any("artifact" in e and ("size changed" in e or "SHA-256 changed" in e) for e in out["errors"])


def test_tampered_record_identity_rejected():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        store, _, _, evidence_id = _fixture(root)
        path = store.records_dir / f"{evidence_id}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["primary_metrics"]["rmse"] = 0.01
        path.write_text(json.dumps(record), encoding="utf-8")
        out = check_empirical_evidence(
            root,
            scope={"dataset": "heldout-v1"},
            evidence_refs=[evidence_id],
        )
        assert out["passed"] is False
        assert any("identity" in e for e in out["errors"])


def test_missing_scope_or_evidence_stops_early():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        out = check_empirical_evidence(root, scope={}, evidence_refs=[])
        assert out["passed"] is False
        names = {c["name"] for c in out["checks"] if not c["passed"]}
        assert {"scope", "evidence_refs"} <= names


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [ok] {name}")
    print("ALL EMPIRICAL CHECK TESTS PASSED")


if __name__ == "__main__":
    main()
