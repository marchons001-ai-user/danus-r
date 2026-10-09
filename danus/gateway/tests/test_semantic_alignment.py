from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.gateway import server
from danus.gateway.roles import tools_for


@contextmanager
def _env(**values):
    old = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_semantic_gate_authority_is_role_gated():
    assert "implementation_pending_contracts" in tools_for("researcher")
    assert "implementation_submit_contract" in tools_for("researcher")
    assert "implementation_render_task" in tools_for("researcher")
    assert "implementation_review_contract" not in tools_for("researcher")

    assert "implementation_pending_contracts" in tools_for("reviewer")
    assert "implementation_review_contract" in tools_for("reviewer")
    assert "implementation_submit_contract" not in tools_for("reviewer")
    assert "implementation_render_task" not in tools_for("reviewer")

    assert not any(name.startswith("implementation_") for name in tools_for("experimenter"))

    assert "implementation_pending_contracts" in tools_for("main")
    assert "implementation_submit_contract" not in tools_for("main")
    assert "implementation_render_task" not in tools_for("main")
    assert "implementation_review_contract" not in tools_for("main")


def test_semantic_gate_round_trip_is_offline(tmp_path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")

    source = ProgressLedger(tmp_path).assign(worker="researcher", role="researcher", node_id="root", plan_version=1, task="propose contract")
    with _env(DANUS_PROJECT_DIR=tmp_path, DANUS_AUTHOR="researcher", DANUS_ROLE="researcher", DANUS_ASSIGNMENT_ID=source["assignment_id"]):
        fields = {
            "goal": "Keep DSH as the Reviewer primary backend.",
            "must_preserve_invariants": ["Reviewer remains the intentional heterogeneous role."],
            "forbidden_behaviors": ["Do not make Codex or Terra primary while DSH is usable."],
            "acceptance_conditions": ["Terra is used only after explicit DSH QUOTA."],
        }
        preview = server.implementation_render_task(**fields)
        assert ProgressLedger(tmp_path).implementation_contracts() == []
        proposed = server.implementation_submit_contract(
            node_id="root", plan_version=1, **fields,
        )
        assert proposed["task"] == preview["task"]
        assert ProgressLedger.task_sha256(proposed["task"]) == preview["task_sha256"]
        pending = server.implementation_pending_contracts()

    assert proposed["status"] == "PROPOSED"
    assert pending["count"] == 1
    assert pending["contracts"][0]["contract_id"] == proposed["contract_id"]

    ledger = ProgressLedger(tmp_path)
    handoffs = HandoffRequests(ledger)
    review = handoffs.open(kind="REVIEW", plan_version=1, node_id="root",
        subject_type="IMPLEMENTATION_CONTRACT", subject_id=proposed["contract_id"],
        requested_by="supervisor", source_role="researcher", request="review exact subject", reason="review required")
    assignment = ledger.assign(worker="reviewer", role="reviewer", node_id="root",
        plan_version=1, task=review["request"], handoff_id=review["handoff_id"])
    with _env(DANUS_PROJECT_DIR=tmp_path, DANUS_AUTHOR="reviewer", DANUS_ROLE="reviewer", DANUS_ASSIGNMENT_ID=assignment["assignment_id"]):
        reviewed = server.implementation_review_contract(
            proposed["contract_id"],
            verdict="APPROVE",
            rationale="The implementation contract matches the intended heterogeneous policy.",
        )

    ledger.update(worker="reviewer", state="DONE", actor="runtime", outcome="COMPLETE")
    handoffs.resolve(review["handoff_id"], assignment_id=assignment["assignment_id"], actor="supervisor", summary="review completed")
    assert reviewed["status"] == "APPROVE"
    ledger = ProgressLedger(tmp_path)
    assigned = ledger.assign(
        worker="experimenter",
        role="experimenter",
        node_id="root",
        plan_version=plan.snapshot()["plan_version"],
        task=proposed["task"],
    )
    assert assigned["implementation_contract_id"] == proposed["contract_id"]
    assert ledger.pending_implementation_contracts()[0]["consumed_by_assignment_id"] == assigned["assignment_id"]
    ledger.update(worker="experimenter", state="DONE", actor="runtime", outcome="COMPLETE")
    assert ledger.pending_implementation_contracts() == []



def test_canonical_preview_precedes_node_and_creates_no_contract(tmp_path, monkeypatch):
    """R2 must render an E1 Plan task before E1 exists in the committed Plan."""
    from danus.gateway import capability
    from danus.gateway import server as gateway

    monkeypatch.setattr(gateway, "_BOUND_WORKER", None)
    graph = ResearchPlanGraph.create(tmp_path, node_id="P0", title="Project")
    ledger = ProgressLedger(tmp_path)
    fields = {
        "goal": "Execute only the independently reviewed bounded pilot.",
        "must_preserve_invariants": [
            "Split data by trajectory; do not use test statistics.",
            "Keep all negative and failed runs.",
        ],
        "forbidden_behaviors": [
            "Never run before exact Implementation Contract APPROVE.",
        ],
        "acceptance_conditions": [
            "Supply immutable evidence and report all failures.",
        ],
    }
    assignment = ledger.assign(
        worker="low", role="researcher", node_id="P0",
        plan_version=1, task="Draft a governed scientific Plan.", write_task=True,
    )
    binding = capability.issue(tmp_path, "low", "researcher")
    monkeypatch.setattr(gateway, "_BOUND_WORKER", binding)

    before_ledger = ledger.path.read_bytes()
    before_plan = graph.snapshot()
    assert "E1" not in before_plan["nodes"]
    preview = gateway._bound_tool(gateway.implementation_render_task)(**fields)
    assert preview["task"] == ProgressLedger.implementation_task(**fields)
    assert preview["task_sha256"] == ProgressLedger.task_sha256(preview["task"])
    assert preview == gateway._bound_tool(gateway.implementation_render_task)(**fields)
    assert ledger.path.read_bytes() == before_ledger
    assert graph.snapshot() == before_plan
    assert ledger.implementation_contracts() == []
    with pytest.raises(ValueError):
        gateway._bound_tool(gateway.implementation_render_task)(
            **{**fields, "acceptance_conditions": []}
        )
    assert ledger.path.read_bytes() == before_ledger

    # Proposal can now carry the exact task without prematurely submitting a
    # contract on an uncommitted node. Normal Plan review/commit is still needed.
    change = graph.propose_change(
        base_plan_version=1, proposer="low",
        reason="Introduce exact downstream implementation governance",
        operations=[
            {"op": "add_node", "node_id": "S1", "title": "Submit contract",
             "summary": "", "parent": "P0", "depends_on": []},
            {"op": "add_node", "node_id": "V1", "title": "Review contract",
             "summary": "", "parent": "P0", "depends_on": ["S1"]},
            {"op": "add_node", "node_id": "E1", "title": "Execute approved scope",
             "summary": "", "parent": "P0", "depends_on": ["V1"]},
        ],
    )
    graph.review_change(change, reviewer="low2", verdict="APPROVE", rationale="bounded")
    assert graph.commit_change(change, committer="supervisor") == 2
    spec = tmp_path / "tasks" / "v2" / "experimenter" / "E1.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(preview["task"], encoding="utf-8")
    ledger.assign(
        worker="low", role="researcher", node_id="S1",
        plan_version=2, task="Submit identical implementation fields.", write_task=True,
    )
    binding = capability.issue(tmp_path, "low", "researcher")
    monkeypatch.setattr(gateway, "_BOUND_WORKER", binding)
    proposed = gateway.implementation_submit_contract(
        node_id="E1", plan_version=2, **fields,
    )
    assert proposed["status"] == "PROPOSED"
    assert proposed["task"] == preview["task"] == spec.read_text(encoding="utf-8")
    contract = ledger.implementation_contract(proposed["contract_id"])
    assert contract["task_sha256"] == preview["task_sha256"]


def test_canonical_renderer_is_not_exposed_to_other_scientific_roles(monkeypatch):
    from danus.gateway import server as gateway

    monkeypatch.setattr(gateway, "_BOUND_WORKER", None)
    fields = {
        "goal": "No write permission is implied",
        "must_preserve_invariants": ["No scope change"],
        "forbidden_behaviors": ["No experiment execution"],
        "acceptance_conditions": ["Return text only"],
    }
    for role in ("reviewer", "experimenter", "main", "verifier", "worker"):
        assert "implementation_render_task" not in tools_for(role)
        with _env(DANUS_ROLE=role):
            with pytest.raises(PermissionError, match="Researcher role"):
                gateway.implementation_render_task(**fields)

