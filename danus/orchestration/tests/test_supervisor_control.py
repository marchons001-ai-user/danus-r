from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.orchestration import cli
from danus.orchestration.control import reconcile


def _commit(plan: ResearchPlanGraph, operations):
    version = plan.snapshot()["plan_version"]
    change_id = plan.propose_change(
        base_plan_version=version,
        proposer="researcher",
        reason="test route change",
        operations=operations,
    )
    plan.review_change(
        change_id,
        reviewer="reviewer",
        approve=True,
        rationale="required for test",
    )
    plan.commit_change(change_id, committer="supervisor")


def _row(worker: str, *, alive: bool, label: str):
    return {"worker": worker, "alive": alive, "label": label}


def _decision(report, *, action: str, reason: str):
    return [d for d in report["decisions"] if d["action"] == action and d["reason"] == reason]


def test_completion_is_explicit_and_unlocks_dependency(tmp_path: Path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    _commit(
        plan,
        [
            {"op": "add_node", "node_id": "A", "title": "Question A", "parent": "root"},
            {"op": "add_node", "node_id": "B", "title": "Question B", "parent": "root", "depends_on": ["A"]},
        ],
    )
    ledger = ProgressLedger(tmp_path)
    ledger.assign(worker="r", role="researcher", node_id="A",
                  plan_version=plan.snapshot()["plan_version"], task="study A")
    ledger.update(worker="r", state="DONE", actor="r")

    before = reconcile(tmp_path, [_row("r", alive=False, label="dead")])
    assert before["frontier"] == ["A"]
    assert _decision(before, action="REVIEW", reason="completion_not_recorded")
    assert "B" not in before["frontier"]

    ledger.set_node_state("A", state="DONE", actor="supervisor")
    after = reconcile(tmp_path, [_row("r", alive=False, label="dead")])
    assert after["completed_nodes"] == ["A"]
    assert after["frontier"] == ["B"]
    assert _decision(after, action="RETASK", reason="ready_node_unassigned")

    ledger.set_node_state("A", state="OPEN", actor="supervisor")
    reopened = reconcile(tmp_path, [_row("r", alive=False, label="dead")])
    assert reopened["frontier"] == ["A"]
    assert "A" not in reopened["completed_nodes"]


def test_running_blocked_stuck_and_dead_map_to_control_actions(tmp_path: Path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    ledger.assign(worker="r", role="researcher", node_id="root",
                  plan_version=plan.snapshot()["plan_version"], task="work")

    working = reconcile(tmp_path, [_row("r", alive=True, label="working")])
    assert _decision(working, action="CONTINUE", reason="work_in_progress")

    stuck = reconcile(tmp_path, [_row("r", alive=True, label="stuck?")])
    assert _decision(stuck, action="REVIEW", reason="worker_stuck")

    dead = reconcile(tmp_path, [_row("r", alive=False, label="dead")])
    assert _decision(dead, action="RETASK", reason="worker_not_running")

    ledger.update(worker="r", state="BLOCKED", actor="r", note="need review")
    blocked = reconcile(tmp_path, [_row("r", alive=True, label="working")])
    assert _decision(blocked, action="REVIEW", reason="assignment_blocked")


def test_dependency_and_ancestor_changes_stop_now_invalid_work(tmp_path: Path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    _commit(
        plan,
        [
            {"op": "add_node", "node_id": "A", "title": "A", "parent": "root"},
            {"op": "add_node", "node_id": "B", "title": "B", "parent": "root", "depends_on": ["A"]},
            {"op": "add_node", "node_id": "P", "title": "Parent", "parent": "root"},
            {"op": "add_node", "node_id": "C", "title": "Child", "parent": "P"},
        ],
    )
    ledger = ProgressLedger(tmp_path)
    version = plan.snapshot()["plan_version"]
    ledger.assign(worker="b", role="researcher", node_id="B", plan_version=version, task="B")
    ledger.assign(worker="c", role="researcher", node_id="C", plan_version=version, task="C")

    blocked = reconcile(
        tmp_path,
        [_row("b", alive=True, label="working"), _row("c", alive=True, label="working")],
    )
    assert any(d["worker"] == "b" and d["reason"] == "dependencies_not_complete" for d in blocked["decisions"])

    ledger.set_node_state("A", state="DONE", actor="supervisor")
    unblocked = reconcile(
        tmp_path,
        [_row("b", alive=True, label="working"), _row("c", alive=True, label="working")],
    )
    assert any(d["worker"] == "b" and d["action"] == "CONTINUE" for d in unblocked["decisions"])

    _commit(plan, [{"op": "set_state", "node_id": "P", "state": "CANCELLED"}])
    retired = reconcile(
        tmp_path,
        [_row("b", alive=True, label="working"), _row("c", alive=True, label="working")],
    )
    assert any(d["worker"] == "c" and d["action"] == "STOP" and d["reason"] == "plan_route_retired"
               for d in retired["decisions"])


@contextmanager
def _agents_root(path: Path):
    old = os.environ.get("DANUS_AGENTS_ROOT")
    os.environ["DANUS_AGENTS_ROOT"] = str(path)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("DANUS_AGENTS_ROOT", None)
        else:
            os.environ["DANUS_AGENTS_ROOT"] = old


def test_supervise_cli_reconciles_and_records_node_completion(tmp_path: Path):
    with _agents_root(tmp_path / "projects"):
        cli.do_new("P", roles="high:1")
        pdir = tmp_path / "projects" / "P"
        ResearchPlanGraph.create(pdir, node_id="root", title="Root")

        report = cli.do_supervise("P")
        assert report["frontier"] == ["root"]
        assert _decision(report, action="RETASK", reason="ready_node_unassigned")

        done = cli.do_supervise("P", complete_node="root")
        assert done["frontier"] == []
        assert done["completed_nodes"] == ["root"]

        args = cli.build_parser().parse_args(["supervise", "P", "--reopen-node", "root"])
        assert args.cmd == "supervise" and args.reopen_node == "root"
