from __future__ import annotations

import json
from pathlib import Path

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.execution import context as C
from danus.orchestration import planning
from danus.orchestration import supervisor_llm as S
from danus.orchestration.control import build_supervisor_snapshot, reduce_supervisor_snapshot
from danus.orchestration.tests.test_audit_r13_scientific_authority_completeness import (
    _commit_unrelated,
    _evidence,
    _proposal,
)
from danus.orchestration.tests.test_s6_scientific_route_closure import (
    _allow_disposition,
    _ambiguity,
    _bind_layout,
    _luna,
)


def test_crash_revision_replay_assignment_and_reopen_is_single_use(tmp_path: Path, monkeypatch):
    _bind_layout(monkeypatch, tmp_path)
    ledger, source, action = _ambiguity(tmp_path, source_role="researcher")
    monkeypatch.setattr(S, "_run_luna", lambda prompt: _luna("RETHINK"))
    materialize = S._materialize_decision
    monkeypatch.setattr(
        S,
        "_materialize_decision",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        S.resolve_scientific_routes(tmp_path, [action])
    assert not [
        h for h in HandoffRequests(ledger).all()
        if h.get("kind") in {"INTERPRET", "RETHINK", "ESCALATE"}
        and h.get("subject_id") == source["assignment_id"]
    ]
    assert _commit_unrelated(tmp_path) == 2

    monkeypatch.setattr(S, "_materialize_decision", materialize)
    monkeypatch.setattr(S, "_run_luna", lambda prompt: pytest.fail("durable replay must not call Luna"))
    replay = S.resolve_scientific_routes(tmp_path, [action])[0]
    handoffs = HandoffRequests(ledger)
    handoff = handoffs.get(replay["handoff_id"])
    assert replay["replayed"] is True
    assert handoff and handoff["plan_version"] == 2

    researcher_dir = tmp_path / "workers" / "researcher"
    researcher_dir.mkdir(parents=True, exist_ok=True)
    (researcher_dir / "TASK.md").write_text(handoff["request"], encoding="utf-8")
    route_assignment = ledger.assign(
        worker="researcher",
        role="researcher",
        node_id="root",
        plan_version=2,
        task=handoff["request"],
        handoff_id=handoff["handoff_id"],
    )
    ledger.update(
        worker="researcher",
        state="DONE",
        actor="runtime",
        outcome="COMPLETE",
        expected_assignment_id=route_assignment["assignment_id"],
    )
    C.write_checkpoint(
        researcher_dir,
        role="researcher",
        round_no=1,
        telemetry={"last_agent_message": "RETHINK RESULT: bounded conclusion\nDANUS_OUTCOME: COMPLETE"},
    )
    handoffs.resolve(
        handoff["handoff_id"],
        assignment_id=route_assignment["assignment_id"],
        actor="supervisor",
        summary="scientific route completed",
    )
    _allow_disposition(monkeypatch)

    disposition = S.apply_scientific_route_dispositions("p")
    assert disposition[0]["result"] == "completed"
    assert "root" in ledger.completed_nodes()

    ledger.set_node_state("root", state="OPEN", actor="supervisor", note="manual reopen")
    assert S.apply_scientific_route_dispositions("p") == []
    assert ledger.node_states()["root"]["state"] == "OPEN"
    assert "root" not in ledger.completed_nodes()


def test_conflicting_long_reviewer_output_fails_closed_in_reducer(tmp_path: Path):
    log = tmp_path / "review.log"
    log.write_text(
        json.dumps({
            "type": "item.completed",
            "item": {
                "id": "review",
                "type": "agent_message",
                "text": (
                    "DANUS_REVIEW_CLASS: DETERMINISTIC\n"
                    + ("x" * 9000)
                    + "\nDANUS_REVIEW_CLASS: SCIENTIFIC_AMBIGUITY\n"
                    + "DANUS_OUTCOME: COMPLETE"
                ),
            },
        }) + "\n",
        encoding="utf-8",
    )
    bounded = C.inspect_log(log)["last_agent_message"]
    ledger, source, _ = _ambiguity(tmp_path, source_role="experimenter", review_result=bounded)

    report = reduce_supervisor_snapshot(build_supervisor_snapshot(tmp_path, []))
    source_actions = [
        item for item in report["actions"]
        if item.get("subject_id") == source["assignment_id"]
    ]
    assert not any(item.get("kind") == "SCIENTIFIC_ROUTE_REQUIRED" for item in source_actions)
    wait = next(item for item in source_actions if item.get("kind") == "WAIT")
    assert wait["reason"] == "generic_review_class_invalid"


def test_budget_stop_preserves_repair_evidence_obligation(tmp_path: Path, monkeypatch):
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
        request="rethink using frozen evidence",
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
        expected_assignment_id=researcher["assignment_id"],
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
    proposal_path = planning._proposal_path(tmp_path)
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_text(json.dumps({**_proposal([], post_hoc=False), "assignment_id": ledger.current("researcher")["assignment_id"]}), encoding="utf-8")

    first = planning._planning_step_unlocked("P")
    assert first[0]["result"] == "blocked"
    repair = json.loads(planning._repair_error_path(tmp_path).read_text(encoding="utf-8"))
    assert repair["required_evidence_refs"] == [evidence_id]

    repair_assignment = ledger.assign(
        worker="researcher",
        role="researcher",
        node_id="root",
        plan_version=1,
        task="repair the proposal\n",
    )
    ledger.update(
        worker="researcher",
        state="DONE",
        actor="runtime",
        outcome="COMPLETE",
        expected_assignment_id=repair_assignment["assignment_id"],
    )
    proposal_path.write_text(json.dumps({**_proposal([], post_hoc=False), "assignment_id": ledger.current("researcher")["assignment_id"]}), encoding="utf-8")
    monkeypatch.setattr(
        planning.cli,
        "_do_supervise_unlocked",
        lambda *args, **kwargs: {"project_state": "RUNNING", "budget_stop": "resource_token_budget"},
    )

    assert planning.planning_step("P", token_budget=1) == []
    assert proposal_path.is_file()
    repair = json.loads(planning._repair_error_path(tmp_path).read_text(encoding="utf-8"))
    assert repair["required_evidence_refs"] == [evidence_id]

    monkeypatch.setattr(
        planning.cli,
        "_do_supervise_unlocked",
        lambda *args, **kwargs: {"project_state": "RUNNING", "budget_stop": None},
    )
    after = planning.planning_step("P", token_budget=1)
    assert after[0]["result"] == "blocked"
    repair = json.loads(planning._repair_error_path(tmp_path).read_text(encoding="utf-8"))
    assert repair["required_evidence_refs"] == [evidence_id]
