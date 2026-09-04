from __future__ import annotations

from pathlib import Path

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.orchestration.control import build_supervisor_snapshot, reduce_supervisor_snapshot


def _report(root: Path):
    return reduce_supervisor_snapshot(build_supervisor_snapshot(root, []))


def _actions(report, kind: str):
    return [item for item in report["actions"] if item.get("kind") == kind]


def test_contract_review_and_approval_are_reducer_owned_routes(tmp_path: Path):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    contract = ledger.submit_implementation_contract(
        proposer="researcher",
        node_id="root",
        plan_version=1,
        goal="change controller",
        must_preserve_invariants=["nominal objective"],
        forbidden_behaviors=["bypass review"],
        acceptance_conditions=["targeted test passes"],
    )

    opened = _actions(_report(tmp_path), "OPEN_HANDOFF")
    assert len(opened) == 1
    assert opened[0]["handoff_kind"] == "REVIEW"
    assert opened[0]["subject_type"] == "IMPLEMENTATION_CONTRACT"
    assert opened[0]["subject_id"] == contract["contract_id"]
    assert opened[0]["target_role"] == "reviewer"

    handoffs = HandoffRequests(ledger)
    review = handoffs.open(
        kind="REVIEW",
        plan_version=1,
        node_id="root",
        subject_type="IMPLEMENTATION_CONTRACT",
        subject_id=contract["contract_id"],
        requested_by="researcher",
        source_role="researcher",
        request="review contract",
        reason="implementation contract requires independent review",
    )
    assert _actions(_report(tmp_path), "ASSIGN_HANDOFF")[0]["handoff_id"] == review["handoff_id"]

    assigned = ledger.assign(
        worker="reviewer",
        role="reviewer",
        node_id="root",
        plan_version=1,
        task=review["request"],
        handoff_id=review["handoff_id"],
    )
    ledger.review_implementation_contract(
        contract["contract_id"],
        reviewer="reviewer",
        verdict="APPROVE",
        rationale="bounded and exact",
    )
    ledger.update(worker="reviewer", state="DONE", actor="runtime", outcome="COMPLETE")

    report = _report(tmp_path)
    assert _actions(report, "RESOLVE_HANDOFF")[0]["handoff_id"] == review["handoff_id"]
    assign_contract = _actions(report, "ASSIGN_CONTRACT")
    assert assign_contract[0]["contract_id"] == contract["contract_id"]
    assert assign_contract[0]["target_role"] == "experimenter"
    assert assign_contract[0]["plan_version"] == assigned["plan_version"]


def test_contract_revise_plans_resolution_and_child_revision_together(tmp_path: Path):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    contract = ledger.submit_implementation_contract(
        proposer="researcher",
        node_id="root",
        plan_version=1,
        goal="change controller",
        must_preserve_invariants=["nominal objective"],
        forbidden_behaviors=["bypass review"],
        acceptance_conditions=["targeted test passes"],
    )
    handoffs = HandoffRequests(ledger)
    review = handoffs.open(
        kind="REVIEW",
        plan_version=1,
        node_id="root",
        subject_type="IMPLEMENTATION_CONTRACT",
        subject_id=contract["contract_id"],
        requested_by="researcher",
        source_role="researcher",
        request="review contract",
        reason="implementation contract requires independent review",
    )
    assignment = ledger.assign(
        worker="reviewer",
        role="reviewer",
        node_id="root",
        plan_version=1,
        task=review["request"],
        handoff_id=review["handoff_id"],
    )
    ledger.review_implementation_contract(
        contract["contract_id"],
        reviewer="reviewer",
        verdict="REVISE",
        rationale="narrow correction required",
    )
    ledger.update(worker="reviewer", state="DONE", actor="runtime", outcome="COMPLETE")

    report = _report(tmp_path)
    assert _actions(report, "RESOLVE_HANDOFF")[0]["handoff_id"] == review["handoff_id"]
    correction = _actions(report, "OPEN_HANDOFF")
    correction = next(item for item in correction if item.get("handoff_kind") == "REVISION")
    assert correction["subject_id"] == contract["contract_id"]
    assert correction["parent_handoff_id"] == review["handoff_id"]
    assert correction["source_assignment_id"] == assignment["assignment_id"]
    assert correction["target_role"] == "researcher"


def test_upward_handoff_suppresses_duplicate_generic_review_route(tmp_path: Path):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    source = ledger.assign(
        worker="experimenter",
        role="experimenter",
        node_id="root",
        plan_version=1,
        task="run bounded experiment",
    )
    ledger.update(
        worker="experimenter",
        state="BLOCKED",
        actor="runtime",
        note="scientific interpretation required",
        outcome="NEEDS_REVIEW",
    )
    handoff = HandoffRequests(ledger).open(
        kind="INTERPRET",
        plan_version=1,
        node_id="root",
        subject_type="ASSIGNMENT",
        subject_id=source["assignment_id"],
        requested_by="experimenter",
        source_role="experimenter",
        source_assignment_id=source["assignment_id"],
        request="interpret the result",
        reason="scientific interpretation required",
    )

    report = _report(tmp_path)
    assert not [
        item
        for item in _actions(report, "OPEN_HANDOFF")
        if item.get("handoff_kind") == "REVIEW"
        and item.get("subject_id") == source["assignment_id"]
    ]
    assign = _actions(report, "ASSIGN_HANDOFF")
    assert assign[0]["handoff_id"] == handoff["handoff_id"]
    assert assign[0]["target_role"] == "researcher"


def test_plan_review_resolution_and_commit_are_one_deterministic_action_plan(tmp_path: Path):
    graph = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    change_id = graph.propose_change(
        base_plan_version=1,
        proposer="researcher",
        reason="[proposal:p1] add bounded branch",
        operations=[{
            "op": "add_node",
            "node_id": "A",
            "title": "A",
            "summary": "",
            "parent": "root",
            "depends_on": [],
        }],
    )
    opened = _actions(_report(tmp_path), "OPEN_HANDOFF")
    assert opened[0]["subject_type"] == "PLAN_CHANGE"
    assert opened[0]["subject_id"] == change_id
    assert opened[0]["handoff_kind"] == "REVIEW"

    ledger = ProgressLedger(tmp_path)
    handoffs = HandoffRequests(ledger)
    review = handoffs.open(
        kind="REVIEW",
        plan_version=1,
        node_id="root",
        subject_type="PLAN_CHANGE",
        subject_id=change_id,
        requested_by="researcher",
        source_role="researcher",
        request="review plan",
        reason="plan change requires independent review",
    )
    ledger.assign(
        worker="reviewer",
        role="reviewer",
        node_id="root",
        plan_version=1,
        task=review["request"],
        handoff_id=review["handoff_id"],
    )
    graph.review_change(
        change_id,
        reviewer="reviewer",
        verdict="APPROVE",
        rationale="minimal and justified",
    )
    ledger.update(worker="reviewer", state="DONE", actor="runtime", outcome="COMPLETE")

    report = _report(tmp_path)
    assert _actions(report, "RESOLVE_HANDOFF")[0]["handoff_id"] == review["handoff_id"]
    commit = _actions(report, "COMMIT_PLAN_CHANGE")
    assert commit[0]["change_id"] == change_id
    assert commit[0]["proposal_id"] == "p1"
