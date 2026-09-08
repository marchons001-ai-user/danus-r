import multiprocessing
import runpy
import subprocess
import sys

import pytest

from danus.core import ResearchPlanGraph
from danus.execution import layout as L
from danus.execution import loop as LOOP
from danus.orchestration import cli
from danus.orchestration.tests.test_audit_r8_assignment_task_identity import _worker, _run, _message


@pytest.mark.parametrize("quota_pause", [False, True])
def test_budget_stop_never_executes_scientific_round(tmp_path, monkeypatch, quota_pause):
    root, worker, ledger, assignment = _worker(tmp_path, monkeypatch)
    monkeypatch.setenv("DANUS_MAX_ROUNDS", "1")
    if quota_pause:
        L.WorkerLayout(worker).pause.write_text("quota:exhausted:test\n")
    rows = cli._project_rows
    monkeypatch.setattr(cli, "_project_rows", lambda project: [
        {**row, "usage_complete": True, "usage_tokens_total": 100} for row in rows(project)
    ])
    monkeypatch.setattr(cli.Q, "load_quota", lambda *a: None)
    calls = []
    def round_(wl, role, prompt, log_path, hard_timeout, **kwargs):
        calls.append(ResearchPlanGraph(root).snapshot()["plan_version"])
        _message(log_path, "bounded work\nDANUS_OUTCOME: COMPLETE")
        return 0
    monkeypatch.setattr(LOOP, "run_round", round_)
    def start(wl):
        assert _run(wl.dir) == 0
        return "started"
    monkeypatch.setattr(cli, "_start_one", start)
    monkeypatch.setattr(cli, "_stop_one", lambda *a, **k: "not-running")
    report = cli.do_supervise("P", apply=True, token_budget=1)
    assert report["budget_stop"] == "resource_token_budget"
    print({"budget_stop": report["budget_stop"], "round_calls": calls, "quota_applied": report["quota_applied"]})
    assert calls == []


@pytest.mark.parametrize("recover_first", [False, True])
def test_direct_assign_does_not_execute_prepared_cancelled_node(tmp_path, monkeypatch, recover_first):
    root, worker, ledger, assignment = _worker(tmp_path, monkeypatch)
    graph = ResearchPlanGraph(root)
    cid = graph.propose_change(base_plan_version=1, proposer="r", reason="retire route",
        operations=[{"op": "set_state", "node_id": "root", "state": "CANCELLED"}])
    graph.review_change(cid, reviewer="v", verdict="APPROVE", rationale="retire")
    code = ("import os; from pathlib import Path; from danus.core import ResearchPlanGraph; "
            "ResearchPlanGraph._write_current=lambda *a: os._exit(73); "
            f"ResearchPlanGraph(Path({str(root)!r})).commit_change({cid!r}, committer='supervisor')")
    assert subprocess.run([sys.executable, "-c", code], timeout=15).returncode == 73
    assert graph.revision(2)["nodes"]["root"]["state"] == "CANCELLED"
    if recover_first:
        cli._recover_project(root)
    calls = []
    def round_(wl, role, prompt, log_path, hard_timeout, **kwargs):
        calls.append(graph.snapshot()["plan_version"])
        _message(log_path, "work on cancelled route\nDANUS_OUTCOME: COMPLETE")
        return 0
    monkeypatch.setattr(LOOP, "run_round", round_)
    monkeypatch.setenv("DANUS_MAX_ROUNDS", "1")
    try:
        issued = cli.do_assign("P/researcher", "new work", node_id="root")
    except SystemExit:
        issued = None
    if issued:
        assert _run(worker) == 0
    print({"issued": issued, "round_calls": calls, "current_version": graph.snapshot()["plan_version"]})
    assert calls == []


def _pause_blocked(connection):
    def stop(*args, **kwargs):
        connection.send("pause_written")
        assert connection.recv() == "release"
        return "not-running"
    cli._stop_one = stop
    cli.do_pause("P")


def _resume_public(connection, public):
    cli._start_one = lambda *a: "started"
    if public:
        sys.argv = ["danus", "resume", "P"]
        try:
            runpy.run_module("danus.orchestration", run_name="__main__")
        except SystemExit as exc:
            assert exc.code in (0, None)
    else:
        cli.do_resume("P", stagger=0)
    connection.send("resumed")


@pytest.mark.parametrize("public", [False, True])
def test_public_resume_serializes_with_pause(tmp_path, monkeypatch, public):
    root, worker, ledger, assignment = _worker(tmp_path, monkeypatch)
    ctx = multiprocessing.get_context("fork")
    parent, child = ctx.Pipe()
    result, remote = ctx.Pipe()
    pause = ctx.Process(target=_pause_blocked, args=(child,))
    resume = ctx.Process(target=_resume_public, args=(remote, public))
    try:
        pause.start()
        assert parent.poll(10) and parent.recv() == "pause_written"
        resume.start()
        raced = result.poll(1)
        print({"public": public, "resume_finished_while_pause_locked": raced,
               "pause_file_exists": (root / L.PAUSE_FILE).exists()})
        assert not raced
    finally:
        if pause.is_alive():
            parent.send("release")
        for process in [pause, resume]:
            if process.pid:
                process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(10)
        for pipe in [parent, child, result, remote]:
            pipe.close()
