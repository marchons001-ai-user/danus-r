from __future__ import annotations

from pathlib import Path

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.orchestration.control import (
    build_supervisor_snapshot,
    reconcile,
    reduce_supervisor_snapshot,
)


def _row(worker: str, *, alive: bool, label: str):
    return {"worker": worker, "alive": alive, "label": label}


def test_snapshot_reduces_deterministically_without_changing_legacy_decision(tmp_path: Path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    snapshot = build_supervisor_snapshot(tmp_path, [])

    assert snapshot["plan"]["plan_version"] == plan.snapshot()["plan_version"]
    assert snapshot["plan_changes"] == []
    assert snapshot["current_assignments"] == []
    assert snapshot["assignments"] == []
    assert snapshot["completed_nodes"] == []
    assert snapshot["implementation_contracts"] == []
    assert snapshot["handoffs"] == []
    assert snapshot["workers"] == []
    assert snapshot["project_paused"] is False

    first = reduce_supervisor_snapshot(snapshot)
    second = reduce_supervisor_snapshot(snapshot)
    assert first == second
    assert first["decisions"] == [
        {
            "action": "RETASK",
            "worker": None,
            "node_id": "root",
            "assignment_id": None,
            "reason": "ready_node_unassigned",
        }
    ]
    assert first["actions"] == [
        {
            "kind": "ASSIGN_NODE",
            "plan_version": 1,
            "node_id": "root",
            "assignment_id": None,
            "worker": None,
            "reason": "ready_node_unassigned",
        }
    ]
    assert reconcile(tmp_path, [])["decisions"] == first["decisions"]


def test_action_protocol_distinguishes_process_restart_from_scientific_review(tmp_path: Path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    assignment = ledger.assign(
        worker="r",
        role="researcher",
        node_id="root",
        plan_version=plan.snapshot()["plan_version"],
        task="work",
    )

    dead = reduce_supervisor_snapshot(
        build_supervisor_snapshot(tmp_path, [_row("r", alive=False, label="dead")])
    )
    assert dead["decisions"][0]["action"] == "RETASK"
    assert dead["actions"] == [
        {
            "kind": "START_WORKER",
            "plan_version": 1,
            "node_id": "root",
            "assignment_id": assignment["assignment_id"],
            "worker": "r",
            "reason": "worker_not_running",
        }
    ]

    ledger.update(worker="r", state="BLOCKED", actor="r", note="needs judgment")
    blocked = reduce_supervisor_snapshot(
        build_supervisor_snapshot(tmp_path, [_row("r", alive=True, label="working")])
    )
    assert blocked["decisions"][0]["action"] == "REVIEW"
    assert blocked["actions"][0]["kind"] == "REVIEW_ASSIGNMENT"
    assert blocked["actions"][0]["assignment_id"] == assignment["assignment_id"]
    assert blocked["actions"][0]["plan_version"] == 1


def test_stop_and_wait_are_explicit_protocol_actions(tmp_path: Path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    ledger.assign(
        worker="r",
        role="researcher",
        node_id="root",
        plan_version=plan.snapshot()["plan_version"],
        task="work",
    )

    working = reduce_supervisor_snapshot(
        build_supervisor_snapshot(tmp_path, [_row("r", alive=True, label="working")])
    )
    assert working["actions"][0]["kind"] == "WAIT"

    ledger.update(worker="r", state="DONE", actor="r")
    ledger.set_node_state("root", state="DONE", actor="supervisor")
    completed = reduce_supervisor_snapshot(
        build_supervisor_snapshot(tmp_path, [_row("r", alive=True, label="working")])
    )
    assert completed["project_state"] == "COMPLETE"
    assert completed["actions"][0]["kind"] == "STOP_WORKER"
    assert completed["actions"][0]["reason"] == "node_completed"
