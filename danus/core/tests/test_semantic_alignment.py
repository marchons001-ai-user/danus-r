from __future__ import annotations

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph


def _commit(plan: ResearchPlanGraph, operations):
    version = plan.snapshot()["plan_version"]
    change_id = plan.propose_change(
        base_plan_version=version,
        proposer="researcher",
        reason="test structural change",
        operations=operations,
    )
    plan.review_change(change_id, reviewer="reviewer", approve=True, rationale="needed")
    plan.commit_change(change_id, committer="supervisor")


def _submit(
    ledger: ProgressLedger,
    version: int,
    *,
    node_id: str = "root",
    goal: str = "Keep DSH primary",
):
    return ledger.submit_implementation_contract(
        proposer="researcher",
        node_id=node_id,
        plan_version=version,
        goal=goal,
        must_preserve_invariants=["Reviewer remains heterogeneous"],
        forbidden_behaviors=["Do not make Codex the primary Reviewer backend"],
        acceptance_conditions=["DSH is selected until explicit DSH QUOTA"],
    )


def test_material_assignment_requires_approved_exact_contract(tmp_path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    version = plan.snapshot()["plan_version"]
    contract = _submit(ledger, version)

    assert contract["status"] == "PROPOSED"
    with pytest.raises(ValueError, match="requires APPROVE"):
        ledger.assign(
            worker="experimenter",
            role="experimenter",
            node_id="root",
            plan_version=version,
            task=contract["task"],
        )

    reviewed = ledger.review_implementation_contract(
        contract["contract_id"],
        reviewer="reviewer",
        verdict="APPROVE",
        rationale="The direction and invariants match the intended policy.",
    )
    assert reviewed["status"] == "APPROVE"

    with pytest.raises(ValueError, match="does not match approved"):
        ledger.assign(
            worker="experimenter",
            role="experimenter",
            node_id="root",
            plan_version=version,
            task=contract["task"] + "extra instruction\n",
        )

    assigned = ledger.assign(
        worker="experimenter",
        role="experimenter",
        node_id="root",
        plan_version=version,
        task=contract["task"],
    )
    assert assigned["implementation_contract_id"] == contract["contract_id"]
    assert ledger.implementation_contract(contract["contract_id"])["status"] == "CONSUMED"
    assert ledger.pending_implementation_contracts() == []


def test_revise_and_plan_semantic_change_keep_gate_closed(tmp_path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    _commit(
        plan,
        [
            {"op": "add_node", "node_id": "a", "title": "Direction A", "summary": "", "parent": "root", "depends_on": []},
            {"op": "add_node", "node_id": "b", "title": "Direction B", "summary": "", "parent": "root", "depends_on": []},
        ],
    )
    ledger = ProgressLedger(tmp_path)
    version = plan.snapshot()["plan_version"]
    contract = _submit(ledger, version, node_id="a")
    ledger.review_implementation_contract(
        contract["contract_id"],
        reviewer="reviewer",
        verdict="REVISE",
        rationale="Acceptance condition is incomplete.",
    )
    with pytest.raises(ValueError, match="current status is REVISE"):
        ledger.assign(
            worker="experimenter",
            role="experimenter",
            node_id="a",
            plan_version=version,
            task=contract["task"],
        )

    replacement = _submit(
        ledger,
        version,
        node_id="a",
        goal="Keep DSH primary with bounded Terra fallback",
    )
    ledger.review_implementation_contract(
        replacement["contract_id"],
        reviewer="reviewer",
        verdict="APPROVE",
        rationale="Aligned.",
    )
    _commit(plan, [{"op": "set_dependencies", "node_id": "a", "depends_on": ["b"]}])

    pending = ledger.pending_implementation_contracts()
    assert pending[0]["contract_id"] == replacement["contract_id"]
    assert pending[0]["status"] == "STALE"
    with pytest.raises(ValueError, match="current status is STALE"):
        ledger.assign(
            worker="experimenter",
            role="experimenter",
            node_id="a",
            plan_version=plan.snapshot()["plan_version"],
            task=replacement["task"],
        )
