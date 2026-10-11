"""Read-only dynamic Evidence access is authorized by an active INTERPRET handoff.

Runs without a model, numerical packages, experiment execution or network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from danus.core import EvidenceStore, ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.gateway import capability, server
from danus.gateway.roles import tools_for


def _setup(tmp_path: Path, monkeypatch):
    root = tmp_path / "P"
    root.mkdir()
    for name in ("low", "low2", "low3"):
        (root / "workers" / name).mkdir(parents=True)
    ResearchPlanGraph.create(root, node_id="P0", title="Synthetic pilot")
    ledger = ProgressLedger(root)
    experimenter = ledger.assign(
        worker="low3", role="experimenter", node_id="P0",
        plan_version=1, task="Approved bounded pilot without prior protocol ID",
        write_task=True,
    )
    store = EvidenceStore(root)
    protocol_id = store.freeze_protocol(
        question="Does this approved pilot produce valid raw results?",
        hypothesis="Synthetic pilot records the bounded metric.",
        matrix={"units": [{"id": "u1", "argv": ["python", "pilot.py", "raw/pilot.json"],
                           "artifacts": ["raw/pilot.json"]}]},
        primary_metrics=["loss"], decision_rule="record loss",
        author="low3", implementation_contract_id="a" * 16,
        execution_assignment_id=experimenter["assignment_id"],
    )
    raw = root / "experiment_runtime" / protocol_id / "artifacts" / "u1" / "raw" / "pilot.json"
    raw.parent.mkdir(parents=True)
    raw.write_text(json.dumps({"loss": 0.7, "diagnostics": {
        "hardware": "cpu", "successful_swaps": 5,
        "nested": {"peak_memory_mb": 123},
    }}), encoding="utf-8")
    ref = store.artifact_ref(raw)
    eid = store.add_record(
        protocol_id=protocol_id, author="low3",
        matrix_coverage={"planned": 1, "completed": 1},
        primary_metrics={"loss": 0.7}, artifacts=[ref],
    )
    handoffs = HandoffRequests(ledger)
    handed = handoffs.open(
        kind="INTERPRET", plan_version=1, node_id="P0",
        subject_type="ASSIGNMENT", subject_id=experimenter["assignment_id"],
        source_assignment_id=experimenter["assignment_id"],
        requested_by="low3", source_role="experimenter",
        request="Interpret only the immutable Evidence", reason="bounded outcome",
        evidence_refs=[eid],
    )
    ledger.update(worker="low3", state="DONE", actor="runtime", outcome="COMPLETE")
    researcher = ledger.assign(
        worker="low", role="researcher", node_id="P0",
        plan_version=1, task=handed["request"],
        handoff_id=handed["handoff_id"], write_task=True,
    )
    (root / "RESEARCH_BRIEF.md").write_text(
        "A historical brief which cannot mention a future protocol ID.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "_BOUND_WORKER", capability.issue(root, "low", "researcher"))
    return root, ledger, researcher, protocol_id, eid, raw


def test_interpret_can_read_exact_new_evidence_and_protocol_without_old_brief(tmp_path, monkeypatch):
    root, ledger, assignment, pid, eid, raw = _setup(tmp_path, monkeypatch)
    assert "experiment_evidence_get" in tools_for("researcher")
    assert "experiment_evidence_get" not in tools_for("reviewer")
    assert "experiment_evidence_get" not in tools_for("main")
    assert "experiment_evidence_get" not in tools_for("experimenter")
    assert "experiment_evidence_get" not in tools_for("verifier")

    info = server.experiment_evidence_get(eid)
    assert info["evidence_id"] == eid
    assert info["protocol_id"] == pid
    assert info["primary_metrics"] == {"loss": 0.7}
    assert len(info["artifacts"]) == 1
    assert "selected_raw_json" not in info
    assert server.experiment_protocol_get(pid)["protocol"]["protocol_id"] == pid
    assert server.experiment_evidence_get(
        eid, artifact_index=0, json_pointer="/diagnostics/nested/peak_memory_mb",
    )["selected_raw_json"]["value"] == 123
    assert server.experiment_evidence_get(
        eid, artifact_index=0, json_pointer="/diagnostics/successful_swaps",
    )["selected_raw_json"]["value"] == 5
    assert raw.exists()


def test_unrelated_evidence_and_missing_active_authority_are_rejected(tmp_path, monkeypatch):
    root, ledger, assignment, pid, eid, raw = _setup(tmp_path, monkeypatch)
    with pytest.raises(PermissionError, match="not bound"):
        server.experiment_evidence_get("f" * 16)
    with pytest.raises(PermissionError, match="not bound"):
        server.experiment_protocol_get("b" * 16)
    with pytest.raises(ValueError, match="artifact_index"):
        server.experiment_evidence_get(eid, json_pointer="/diagnostics")
    with pytest.raises(ValueError, match="artifact_index"):
        server.experiment_evidence_get(eid, artifact_index=99)
    ledger.update(worker="low", state="BLOCKED", actor="runtime", outcome="BLOCKED")
    with pytest.raises(PermissionError, match="active assignment"):
        server.experiment_evidence_get(eid)


def test_hash_mismatch_is_not_disclosed_as_raw_evidence(tmp_path, monkeypatch):
    root, ledger, assignment, pid, eid, raw = _setup(tmp_path, monkeypatch)
    raw.write_text('{"loss": 99}', encoding="utf-8")
    with pytest.raises(ValueError, match="size differs"):
        server.experiment_evidence_get(eid, artifact_index=0, json_pointer="/loss")
    assert server.experiment_evidence_get(eid)["evidence_id"] == eid


def test_reviewer_and_unbound_cannot_steal_interpret_grant(tmp_path, monkeypatch):
    root, ledger, assignment, pid, eid, raw = _setup(tmp_path, monkeypatch)
    binding = server._BOUND_WORKER
    server._BOUND_WORKER = None
    with pytest.raises(PermissionError, match="bound Researcher"):
        server.experiment_evidence_get(eid)
    server._BOUND_WORKER = {"role": "reviewer", **{k: v for k, v in binding.items() if k != "role"}}
    with pytest.raises(PermissionError, match="bound Researcher"):
        server.experiment_evidence_get(eid)
