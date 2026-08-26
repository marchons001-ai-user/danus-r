"""Offline A2-7b Experiment Runtime / metric-recompute tests. No LLM or network."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from danus.core import EvidenceStore
from danus.execution.experiment import run_experiment
from danus.verify.empirical import check_empirical_evidence


RUNNER = r'''from pathlib import Path
import json, sys
unit, output = sys.argv[1], Path(sys.argv[2])
counter = Path("counts") / f"{unit}.txt"
counter.parent.mkdir(exist_ok=True)
count = int(counter.read_text() or "0") + 1 if counter.exists() else 1
counter.write_text(str(count))
if unit == "u2" and Path("fail_u2").exists():
    raise SystemExit(7)
output.parent.mkdir(parents=True, exist_ok=True)
value = 1.0 if unit == "u1" else 3.0
output.write_text(json.dumps({"score": value}))
'''

REDUCER = r'''import json

def reduce_metrics(protocol, artifacts):
    values = [json.loads(path.read_text())["score"] for path in artifacts]
    return {"mean_score": sum(values) / len(values)}
'''


def _fixture(root: Path):
    runner = root / "runner.py"
    reducer = root / "reducer.py"
    runner.write_text(RUNNER, encoding="utf-8")
    reducer.write_text(REDUCER, encoding="utf-8")

    store = EvidenceStore(root)
    reducer_ref = store.artifact_ref(reducer)
    reducer_ref["function"] = "reduce_metrics"
    protocol_id = store.freeze_protocol(
        question="Does the two-unit frozen run have mean score 2?",
        hypothesis="The frozen mean score is 2.",
        matrix={
            "units": [
                {
                    "id": "u1",
                    "argv": [sys.executable, "runner.py", "u1", "raw/u1.json"],
                    "artifacts": ["raw/u1.json"],
                },
                {
                    "id": "u2",
                    "argv": [sys.executable, "runner.py", "u2", "raw/u2.json"],
                    "artifacts": ["raw/u2.json"],
                },
            ]
        },
        primary_metrics=["mean_score"],
        decision_rule="support iff mean_score == 2",
        author="reviewer",
        metric_reducer=reducer_ref,
    )
    return store, protocol_id


def test_checkpoint_resume_reducer_and_verification(tmp_path: Path):
    store, protocol_id = _fixture(tmp_path)
    (tmp_path / "fail_u2").touch()

    with pytest.raises(RuntimeError, match="u2 failed"):
        run_experiment(tmp_path, protocol_id)

    checkpoint_path = tmp_path / "experiment_runtime" / protocol_id / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert set(checkpoint["completed"]) == {"u1"}
    assert checkpoint["failures"][-1]["unit_id"] == "u2"
    assert checkpoint["failures"][-1]["attempt"] == 1
    assert (tmp_path / checkpoint["failures"][-1]["log"]).is_file()
    assert (tmp_path / "counts" / "u1.txt").read_text() == "1"

    (tmp_path / "fail_u2").unlink()
    result = run_experiment(tmp_path, protocol_id)
    assert result["primary_metrics"] == {"mean_score": 2.0}
    assert result["completed_units"] == 2
    assert (tmp_path / "counts" / "u1.txt").read_text() == "1"
    assert (tmp_path / "counts" / "u2.txt").read_text() == "2"
    assert (tmp_path / "experiment_runtime" / protocol_id / "logs" / "u2.attempt2.log").is_file()

    # Completed runtime is idempotent: neither unit is run again.
    again = run_experiment(tmp_path, protocol_id)
    assert again["evidence_id"] == result["evidence_id"]
    assert (tmp_path / "counts" / "u1.txt").read_text() == "1"
    assert (tmp_path / "counts" / "u2.txt").read_text() == "2"

    record = store.get_record(result["evidence_id"])
    assert record and record["primary_metrics"] == {"mean_score": 2.0}
    assert len(record["failures"]) == 1 and record["failures"][0]["unit_id"] == "u2"
    out = check_empirical_evidence(
        tmp_path,
        scope={"matrix": "two-unit frozen smoke"},
        evidence_refs=[result["evidence_id"]],
    )
    assert out["passed"] is True
    recompute = [c for c in out["checks"] if c["name"].endswith("primary_metrics_recompute")]
    assert len(recompute) == 1 and recompute[0]["passed"] is True


def test_completed_artifact_change_blocks_resume(tmp_path: Path):
    _, protocol_id = _fixture(tmp_path)
    (tmp_path / "fail_u2").touch()
    with pytest.raises(RuntimeError):
        run_experiment(tmp_path, protocol_id)

    (tmp_path / "raw" / "u1.json").write_text('{"score": 99}', encoding="utf-8")
    (tmp_path / "fail_u2").unlink()
    with pytest.raises(ValueError, match="completed unit artifact changed: u1"):
        run_experiment(tmp_path, protocol_id)
    assert (tmp_path / "counts" / "u1.txt").read_text() == "1"


def test_verifier_rejects_wrong_metrics_even_with_valid_record_identity(tmp_path: Path):
    store, protocol_id = _fixture(tmp_path)
    result = run_experiment(tmp_path, protocol_id)
    good = store.get_record(result["evidence_id"])
    assert good

    # Create a different, internally content-addressed Evidence Record over the
    # same raw artifacts but with a false metric. Identity alone therefore passes;
    # raw-artifact recomputation must be what rejects it.
    bad_id = store.add_record(
        protocol_id=protocol_id,
        author="experimenter",
        matrix_coverage=good["matrix_coverage"],
        primary_metrics={"mean_score": 999.0},
        failures=[],
        deviations=[],
        artifacts=good["artifacts"],
    )
    out = check_empirical_evidence(
        tmp_path,
        scope={"matrix": "two-unit frozen smoke"},
        evidence_refs=[bad_id],
    )
    assert out["passed"] is False
    assert any("primary_metrics_recompute" in error for error in out["errors"])


def test_reducer_change_rejected_by_runtime_and_verifier(tmp_path: Path):
    _, protocol_id = _fixture(tmp_path)
    result = run_experiment(tmp_path, protocol_id)
    (tmp_path / "reducer.py").write_text(REDUCER + "\n# changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="metric reducer"):
        run_experiment(tmp_path, protocol_id)

    out = check_empirical_evidence(
        tmp_path,
        scope={"matrix": "two-unit frozen smoke"},
        evidence_refs=[result["evidence_id"]],
    )
    assert out["passed"] is False
    assert any("primary_metrics_recompute" in error and "reducer" in error for error in out["errors"])


def test_tampered_frozen_protocol_is_never_executed(tmp_path: Path):
    store, protocol_id = _fixture(tmp_path)
    path = store.protocols_dir / f"{protocol_id}.json"
    protocol = json.loads(path.read_text(encoding="utf-8"))
    protocol["matrix"]["units"][0]["argv"] = [sys.executable, "runner.py", "u1", "raw/evil.json"]
    path.write_text(json.dumps(protocol), encoding="utf-8")

    with pytest.raises(ValueError, match="protocol identity mismatch"):
        run_experiment(tmp_path, protocol_id)
    assert not (tmp_path / "counts").exists()


def test_historical_protocol_without_reducer_keeps_identity_and_verification(tmp_path: Path):
    store = EvidenceStore(tmp_path)
    artifact = tmp_path / "legacy.csv"
    artifact.write_text("metric,value\nrmse,0.1\n", encoding="utf-8")
    protocol_id = store.freeze_protocol(
        question="legacy q",
        hypothesis="legacy h",
        matrix={"noise": [0.0]},
        primary_metrics=["rmse"],
        decision_rule="legacy d",
        author="reviewer",
    )
    protocol = store.get_protocol(protocol_id)
    assert protocol and "metric_reducer" not in protocol
    evidence_id = store.add_record(
        protocol_id=protocol_id,
        author="experimenter",
        matrix_coverage={"planned": 1, "completed": 1},
        primary_metrics={"rmse": 0.1},
        artifacts=[store.artifact_ref(artifact)],
    )
    out = check_empirical_evidence(
        tmp_path,
        scope={"legacy": True},
        evidence_refs=[evidence_id],
    )
    assert out["passed"] is True
    assert not any(c["name"].endswith("primary_metrics_recompute") for c in out["checks"])
