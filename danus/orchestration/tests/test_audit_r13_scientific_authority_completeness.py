from __future__ import annotations

import json
from pathlib import Path

import pytest

from danus.core import EvidenceStore, ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.execution import context as C
from danus.orchestration import planning
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


def _evidence(root: Path) -> str:
    store = EvidenceStore(root)
    protocol_id = store.freeze_protocol(
        question="Which bounded route follows the result?",
        hypothesis="The frozen result can change the coarse route.",
        matrix={"cases": [0]},
        primary_metrics=["score"],
        decision_rule="use only the frozen bounded result",
        author="reviewer",
    )
    return store.add_record(
        protocol_id=protocol_id,
        author="experimenter",
        matrix_coverage={"case": 0},
        primary_metrics={"score": 1.0},
    )


def _proposal(evidence_refs, *, post_hoc: bool):
    return {
        "base_plan_version": 1,
        "reason": "evidence-bound repair",
        "post_hoc": post_hoc,
        "evidence_refs": list(evidence_refs),
        "operations": [{
            "op": "add_node",
            "node_id": "follow",
            "title": "Follow-up",
            "summary": "Evidence-bound follow-up.",
            "parent": "root",
            "depends_on": [],
        }],
        "tasks": [{
            "node_id": "follow",
            "role": "researcher",
            "task": "perform the evidence-bound follow-up",
        }],
    }


def _commit_unrelated(root: Path) -> int:
    graph = ResearchPlanGraph(root)
    change_id = graph.propose_change(
        base_plan_version=graph.snapshot()["plan_version"],
        proposer="planner",
        reason="unrelated branch",
        operations=[{
            "op": "add_node",
            "node_id": "other",
            "title": "Other",
            "summary": "Unrelated branch.",
            "parent": "root",
            "depends_on": [],
        }],
    )
    graph.review_change(
        change_id,
        reviewer="reviewer",
        verdict="APPROVE",
        rationale="bounded unrelated change",
    )
    return graph.commit_change(change_id, committer="supervisor")


def _current_route_action(root: Path):
    report = reduce_supervisor_snapshot(build_supervisor_snapshot(root, []))
    return next(
        item for item in report["actions"]
        if item.get("kind") == "SCIENTIFIC_ROUTE_REQUIRED"
    )


def test_plan_repair_preserves_route_evidence_obligation(tmp_path: Path, monkeypatch):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    evidence_id = _evidence(tmp_path)
    handoffs = HandoffRequests(ledger)
    route = handoffs.open(
        kind="RETHINK",
        plan_version=1,
        node_id="root",
        subject_type="ASSIGNMENT",
        subject_id="source-assignment",
        requested_by="supervisor",
        source_role="experimenter",
        source_assignment_id="source-assignment",
        request="rethink using the frozen evidence",
        reason="evidence-bound route",
        evidence_refs=[evidence_id],
    )
    researcher = ledger.assign(
        worker="researcher",
        role="researcher",
        node_id="root",
        plan_version=1,
        task=route["request"],
        handoff_id=route["handoff_id"],
    )
    ledger.update(
        worker="researcher",
        state="DONE",
        actor="runtime",
        outcome="COMPLETE",
    )
    monkeypatch.setattr(planning.L, "project_dir", lambda project: tmp_path)
    monkeypatch.setattr(
        planning,
        "_validate_roster",
        lambda project: {"researcher": ["researcher"], "reviewer": ["reviewer"], "experimenter": []},
    )
    monkeypatch.setattr(
        planning,
        "_assign_role",
        lambda *args, **kwargs: {"role": "researcher", "result": "assigned"},
    )
    path = planning._proposal_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**_proposal([], post_hoc=False), "assignment_id": ledger.current("researcher")["assignment_id"]}), encoding="utf-8")

    actions = planning._planning_step_unlocked("P")
    repair = json.loads(planning._repair_error_path(tmp_path).read_text(encoding="utf-8"))
    assert actions[0]["result"] == "blocked"
    assert repair["required_evidence_refs"] == [evidence_id]
    assert repair["source_assignment_id"] == researcher["assignment_id"]

    repair_assignment = ledger.assign(
        worker="researcher",
        role="researcher",
        node_id="root",
        plan_version=1,
        task="repair the proposal",
    )
    ledger.update(
        worker="researcher",
        state="DONE",
        actor="runtime",
        outcome="COMPLETE",
        expected_assignment_id=repair_assignment["assignment_id"],
    )
    path.write_text(json.dumps({**_proposal([], post_hoc=False), "assignment_id": ledger.current("researcher")["assignment_id"]}), encoding="utf-8")
    blocked = planning._capture_proposal("P")
    assert blocked and blocked["result"] == "blocked"
    assert blocked["required_evidence_refs"] == [evidence_id]

    path.write_text(json.dumps({**_proposal([evidence_id], post_hoc=True), "assignment_id": repair_assignment["assignment_id"]}), encoding="utf-8")
    captured = planning._capture_proposal("P")
    assert captured and captured["result"] == "captured"
    change = ResearchPlanGraph(tmp_path).get_change(captured["change_id"])
    assert change["post_hoc"] is True
    assert change["evidence_refs"] == [evidence_id]


def _telemetry_for_message(tmp_path: Path, message: str):
    log = tmp_path / "round.log"
    log.write_text(
        json.dumps({
            "type": "item.completed",
            "item": {"id": "m1", "type": "agent_message", "text": message},
        }) + "\n",
        encoding="utf-8",
    )
    return C.inspect_log(log)


def test_full_reviewer_marker_conflict_fails_closed_across_tail_truncation(tmp_path: Path):
    telemetry = _telemetry_for_message(
        tmp_path,
        "DANUS_REVIEW_CLASS: DETERMINISTIC\n"
        + ("x" * 9000)
        + "\nDANUS_REVIEW_CLASS: SCIENTIFIC_AMBIGUITY\n"
        + "DANUS_OUTCOME: COMPLETE",
    )
    assert "DANUS_REVIEW_CLASS_VERIFIED:" not in telemetry["last_agent_message"]
    assert generic_review_class({
        "assignment_id": "review",
        "result": telemetry["last_agent_message"],
    }) == ""


def test_single_full_reviewer_class_is_preserved_in_bounded_tail(tmp_path: Path):
    telemetry = _telemetry_for_message(
        tmp_path,
        "DANUS_REVIEW_CLASS: SCIENTIFIC_AMBIGUITY\n"
        + ("x" * 9000)
        + "\nDANUS_OUTCOME: COMPLETE",
    )
    result = telemetry["last_agent_message"]
    assert len(result) <= 8000
    assert "DANUS_REVIEW_CLASS_VERIFIED: SCIENTIFIC_AMBIGUITY" in result
    assert generic_review_class({"assignment_id": "review", "result": result}) == "SCIENTIFIC_AMBIGUITY"


def test_unrelated_plan_revision_allows_fresh_current_scientific_route(tmp_path: Path, monkeypatch):
    ledger, source, _, _, _ = _setup_ambiguity(tmp_path)
    assert _commit_unrelated(tmp_path) == 2
    action = _current_route_action(tmp_path)
    assert action["plan_version"] == 2
    calls = []
    monkeypatch.setattr(S, "_run_luna", lambda prompt: calls.append(prompt) or _luna_result("INTERPRET"))

    result = S.resolve_scientific_routes(tmp_path, [action])[0]
    handoff = HandoffRequests(ledger).get(result["handoff_id"])
    assert result["status"] == "valid"
    assert len(calls) == 1
    assert handoff and handoff["plan_version"] == 2
    assert handoff["subject_id"] == source["assignment_id"]


def test_durable_old_decision_replays_after_unrelated_plan_revision(tmp_path: Path, monkeypatch):
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
    assert _commit_unrelated(tmp_path) == 2

    monkeypatch.setattr(S, "_materialize_decision", materialize)
    monkeypatch.setattr(S, "_run_luna", lambda prompt: pytest.fail("durable replay must not call Luna"))
    replay = S.resolve_scientific_routes(tmp_path, [action])[0]
    handoff = HandoffRequests(ledger).get(replay["handoff_id"])
    assert replay["replayed"] is True
    assert handoff and handoff["plan_version"] == 2
    assert len(_decision_events(ledger)) == 1


def test_existing_scientific_handoff_assigns_after_unrelated_revision(tmp_path: Path, monkeypatch):
    ledger, _, _, _, action = _setup_ambiguity(tmp_path)
    monkeypatch.setattr(S, "_run_luna", lambda prompt: _luna_result("INTERPRET"))
    decision = S.resolve_scientific_routes(tmp_path, [action])[0]
    handoff = HandoffRequests(ledger).get(decision["handoff_id"])
    assert handoff and handoff["plan_version"] == 1
    assert _commit_unrelated(tmp_path) == 2

    assignment = ledger.assign(
        worker="researcher",
        role="researcher",
        node_id="root",
        plan_version=2,
        task=handoff["request"],
        handoff_id=handoff["handoff_id"],
    )
    assert assignment["handoff_id"] == handoff["handoff_id"]
    assert assignment["plan_version"] == 2
