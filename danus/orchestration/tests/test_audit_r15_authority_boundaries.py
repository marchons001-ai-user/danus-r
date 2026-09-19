from pathlib import Path
import subprocess
import sys
import multiprocessing

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.core.tests.test_handoff import _open, _bind
from danus.core.tests.test_research_plan import _commit
from danus.execution import layout as L
from danus.execution import loop as LOOP
from danus.orchestration.tests.test_audit_r8_assignment_task_identity import (
    _worker, _run, _message,
)


@pytest.mark.parametrize("project_pause", [False, True])
@pytest.mark.parametrize("before_start", [False, True])
@pytest.mark.parametrize("migration_requested", [False, True])
def test_durable_pause_prevents_round_after_interrupted_stop(
    tmp_path: Path, monkeypatch, project_pause, before_start, migration_requested,
):
    root, worker, ledger, assignment = _worker(tmp_path, monkeypatch)
    pause = root / L.PAUSE_FILE if project_pause else L.WorkerLayout(worker).pause
    monkeypatch.setenv("DANUS_MAX_ROUNDS", "2")
    calls = []
    monkeypatch.setattr(LOOP.C, "budget_reason", lambda *a: "context_budget" if migration_requested else None)
    monkeypatch.setattr(LOOP, "_run_context_migration", lambda *a: pytest.fail("paused migration must not execute"))

    def interrupted_pause():
        target = "P" if project_pause else "P/researcher"
        code = (
            "import os; from danus.orchestration import cli; "
            "cli._stop_one=lambda *a, **k: os._exit(73); "
            f"cli.do_pause({target!r})"
        )
        assert subprocess.run([sys.executable, "-c", code], timeout=10).returncode == 73

    if before_start:
        interrupted_pause()

    def round_(wl, role, prompt, log_path, hard_timeout, **kwargs):
        calls.append(1)
        _message(log_path, "bounded progress")
        interrupted_pause()
        return 0

    monkeypatch.setattr(LOOP, "run_round", round_)
    assert _run(worker) == 0
    assert len(calls) == (0 if before_start else 1)
    assert pause.is_file()
    assert ledger.current("researcher")["assignment_id"] == assignment["assignment_id"]


def test_changed_transitive_dependency_invalidates_downstream_authority(tmp_path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    _commit(plan, [
        {"op": "add_node", "node_id": nid, "title": nid, "parent": "root", "depends_on": deps}
        for nid, deps in [("a", []), ("b", ["a"]), ("c", ["b"]), ("other", [])]
    ])
    ledger = ProgressLedger(tmp_path)
    assignment = ledger.assign(worker="r", role="researcher", node_id="c", plan_version=2, task="c")
    for nid in ["a", "b", "c", "other"]:
        ledger.set_node_state(nid, state="DONE", actor="supervisor")
    _commit(plan, [{"op": "set_dependencies", "node_id": "a", "depends_on": ["other"]}])
    assert ledger.current("r")["state"] == "STALE"
    assert ledger.completed_nodes() == ["other"]
    assert not plan.node_compatible("c", assignment["plan_version"])


def _complete_together(root, barrier, results, message):
    barrier.wait(10)
    try:
        ProgressLedger(root).update(worker="r", state="DONE", actor="runtime", outcome="COMPLETE", result=message)
        results.put(("accepted", message))
    except ValueError:
        results.put(("rejected", message))


def test_conflicting_completion_processes_preserve_one_result(tmp_path):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    ledger.assign(worker="r", role="researcher", node_id="root", plan_version=1, task="task")
    ctx = multiprocessing.get_context("fork")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    children = [ctx.Process(target=_complete_together, args=(tmp_path, barrier, results, text)) for text in ["first", "second"]]
    try:
        for child in children:
            child.start()
        for child in children:
            child.join(10)
            assert child.exitcode == 0
        outcomes = [results.get(timeout=1) for _ in children]
        assert sorted(status for status, _ in outcomes) == ["accepted", "rejected"]
        assert ledger.current("r")["result"] == next(message for status, message in outcomes if status == "accepted")
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
            child.join(10)
        results.close()


def test_old_assigned_handoff_retry_cannot_replace_current_task(tmp_path):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    handoffs = HandoffRequests(ledger)
    request = _open(handoffs, 1)
    old = _bind(ledger, request)
    ledger.update(worker="v1", state="DONE", actor="runtime", outcome="COMPLETE")
    worker = tmp_path / "workers" / "v1"
    worker.mkdir(parents=True)
    new = ledger.assign(worker="v1", role="reviewer", node_id="root", plan_version=1, task="new task\n", write_task=True)
    with pytest.raises(ValueError):
        ledger.assign(worker="v1", role="reviewer", node_id="root", plan_version=1,
                      task=request["request"], handoff_id=request["handoff_id"], write_task=True)
    assert (worker / "TASK.md").read_text(encoding="utf-8") == "new task\n"
    assert ledger.current("v1")["assignment_id"] == new["assignment_id"] != old["assignment_id"]


def test_completed_assignment_rejects_conflicting_terminal_result(tmp_path):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    ledger.assign(worker="r", role="researcher", node_id="root", plan_version=1, task="task")
    first = ledger.update(worker="r", state="DONE", actor="runtime", outcome="COMPLETE", result="first", checkpoint_id="one")
    assert ledger.update(worker="r", state="DONE", actor="runtime", outcome="COMPLETE", result="first") == first
    with pytest.raises(ValueError):
        ledger.update(worker="r", state="DONE", actor="runtime", outcome="COMPLETE", result="conflicting", checkpoint_id="two")
    assert ledger.current("r") == first


def test_reopen_invalidates_dependents_of_reopened_ancestor(tmp_path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    _commit(plan, [
        {"op": "add_node", "node_id": "group", "title": "Group", "parent": "root"},
        {"op": "add_node", "node_id": "leaf", "title": "Leaf", "parent": "group"},
        {"op": "add_node", "node_id": "dependent", "title": "Dependent", "parent": "root", "depends_on": ["group"]},
        {"op": "add_node", "node_id": "other", "title": "Other", "parent": "root"},
    ])
    ledger = ProgressLedger(tmp_path)
    ledger.assign(worker="r", role="researcher", node_id="dependent", plan_version=2, task="dependent")
    for nid in ["root", "group", "leaf", "dependent", "other"]:
        ledger.set_node_state(nid, state="DONE", actor="supervisor")
    ledger.set_node_state("leaf", state="OPEN", actor="operator")
    assert ledger.completed_nodes() == ["other"]
    assert ledger.current("r")["state"] == "STALE"
