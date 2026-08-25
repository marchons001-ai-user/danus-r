from __future__ import annotations

import json

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph


def _commit(plan: ResearchPlanGraph, operations):
    current = plan.snapshot()["plan_version"]
    cid = plan.propose_change(
        base_plan_version=current,
        proposer="researcher",
        reason="test route change",
        operations=operations,
    )
    plan.review_change(cid, reviewer="reviewer", approve=True, rationale="needed")
    plan.commit_change(cid, committer="supervisor")


def _plan_with_two_nodes(tmp_path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    _commit(
        plan,
        [
            {"op": "add_node", "node_id": "a", "title": "Direction A", "summary": "", "parent": "root", "depends_on": []},
            {"op": "add_node", "node_id": "b", "title": "Direction B", "summary": "", "parent": "root", "depends_on": []},
        ],
    )
    return plan


def test_progress_updates_do_not_modify_plan(tmp_path):
    plan = _plan_with_two_nodes(tmp_path)
    before = plan.snapshot()
    ledger = ProgressLedger(tmp_path)
    rec = ledger.assign(
        worker="r1",
        role="researcher",
        node_id="a",
        plan_version=before["plan_version"],
        task="analyze A\n",
    )
    assert rec["state"] == "ASSIGNED"
    assert rec["node_id"] == "a"
    assert rec["task_sha256"] == ProgressLedger.task_sha256("analyze A\n")

    assert ledger.update(worker="r1", state="RUNNING", actor="r1")["state"] == "RUNNING"
    assert ledger.update(worker="r1", state="BLOCKED", actor="r1", note="need review")["state"] == "BLOCKED"
    assert ledger.update(worker="r1", state="RUNNING", actor="r1")["state"] == "RUNNING"
    assert ledger.update(worker="r1", state="DONE", actor="r1")["state"] == "DONE"
    assert plan.snapshot() == before

    events = [json.loads(line) for line in ledger.path.read_text(encoding="utf-8").splitlines()]
    assert [event["state"] for event in events] == ["ASSIGNED", "RUNNING", "BLOCKED", "RUNNING", "DONE"]


def test_assignment_stales_only_when_bound_node_changes(tmp_path):
    plan = _plan_with_two_nodes(tmp_path)
    ledger = ProgressLedger(tmp_path)
    version = plan.snapshot()["plan_version"]
    ledger.assign(worker="r1", role="researcher", node_id="a", plan_version=version, task="task")

    _commit(
        plan,
        [{"op": "add_node", "node_id": "c", "title": "Direction C", "summary": "", "parent": "root", "depends_on": []}],
    )
    still_valid = ledger.current("r1", refresh_stale=True)
    assert still_valid is not None and still_valid["state"] == "ASSIGNED"
    assert still_valid["plan_version"] == version

    _commit(plan, [{"op": "set_dependencies", "node_id": "a", "depends_on": ["b"]}])
    stale = ledger.current("r1", refresh_stale=True)
    assert stale is not None and stale["state"] == "STALE"
    assert "changed or was retired" in stale["note"]


def test_progress_rejects_invalid_binding_and_terminal_reopen(tmp_path):
    plan = _plan_with_two_nodes(tmp_path)
    ledger = ProgressLedger(tmp_path)
    version = plan.snapshot()["plan_version"]

    with pytest.raises(ValueError, match="unknown plan node"):
        ledger.assign(worker="r1", role="researcher", node_id="missing", plan_version=version, task="x")
    with pytest.raises(ValueError, match="stale plan_version"):
        ledger.assign(worker="r1", role="researcher", node_id="a", plan_version=version - 1, task="x")

    ledger.assign(worker="r1", role="researcher", node_id="a", plan_version=version, task="x")
    ledger.update(worker="r1", state="DONE", actor="r1")
    with pytest.raises(ValueError, match="already DONE"):
        ledger.update(worker="r1", state="RUNNING", actor="r1")


def test_new_assignment_supersedes_unfinished_assignment(tmp_path):
    plan = _plan_with_two_nodes(tmp_path)
    ledger = ProgressLedger(tmp_path)
    version = plan.snapshot()["plan_version"]
    first = ledger.assign(worker="r1", role="researcher", node_id="a", plan_version=version, task="first")
    second = ledger.assign(worker="r1", role="researcher", node_id="b", plan_version=version, task="second")

    assert first["assignment_id"] != second["assignment_id"]
    assert ledger.current("r1")["assignment_id"] == second["assignment_id"]
    events = [json.loads(line) for line in ledger.path.read_text(encoding="utf-8").splitlines()]
    assert any(event.get("assignment_id") == first["assignment_id"] and event.get("state") == "STALE" for event in events)
