"""Offline proof of a discoverable, bound Experimenter freeze capability.

No network, shell experiment, GPU or third-party numerical package required.
"""
from __future__ import annotations

import json

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.evidence import EvidenceStore
from danus.core.handoff import HandoffRequests
from danus.gateway import capability
from danus.gateway import server as gateway
from danus.gateway.roles import tools_for


def _prepare(tmp_path, monkeypatch):
    root = tmp_path / "project"
    workspace = tmp_path / "scientific"
    root.mkdir()
    workspace.mkdir()
    (root / "project.json").write_text(
        json.dumps({"name": "P", "workspace": str(workspace)}), encoding="utf-8",
    )
    for name in ("researcher", "reviewer", "experimenter"):
        (root / "workers" / name).mkdir(parents=True)
    ResearchPlanGraph.create(root, node_id="P0", title="Research")
    ledger = ProgressLedger(root)
    research = ledger.assign(
        worker="researcher", role="researcher", node_id="P0",
        plan_version=1, task="Submit the exact bounded pilot",
        write_task=True,
    )
    fields = dict(
        node_id="P0", plan_version=1, proposer="researcher",
        goal="Run the bounded pilot",
        must_preserve_invariants=["Hold matrix and decision rule fixed"],
        forbidden_behaviors=["No undisclosed extra run"],
        acceptance_conditions=["Immutable bundle and Evidence"],
    )
    contract = ledger.submit_implementation_contract(**fields)
    handoffs = HandoffRequests(ledger)
    handoff = handoffs.open(
        kind="REVIEW", plan_version=1, node_id="P0",
        subject_type="IMPLEMENTATION_CONTRACT", subject_id=contract["contract_id"],
        requested_by="supervisor", source_role="researcher",
        request="Review contract exactly", reason="independent scientific gate",
    )
    review = ledger.assign(
        worker="reviewer", role="reviewer", node_id="P0", plan_version=1,
        task=handoff["request"], handoff_id=handoff["handoff_id"], write_task=True,
    )
    ledger.review_implementation_contract(
        contract["contract_id"], reviewer="reviewer", verdict="APPROVE",
        rationale="Bounded pilot accepted", review_assignment_id=review["assignment_id"],
    )
    ledger.update(worker="reviewer", state="DONE", actor="runtime", outcome="COMPLETE")
    handoffs.resolve(
        handoff["handoff_id"], assignment_id=review["assignment_id"],
        actor="supervisor", summary="Review complete",
    )
    assigned = ledger.assign(
        worker="experimenter", role="experimenter", node_id="P0",
        plan_version=1, task=contract["task"], write_task=True,
    )
    (workspace / "pilot.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "Path(__import__('sys').argv[1]).write_text(json.dumps({'score': 1}))\n",
        encoding="utf-8",
    )
    (workspace / "metric.py").write_text(
        "def reduce_metrics(*args):\n    return {'score': 1}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("danus.core.runtime.identity", lambda: {
        "schema_version": 1, "executable": "/trusted/python", "dependencies_sha256": "fixed"
    })
    monkeypatch.setattr(gateway, "_BOUND_WORKER", capability.issue(root, "experimenter", "experimenter"))
    return root, workspace, assigned, contract


def _fields():
    return dict(
        question="Does the approved pilot run?",
        hypothesis="The pilot returns a valid scalar.",
        matrix={"units": [
            {"id": "pilot-1", "argv": ["python", "pilot.py", "result.json"],
             "artifacts": ["result.json"], "inputs": []},
        ]},
        primary_metrics=["score"],
        decision_rule="Compute the approved score",
        metric_reducer_path="metric.py",
        metric_reducer_function="reduce_metrics",
        execution_files=["pilot.py"],
    )


def test_experimenter_can_freeze_and_read_exact_approved_protocol(tmp_path, monkeypatch):
    root, workspace, assigned, contract = _prepare(tmp_path, monkeypatch)
    assert "experiment_freeze_protocol" in tools_for("experimenter")
    assert "experiment_freeze_protocol" not in tools_for("researcher")
    assert "experiment_freeze_protocol" not in tools_for("reviewer")
    assert "experiment_freeze_protocol" not in tools_for("main")

    freeze = gateway._bound_tool(gateway.experiment_freeze_protocol)
    result = freeze(**_fields())
    assert result["status"] == "frozen"
    assert result["implementation_contract_id"] == contract["contract_id"]
    assert result["execution_assignment_id"] == assigned["assignment_id"]
    protocol = EvidenceStore(root).get_protocol(result["protocol_id"])
    assert protocol["bundle_id"] == result["bundle_id"]
    assert protocol["matrix"] == _fields()["matrix"]
    assert protocol["metric_reducer"]["path"] == "metric.py"
    assert protocol["execution_runtime"]["executable"] == "/trusted/python"
    assert gateway._approved_dynamic_protocol(assigned, result["protocol_id"], pin=False)
    assert freeze(**_fields())["protocol_id"] == result["protocol_id"]
    assert not (root / "experiment_runtime" / "assignment_protocols" /
                (assigned["assignment_id"] + ".json")).exists()


def test_refuses_workspace_escape_and_model_supplied_bad_hash(tmp_path, monkeypatch):
    root, workspace, _, _ = _prepare(tmp_path, monkeypatch)
    freeze = gateway._bound_tool(gateway.experiment_freeze_protocol)
    with pytest.raises(ValueError, match="canonical and relative"):
        freeze(**{**_fields(), "metric_reducer_path": "../outside.py"})
    outside = tmp_path / "elsewhere.py"
    outside.write_text("x=1\n", encoding="utf-8")
    (workspace / "link.py").symlink_to(outside)
    with pytest.raises(ValueError, match="regular file"):
        freeze(**{**_fields(), "execution_files": ["link.py"]})
    args = _fields()
    args["matrix"]["units"][0]["inputs"] = [
        {"path": "metric.py", "size": 5, "sha256": "wrong"},
    ]
    with pytest.raises(ValueError, match="hash/size"):
        freeze(**args)
    assert list((root / "evidence" / "protocols").glob("*.json")) == []


def test_stale_capability_and_unbound_gateway_cannot_freeze(tmp_path, monkeypatch):
    root, workspace, assigned, contract = _prepare(tmp_path, monkeypatch)
    binding = gateway._BOUND_WORKER
    ledger = ProgressLedger(root)
    ledger.update(worker="experimenter", state="BLOCKED", actor="runtime", outcome="BLOCKED")
    with pytest.raises(PermissionError, match="active assignment"):
        gateway._bound_tool(gateway.experiment_freeze_protocol)(**_fields())

    monkeypatch.setattr(gateway, "_BOUND_WORKER", None)
    with pytest.raises(PermissionError, match="bound Experimenter"):
        gateway.experiment_freeze_protocol(**_fields())
