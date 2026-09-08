"""Offline A2-7b Experiment Runtime / metric-recompute tests. No LLM or network."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from danus.core import EvidenceStore
from danus.execution import experiment as experiment_runtime
from danus.execution.experiment import run_experiment
from danus.verify.empirical import check_empirical_evidence


RUNNER = r'''from pathlib import Path
import json, sys
unit, output = sys.argv[1], Path(sys.argv[2])
counter = output.parent.parent / "counts" / f"{unit}.txt"
counter.parent.mkdir(exist_ok=True)
count = int(counter.read_text() or "0") + 1 if counter.exists() else 1
counter.write_text(str(count))
if unit == "u2" and (output.parent.parent / "fail_u2").exists():
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
        execution_files=["runner.py"],
    )
    for unit in ("u1", "u2"):
        (root / "experiment_runtime" / protocol_id / "artifacts" / unit).mkdir(parents=True)
    return store, protocol_id


def test_checkpoint_resume_reducer_and_verification(tmp_path: Path):
    store, protocol_id = _fixture(tmp_path)
    (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u2/fail_u2").touch()

    with pytest.raises(RuntimeError, match="u2 failed"):
        run_experiment(tmp_path, protocol_id)

    runtime_dir = tmp_path / "experiment_runtime" / protocol_id
    checkpoint_path = runtime_dir / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert set(checkpoint["completed"]) == {"u1"}
    assert checkpoint["failures"][-1]["unit_id"] == "u2"
    assert checkpoint["failures"][-1]["attempt"] == 1
    assert (tmp_path / checkpoint["failures"][-1]["log"]).is_file()
    assert (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/counts/u1.txt").read_text() == "1"

    status = json.loads((runtime_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["current_unit"] == "u2"
    assert status["completed_units"] == 1 and status["total_units"] == 2
    assert status["attempt"] == 1 and status["returncode"] == 7

    (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u2/fail_u2").unlink()
    result = run_experiment(tmp_path, protocol_id)
    assert result["primary_metrics"] == {"mean_score": 2.0}
    assert result["completed_units"] == 2
    assert (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/counts/u1.txt").read_text() == "1"
    assert (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u2/counts/u2.txt").read_text() == "2"
    assert (runtime_dir / "logs" / "u2.attempt2.log").is_file()

    status = json.loads((runtime_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "complete"
    assert status["completed_units"] == 2 and status["total_units"] == 2
    assert status["current_unit"] is None
    assert status["evidence_id"] == result["evidence_id"]

    # Completed runtime is idempotent: neither unit is run again.
    again = run_experiment(tmp_path, protocol_id)
    assert again["evidence_id"] == result["evidence_id"]
    assert (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/counts/u1.txt").read_text() == "1"
    assert (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u2/counts/u2.txt").read_text() == "2"

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


def test_status_is_visible_before_each_unit_starts(tmp_path: Path, monkeypatch):
    _, protocol_id = _fixture(tmp_path)
    status_path = tmp_path / "experiment_runtime" / protocol_id / "status.json"
    real_run = experiment_runtime.sandbox.run_sandboxed
    seen = []

    def run_and_observe(argv, *args, **kwargs):
        if isinstance(argv, list) and len(argv) >= 4 and Path(argv[2]).name == "runner.py":
            status = json.loads(status_path.read_text(encoding="utf-8"))
            seen.append((argv[3], status))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(experiment_runtime.sandbox, "run_sandboxed", run_and_observe)
    result = run_experiment(tmp_path, protocol_id)

    assert [unit for unit, _ in seen] == ["u1", "u2"]
    assert seen[0][1]["state"] == "running"
    assert seen[0][1]["current_unit"] == "u1"
    assert seen[0][1]["completed_units"] == 0 and seen[0][1]["total_units"] == 2
    assert seen[0][1]["attempt"] == 1 and seen[0][1]["unit_started_at"]
    assert seen[1][1]["current_unit"] == "u2"
    assert seen[1][1]["completed_units"] == 1

    final = json.loads(status_path.read_text(encoding="utf-8"))
    assert final["state"] == "complete"
    assert final["completed_units"] == final["total_units"] == 2
    assert final["evidence_id"] == result["evidence_id"]


def test_completed_artifact_change_blocks_resume(tmp_path: Path):
    _, protocol_id = _fixture(tmp_path)
    (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u2/fail_u2").touch()
    with pytest.raises(RuntimeError):
        run_experiment(tmp_path, protocol_id)

    (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/raw/u1.json").write_text('{"score": 99}', encoding="utf-8")
    (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u2/fail_u2").unlink()
    with pytest.raises(ValueError, match="completed unit artifact changed: u1"):
        run_experiment(tmp_path, protocol_id)
    assert (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/counts/u1.txt").read_text() == "1"


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


def test_live_reducer_change_preserves_frozen_runtime_and_verifier(tmp_path: Path):
    _, protocol_id = _fixture(tmp_path)
    result = run_experiment(tmp_path, protocol_id)
    (tmp_path / "reducer.py").write_text("raise RuntimeError('replacement')")
    assert run_experiment(tmp_path, protocol_id)["evidence_id"] == result["evidence_id"]
    assert check_empirical_evidence(tmp_path, scope={"matrix": "two units"},
                                    evidence_refs=[result["evidence_id"]])["passed"]


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
