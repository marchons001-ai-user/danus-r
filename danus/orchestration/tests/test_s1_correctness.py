from __future__ import annotations

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.orchestration.control import reconcile


def _plan_with_dependency(root):
    plan = ResearchPlanGraph.create(root, node_id="P0", title="Root")
    cid = plan.propose_change(
        base_plan_version=1,
        proposer="researcher",
        reason="fixture",
        operations=[
            {"op": "add_node", "node_id": "R1", "title": "Research", "summary": "", "parent": "P0", "depends_on": []},
            {"op": "add_node", "node_id": "E1", "title": "Experiment", "summary": "", "parent": "P0", "depends_on": ["R1"]},
        ],
    )
    plan.review_change(cid, reviewer="reviewer", approve=True, rationale="fixture")
    plan.commit_change(cid, committer="supervisor")
    return plan


def test_needs_review_does_not_satisfy_normal_dependency(tmp_path):
    _plan_with_dependency(tmp_path)
    ledger = ProgressLedger(tmp_path)
    ledger.assign(worker="researcher", role="researcher", node_id="R1", plan_version=2, task="research\n")
    ledger.update(
        worker="researcher", state="BLOCKED", actor="runtime", outcome="NEEDS_REVIEW"
    )

    report = reconcile(tmp_path, [])
    assert "R1" not in report["completed_nodes"]
    assert "E1" not in report["frontier"]
    assert any(
        d["node_id"] == "R1" and d["reason"] == "assignment_needs_review"
        for d in report["decisions"]
    )


def test_consumed_approved_contract_still_gates_retry(tmp_path):
    ResearchPlanGraph.create(tmp_path, node_id="E1", title="Experiment")
    ledger = ProgressLedger(tmp_path)
    contract = ledger.submit_implementation_contract(
        proposer="researcher",
        node_id="E1",
        plan_version=1,
        goal="Run the approved bounded experiment",
        must_preserve_invariants=["same protocol"],
        forbidden_behaviors=["no matrix expansion"],
        acceptance_conditions=["record the fixed metric"],
    )
    ledger.review_implementation_contract(
        contract["contract_id"], reviewer="reviewer", verdict="APPROVE", rationale="bounded"
    )
    first = ledger.assign(
        worker="exp1", role="experimenter", node_id="E1", plan_version=1, task=contract["task"]
    )
    assert first["implementation_contract_id"] == contract["contract_id"]
    assert ledger.implementation_contract(contract["contract_id"])["status"] == "CONSUMED"
    assert ledger.implementation_contract(contract["contract_id"])["gate_status"] == "APPROVE"
    ledger.update(worker="exp1", state="DONE", actor="runtime", outcome="COMPLETE")

    second = ledger.assign(
        worker="exp2", role="experimenter", node_id="E1", plan_version=1, task=contract["task"]
    )
    assert second["implementation_contract_id"] == contract["contract_id"]
    ledger.update(worker="exp2", state="DONE", actor="runtime", outcome="COMPLETE")
    with pytest.raises(ValueError, match="does not match approved"):
        ledger.assign(
            worker="exp3",
            role="experimenter",
            node_id="E1",
            plan_version=1,
            task=contract["task"] + "unreviewed change\n",
        )


def test_unresolved_contract_prevents_false_project_complete(tmp_path):
    ResearchPlanGraph.create(tmp_path, node_id="P0", title="Root")
    ledger = ProgressLedger(tmp_path)
    ledger.set_node_state("P0", state="DONE", actor="supervisor")
    contract = ledger.submit_implementation_contract(
        proposer="researcher",
        node_id="P0",
        plan_version=1,
        goal="Material change",
        must_preserve_invariants=["invariant"],
        forbidden_behaviors=["forbidden"],
        acceptance_conditions=["acceptance"],
    )

    blocked = reconcile(tmp_path, [])
    assert blocked["project_state"] == "RUNNING"
    assert blocked["unresolved_implementation_contracts"] == [
        {"contract_id": contract["contract_id"], "node_id": "P0", "status": "PROPOSED"}
    ]

    ledger.review_implementation_contract(
        contract["contract_id"], reviewer="reviewer", verdict="APPROVE", rationale="ok"
    )
    complete = reconcile(tmp_path, [])
    assert complete["project_state"] == "COMPLETE"
    assert complete["unresolved_implementation_contracts"] == []


def test_plan_review_distinguishes_revise_reject_and_legacy_false(tmp_path):
    for name, kwargs, expected in (
        ("revise", {"verdict": "REVISE"}, "REVISE"),
        ("reject", {"verdict": "REJECT"}, "REJECTED"),
        ("legacy", {"approve": False}, "REVISE"),
    ):
        root = tmp_path / name
        plan = ResearchPlanGraph.create(root, node_id="P0", title="Root")
        cid = plan.propose_change(
            base_plan_version=1,
            proposer="researcher",
            reason=name,
            operations=[{
                "op": "add_node", "node_id": "R1", "title": "Research", "summary": "", "parent": "P0", "depends_on": []
            }],
        )
        result = plan.review_change(
            cid, reviewer="reviewer", rationale="fixture", **kwargs
        )
        assert result["status"] == expected
        assert result["review_verdict"] == (
            "REVISE" if name in {"revise", "legacy"} else "REJECT"
        )
