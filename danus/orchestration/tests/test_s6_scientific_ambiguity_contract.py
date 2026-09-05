from __future__ import annotations

from pathlib import Path

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.orchestration.control import build_supervisor_snapshot, reduce_supervisor_snapshot


def _report(root: Path):
    return reduce_supervisor_snapshot(build_supervisor_snapshot(root, []))


def _actions(report, kind: str):
    return [item for item in report["actions"] if item.get("kind") == kind]


def _needs_review_source(root: Path):
    ResearchPlanGraph.create(root, node_id="root", title="Root")
    ledger = ProgressLedger(root)
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
        note="valid evidence has ambiguous scientific interpretation",
        outcome="NEEDS_REVIEW",
    )
    return ledger, source


def _generic_review(ledger: ProgressLedger, source, *, complete: bool):
    handoffs = HandoffRequests(ledger)
    review = handoffs.open(
        kind="REVIEW",
        plan_version=1,
        node_id="root",
        subject_type="ASSIGNMENT",
        subject_id=source["assignment_id"],
        requested_by="experimenter",
        source_role="experimenter",
        source_assignment_id=source["assignment_id"],
        request="review the ambiguous result",
        reason="independent scientific judgment required",
    )
    assignment = ledger.assign(
        worker="reviewer",
        role="reviewer",
        node_id="root",
        plan_version=1,
        task=review["request"],
        handoff_id=review["handoff_id"],
    )
    if complete:
        ledger.update(worker="reviewer", state="DONE", actor="runtime", outcome="COMPLETE")
    return review, assignment


def test_completed_generic_review_exposes_one_bounded_scientific_ambiguity(tmp_path: Path):
    ledger, source = _needs_review_source(tmp_path)
    review, reviewer_assignment = _generic_review(ledger, source, complete=True)

    report = _report(tmp_path)
    ambiguity = _actions(report, "SCIENTIFIC_ROUTE_REQUIRED")

    assert len(ambiguity) == 1
    assert ambiguity[0] == {
        "kind": "SCIENTIFIC_ROUTE_REQUIRED",
        "plan_version": 1,
        "node_id": "root",
        "assignment_id": source["assignment_id"],
        "worker": "experimenter",
        "reason": "review_completed_source_disposition_ambiguous",
        "subject_type": "ASSIGNMENT",
        "subject_id": source["assignment_id"],
        "source_role": "experimenter",
        "review_handoff_id": review["handoff_id"],
        "review_assignment_id": reviewer_assignment["assignment_id"],
        "candidate_routes": ["INTERPRET", "RETHINK", "ESCALATE"],
    }
    assert _actions(report, "RESOLVE_HANDOFF")[0]["handoff_id"] == review["handoff_id"]


def test_generic_review_must_complete_before_scientific_ambiguity(tmp_path: Path):
    ledger, source = _needs_review_source(tmp_path)
    _generic_review(ledger, source, complete=False)

    assert not _actions(_report(tmp_path), "SCIENTIFIC_ROUTE_REQUIRED")


def test_external_blocker_never_becomes_scientific_ambiguity(tmp_path: Path):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    ledger.assign(
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
        note="external calibration file missing",
        outcome="BLOCKED",
    )

    assert not _actions(_report(tmp_path), "SCIENTIFIC_ROUTE_REQUIRED")


@pytest.mark.parametrize(
    ("verdict", "expected_kind"),
    [("REVISE", "REVISION"), ("REJECT", "RETHINK")],
)
def test_contract_verdict_remains_deterministic(
    tmp_path: Path,
    verdict: str,
    expected_kind: str,
):
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
    ledger.assign(
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
        verdict=verdict,
        rationale="reviewer verdict is authoritative",
    )
    ledger.update(worker="reviewer", state="DONE", actor="runtime", outcome="COMPLETE")

    report = _report(tmp_path)
    assert not _actions(report, "SCIENTIFIC_ROUTE_REQUIRED")
    correction = next(
        item
        for item in _actions(report, "OPEN_HANDOFF")
        if item.get("subject_id") == contract["contract_id"]
    )
    assert correction["handoff_kind"] == expected_kind


@pytest.mark.parametrize("kind", ["INTERPRET", "RETHINK", "ESCALATE"])
def test_existing_scientific_route_suppresses_duplicate_ambiguity(tmp_path: Path, kind: str):
    ledger, source = _needs_review_source(tmp_path)
    _generic_review(ledger, source, complete=True)
    HandoffRequests(ledger).open(
        kind=kind,
        plan_version=1,
        node_id="root",
        subject_type="ASSIGNMENT",
        subject_id=source["assignment_id"],
        requested_by="supervisor",
        source_role="experimenter",
        source_assignment_id=source["assignment_id"],
        request="continue the already selected scientific route",
        reason="scientific route already selected",
    )

    assert not _actions(_report(tmp_path), "SCIENTIFIC_ROUTE_REQUIRED")
