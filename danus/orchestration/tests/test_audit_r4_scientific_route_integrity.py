from __future__ import annotations

from pathlib import Path

import pytest

from danus.core import EvidenceStore, ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.orchestration import autonomy as A
from danus.orchestration import supervisor_llm as S
from danus.orchestration.control import (
    build_supervisor_snapshot,
    generic_review_class,
    reduce_supervisor_snapshot,
)
from danus.orchestration.tests.test_s6_durable_supervisor_routes import (
    _decision_events,
    _luna_result,
    _scientific_handoffs,
    _setup_ambiguity,
)
from danus.orchestration.tests.test_s6_scientific_ambiguity_contract import (
    _generic_review,
    _needs_review_source,
)


def _report(root: Path):
    return reduce_supervisor_snapshot(build_supervisor_snapshot(root, []))


def _action_for(source, review, reviewer):
    return {
        "kind": "SCIENTIFIC_ROUTE_REQUIRED",
        "plan_version": 1,
        "node_id": "root",
        "assignment_id": source["assignment_id"],
        "worker": source["worker"],
        "reason": "review_completed_source_disposition_ambiguous",
        "subject_type": "ASSIGNMENT",
        "subject_id": source["assignment_id"],
        "source_role": source["role"],
        "review_handoff_id": review["handoff_id"],
        "review_assignment_id": reviewer["assignment_id"],
        "candidate_routes": ["INTERPRET", "RETHINK", "ESCALATE"],
    }


def _evidence(root: Path) -> str:
    store = EvidenceStore(root)
    protocol_id = store.freeze_protocol(
        question="Which bounded route follows the observed result?",
        hypothesis="The frozen observation can change the coarse research route.",
        matrix={"cases": [0]},
        primary_metrics=["score"],
        decision_rule="interpret only the frozen bounded result",
        author="reviewer",
    )
    return store.add_record(
        protocol_id=protocol_id,
        author="experimenter",
        matrix_coverage={"case": 0},
        primary_metrics={"score": 1.0},
    )


def test_generic_review_task_requires_explicit_routing_class():
    task = A._generic_review_task({
        "assignment_id": "a1",
        "role": "experimenter",
        "node_id": "root",
        "note": "review this",
    })
    assert "DANUS_REVIEW_CLASS: SCIENTIFIC_AMBIGUITY" in task
    assert "DANUS_REVIEW_CLASS: DETERMINISTIC" in task


@pytest.mark.parametrize(
    ("review_class", "reason"),
    [
        ("DETERMINISTIC", "generic_review_deterministic"),
        ("ENGINEERING_FAILURE", "generic_review_class_invalid"),
    ],
)
def test_non_scientific_generic_review_never_enters_luna_route(
    tmp_path: Path,
    review_class: str,
    reason: str,
):
    ledger, source = _needs_review_source(tmp_path)
    _generic_review(ledger, source, complete=True, review_class=review_class)

    actions = _report(tmp_path)["actions"]
    assert not any(item.get("kind") == "SCIENTIFIC_ROUTE_REQUIRED" for item in actions)
    wait = next(
        item for item in actions
        if item.get("kind") == "WAIT" and item.get("subject_id") == source["assignment_id"]
    )
    assert wait["reason"] == reason


def test_missing_or_duplicate_generic_review_class_fails_closed():
    assert generic_review_class({"result": "review done\nDANUS_OUTCOME: COMPLETE"}) == ""
    assert generic_review_class({
        "result": (
            "DANUS_REVIEW_CLASS: SCIENTIFIC_AMBIGUITY\n"
            "DANUS_REVIEW_CLASS: SCIENTIFIC_AMBIGUITY"
        )
    }) == ""


def test_forged_scientific_action_cannot_bypass_deterministic_review(
    tmp_path: Path,
    monkeypatch,
):
    ledger, source = _needs_review_source(tmp_path)
    review, reviewer = _generic_review(
        ledger,
        source,
        complete=True,
        review_class="DETERMINISTIC",
    )
    HandoffRequests(ledger).resolve(
        review["handoff_id"],
        assignment_id=reviewer["assignment_id"],
        actor="supervisor",
        summary="generic review found a deterministic handling path",
    )
    monkeypatch.setattr(S, "_run_luna", lambda prompt: pytest.fail("deterministic review must not reach Luna"))

    result = S.resolve_scientific_routes(
        tmp_path,
        [_action_for(source, review, reviewer)],
    )[0]
    assert result["status"] == "failed"
    assert result["error"].startswith("invalid_context:generic review did not authorize")
    assert not _decision_events(ledger)


def test_rethink_preserves_upstream_evidence_and_plan_gate(tmp_path: Path, monkeypatch):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    handoffs = HandoffRequests(ledger)
    evidence_id = _evidence(tmp_path)

    experiment = ledger.assign(
        worker="experimenter",
        role="experimenter",
        node_id="root",
        plan_version=1,
        task="run frozen experiment",
    )
    upstream = handoffs.open(
        kind="INTERPRET",
        plan_version=1,
        node_id="root",
        subject_type="ASSIGNMENT",
        subject_id=experiment["assignment_id"],
        requested_by="experimenter",
        source_role="experimenter",
        source_assignment_id=experiment["assignment_id"],
        request="interpret the frozen evidence",
        reason="bounded evidence requires interpretation",
        evidence_refs=[evidence_id],
    )
    source = ledger.assign(
        worker="researcher",
        role="researcher",
        node_id="root",
        plan_version=1,
        task=upstream["request"],
        handoff_id=upstream["handoff_id"],
    )
    ledger.update(
        worker="researcher",
        state="BLOCKED",
        actor="runtime",
        outcome="NEEDS_REVIEW",
        result="scientific ambiguity remains\nDANUS_OUTCOME: NEEDS_REVIEW",
    )

    review = handoffs.open(
        kind="REVIEW",
        plan_version=1,
        node_id="root",
        subject_type="ASSIGNMENT",
        subject_id=source["assignment_id"],
        requested_by="researcher",
        source_role="researcher",
        source_assignment_id=source["assignment_id"],
        request="review the remaining ambiguity",
        reason="independent scientific judgment required",
    )
    reviewer = ledger.assign(
        worker="reviewer",
        role="reviewer",
        node_id="root",
        plan_version=1,
        task=review["request"],
        handoff_id=review["handoff_id"],
    )
    ledger.update(
        worker="reviewer",
        state="DONE",
        actor="runtime",
        outcome="COMPLETE",
        result=(
            "a substantive rethink is required\n"
            "DANUS_REVIEW_CLASS: SCIENTIFIC_AMBIGUITY\n"
            "DANUS_OUTCOME: COMPLETE"
        ),
    )
    handoffs.resolve(
        review["handoff_id"],
        assignment_id=reviewer["assignment_id"],
        actor="supervisor",
        summary="generic assignment review completed",
    )
    action = next(
        item for item in _report(tmp_path)["actions"]
        if item.get("kind") == "SCIENTIFIC_ROUTE_REQUIRED"
    )
    monkeypatch.setattr(S, "_run_luna", lambda prompt: _luna_result("RETHINK"))

    decision = S.resolve_scientific_routes(tmp_path, [action])[0]
    route = handoffs.get(decision["handoff_id"])
    assert route is not None
    assert route["kind"] == "RETHINK"
    assert route["evidence_refs"] == [evidence_id]
    assert evidence_id in route["request"]
    assert _decision_events(ledger)[0]["evidence_refs"] == [evidence_id]

    ledger.assign(
        worker="researcher",
        role="researcher",
        node_id="root",
        plan_version=1,
        task=route["request"],
        handoff_id=route["handoff_id"],
    )
    operations = [{
        "op": "add_node",
        "node_id": "follow",
        "title": "Evidence follow-up",
        "summary": "Follow only the evidence-bound rethink.",
        "parent": "root",
        "depends_on": [],
    }]
    graph = ResearchPlanGraph(tmp_path)
    with pytest.raises(ValueError, match="preserve triggering evidence_refs"):
        graph.propose_change(
            base_plan_version=1,
            proposer="researcher",
            reason="[proposal:audit] evidence-bound rethink",
            operations=operations,
            post_hoc=True,
            evidence_refs=[],
        )
    change_id = graph.propose_change(
        base_plan_version=1,
        proposer="researcher",
        reason="[proposal:audit] evidence-bound rethink",
        operations=operations,
        post_hoc=True,
        evidence_refs=[evidence_id],
    )
    assert graph.get_change(change_id)["evidence_refs"] == [evidence_id]


def test_durable_replay_cannot_materialize_after_source_replacement(
    tmp_path: Path,
    monkeypatch,
):
    ledger, source, _, _, action = _setup_ambiguity(tmp_path)
    monkeypatch.setattr(S, "_run_luna", lambda prompt: _luna_result("RETHINK"))
    materialize = S._materialize_decision
    monkeypatch.setattr(
        S,
        "_materialize_decision",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        S.resolve_scientific_routes(tmp_path, [action])

    assert len(_decision_events(ledger)) == 1
    assert not _scientific_handoffs(ledger, source["assignment_id"])
    replacement = ledger.assign(
        worker="experimenter",
        role="experimenter",
        node_id="root",
        plan_version=1,
        task="replacement assignment",
    )
    assert replacement["assignment_id"] != source["assignment_id"]

    monkeypatch.setattr(S, "_materialize_decision", materialize)
    monkeypatch.setattr(S, "_run_luna", lambda prompt: pytest.fail("durable replay must not call Luna"))
    replay = S.resolve_scientific_routes(tmp_path, [action])[0]

    assert replay["status"] == "persisted_unmaterialized"
    assert replay["replayed"] is True
    assert replay["error"].startswith("route_context_invalid:source assignment no longer matches")
    assert len(_decision_events(ledger)) == 1
    assert not _scientific_handoffs(ledger, source["assignment_id"])
