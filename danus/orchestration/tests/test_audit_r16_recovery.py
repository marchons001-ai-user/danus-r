import multiprocessing
import subprocess
import sys

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.core.tests.test_research_plan import _commit
from danus.execution import layout as L
from danus.gateway import server
from danus.gateway.tests.test_plan_gate import _env
from danus.orchestration import autonomy, cli, planning
from danus.orchestration.control import reconcile
from danus.orchestration.tests.test_s2_plan_handoffs import _root, _start_plan_review


@pytest.mark.parametrize("changed", ["group", "a"])
def test_proposal_requires_tasks_for_changed_ancestor_or_dependency(tmp_path, changed):
    graph = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    _commit(graph, [
        {"op": "add_node", "node_id": n, "title": n, "parent": p, "depends_on": d}
        for n, p, d in [("group", "root", []), ("a", "group", []),
                        ("b", "root", ["a"]), ("c", "root", ["b"]), ("other", "root", [])]
    ])
    raw = {"base_plan_version": 2, "reason": "changed upstream",
           "operations": [{"op": "set_dependencies", "node_id": changed, "depends_on": ["other"]}],
           "tasks": []}
    with pytest.raises(ValueError, match="task specs"):
        planning._normalize_proposal(graph, raw, tmp_path / "proposal.json")
    raw["tasks"] = [{"node_id": n, "role": "experimenter", "task": "new " + n} for n in ["a", "b", "c"]]
    proposal = planning._normalize_proposal(graph, raw, tmp_path / "proposal.json")
    _commit(graph, raw["operations"])
    planning._materialize_tasks(tmp_path, {"proposal": proposal}, 3)
    for n in ["a", "b", "c"]:
        assert not graph.node_compatible(n, 2)
        assert autonomy._task_spec(tmp_path, n)["status"] == "ready"
    assert graph.node_compatible("other", 2)


def test_prepared_revision_wins_competing_approvals_after_process_crash(tmp_path, monkeypatch):
    with _root(tmp_path):
        pdir, captured, review = _start_plan_review(tmp_path, monkeypatch)
        graph, ledger = ResearchPlanGraph(pdir), ProgressLedger(pdir)
        cid = captured["change_id"]
        graph.review_change(cid, reviewer="low2", verdict="APPROVE", rationale="ok",
                            review_assignment_id=review["assigned_assignment_id"])
        ledger.update(worker="low2", state="DONE", actor="runtime", outcome="COMPLETE")
        HandoffRequests(ledger).resolve(review["handoff_id"], assignment_id=review["assigned_assignment_id"],
                                       actor="supervisor", summary="done")
        other = graph.propose_change(base_plan_version=1, proposer="low", reason="[proposal:competing] bounded",
                                     operations=[{"op": "add_node", "node_id": "other", "title": "Other", "parent": "P0"}])
        graph.review_change(other, reviewer="other", verdict="APPROVE", rationale="ok")
        code = ("import os; from pathlib import Path; from danus.core import ResearchPlanGraph; "
                "ResearchPlanGraph._write_current=lambda *a: os._exit(73); "
                f"ResearchPlanGraph(Path({str(pdir)!r})).commit_change({cid!r}, committer='supervisor')")
        assert subprocess.run([sys.executable, "-c", code], timeout=20).returncode == 73
        assert graph.snapshot()["plan_version"] == 1
        prepared = graph.revision(2)
        planning.planning_step("P")
        planning.planning_step("P")
        assert graph.snapshot() == prepared
        assert graph.get_change(cid)["status"] == "COMMITTED"
        assert graph.get_change(other)["status"] == "STALE"
        assert (pdir / "tasks/v2/researcher/R1.md").is_file()
        assert not graph._revision_path(3).exists()


@pytest.mark.parametrize("subject", ["PLAN_CHANGE", "IMPLEMENTATION_CONTRACT"])
@pytest.mark.parametrize("worker_pause", [False, True])
def test_paused_reviewer_cannot_write_or_retry_verdict(tmp_path, subject, worker_pause):
    graph = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    if subject == "PLAN_CHANGE":
        sid = graph.propose_change(base_plan_version=1, proposer="r", reason="bounded",
                                  operations=[{"op": "add_node", "node_id": "a", "title": "A", "parent": "root"}])
        write = lambda: server.plan_review_change(sid, verdict="APPROVE", rationale="ok")
    else:
        sid = ledger.submit_implementation_contract(proposer="r", node_id="root", plan_version=1,
            goal="goal", must_preserve_invariants=["keep"], forbidden_behaviors=["avoid"],
            acceptance_conditions=["check"])["contract_id"]
        write = lambda: server.implementation_review_contract(sid, "APPROVE", "ok")
    handoff = HandoffRequests(ledger).open(kind="REVIEW", plan_version=1, node_id="root",
        subject_type=subject, subject_id=sid, requested_by="supervisor", source_role="researcher",
        request="review", reason="required")
    assignment = ledger.assign(worker="v", role="reviewer", node_id="root", plan_version=1,
                               task=handoff["request"], handoff_id=handoff["handoff_id"])
    pause = (tmp_path / "workers/v" if worker_pause else tmp_path) / L.PAUSE_FILE
    pause.parent.mkdir(parents=True, exist_ok=True)
    with _env(DANUS_PROJECT_DIR=tmp_path, DANUS_AUTHOR="v", DANUS_ROLE="reviewer",
              DANUS_ASSIGNMENT_ID=assignment["assignment_id"]):
        for already_written in [False, True]:
            pause.write_text("manual\n")
            before = ledger.path.read_bytes()
            with pytest.raises(ValueError, match="paused"):
                write()
            assert ledger.path.read_bytes() == before
            if subject == "PLAN_CHANGE":
                assert graph.get_change(sid)["status"] == ("APPROVED" if already_written else "PROPOSED")
            pause.unlink()
            write()


def test_identical_contract_after_reopen_gets_new_authority_at_same_time(tmp_path, monkeypatch):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    monkeypatch.setattr("danus.core.progress.utc_now", lambda: "2026-09-08T00:00:00Z")
    args = dict(proposer="r", node_id="root", plan_version=1, goal="goal",
                must_preserve_invariants=["keep"], forbidden_behaviors=["avoid"], acceptance_conditions=["check"])
    old = ledger.submit_implementation_contract(**args)
    ledger.review_implementation_contract(old["contract_id"], reviewer="v", verdict="APPROVE", rationale="ok")
    ledger.set_node_state("root", state="OPEN", actor="operator")
    new = ledger.submit_implementation_contract(**args)
    assert new["contract_id"] != old["contract_id"]
    assert new["status"] == "PROPOSED"
    assert ledger.submit_implementation_contract(**args)["contract_id"] == new["contract_id"]
    with pytest.raises(ValueError):
        ledger.assign(worker="e", role="experimenter", node_id="root", plan_version=1, task=new["task"])
    ledger.review_implementation_contract(new["contract_id"], reviewer="v", verdict="APPROVE", rationale="fresh")
    issued = ledger.assign(worker="e", role="experimenter", node_id="root", plan_version=1, task=new["task"])
    assert issued["implementation_contract_id"] == new["contract_id"]
    assert ledger.implementation_contract(old["contract_id"])["status"] == "STALE"


@pytest.mark.parametrize("verdict", ["APPROVE", "REVISE", "REJECT"])
@pytest.mark.parametrize("crash", [False, True])
def test_reopen_between_verdict_and_complete_requires_fresh_review(tmp_path, monkeypatch, verdict, crash):
    with _root(tmp_path):
        pdir, captured, review = _start_plan_review(tmp_path, monkeypatch)
        graph, ledger = ResearchPlanGraph(pdir), ProgressLedger(pdir)
        old = ledger.current("low2")
        cid = captured["change_id"]
        with _env(DANUS_PROJECT_DIR=pdir, DANUS_AUTHOR="low2", DANUS_ROLE="reviewer", DANUS_ASSIGNMENT_ID=old["assignment_id"]):
            server.plan_review_change(cid, verdict=verdict, rationale="old")
            ledger.set_node_state("P0", state="OPEN", actor="operator")
            if crash:
                code = ("import os; from danus.core import ResearchPlanGraph; "
                        "from danus.orchestration import planning; "
                        "write=ResearchPlanGraph._write_json; "
                        "ResearchPlanGraph._write_json=lambda self,path,data: "
                        "(write(self,path,data), os._exit(73)); planning.planning_step('P')")
                assert subprocess.run([sys.executable, "-c", code], timeout=20).returncode == 73
            for _ in range(3):
                planning.planning_step("P")
            with pytest.raises(ValueError):
                server.plan_review_change(cid, verdict=verdict, rationale="old")
        assert graph.get_change(cid)["status"] == "PROPOSED"
        new = ledger.current("low2")
        assert new["assignment_id"] != old["assignment_id"]
        with _env(DANUS_PROJECT_DIR=pdir, DANUS_AUTHOR="low2", DANUS_ROLE="reviewer", DANUS_ASSIGNMENT_ID=new["assignment_id"]):
            server.plan_review_change(cid, verdict="APPROVE", rationale="fresh")
            server.plan_review_change(cid, verdict="APPROVE", rationale="fresh")
        ledger.update(worker="low2", state="DONE", actor="runtime", outcome="COMPLETE")
        for _ in range(2):
            planning.planning_step("P")
        assert graph.get_change(cid)["status"] == "COMMITTED"
        assert graph.get_change(cid)["review_assignment_id"] == new["assignment_id"]
        assert HandoffRequests(ledger).get(review["handoff_id"])["resolution_assignment_id"] == new["assignment_id"]


def test_reassign_preserves_completion_but_reopen_revokes_it(tmp_path):
    graph = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    _commit(graph, [{"op": "add_node", "node_id": n, "title": n, "parent": "root"} for n in ["a", "b"]])
    ledger = ProgressLedger(tmp_path)
    path = tmp_path / "tasks/v2/researcher/a.md"
    path.parent.mkdir(parents=True)
    path.write_text("do a\n")
    old = ledger.assign(worker="r", role="researcher", node_id="a", plan_version=2, task="do a\n")
    ledger.update(worker="r", state="DONE", actor="runtime", outcome="COMPLETE")
    new = ledger.assign(worker="r", role="researcher", node_id="b", plan_version=2, task="do b")
    decisions = reconcile(tmp_path, [])['decisions']
    decision = next(d for d in decisions if d["node_id"] == "a")
    assert decision["reason"] == "completion_not_recorded"
    assert autonomy._auto_complete(tmp_path, decision)["result"] == "completed"
    assert ledger.current("r")["assignment_id"] == new["assignment_id"]
    ledger.set_node_state("a", state="OPEN", actor="operator")
    assert autonomy._auto_complete(tmp_path, decision)["result"] == "plan_binding_stale"
    with pytest.raises(ValueError, match="authoritative"):
        ledger.set_node_state("a", state="DONE", actor="supervisor", expected_assignment_id=old["assignment_id"])
    assert "a" not in ledger.completed_nodes()


def _resume_blocked(target, connection):
    def start(wl):
        connection.send("starting")
        assert connection.recv() == "release"
        return "started"
    cli._start_one = start
    cli.do_resume(target, stagger=0)


def _pause_process(target, connection):
    cli._stop_one = lambda *a, **k: "not-running"
    connection.send("attempting")
    cli.do_pause(target)
    connection.send("paused")


@pytest.mark.parametrize("worker", [False, True])
def test_pause_waits_for_whole_resume_transaction(tmp_path, worker):
    with _root(tmp_path):
        cli.do_new("P", roles="low:1")
        target = "P/low" if worker else "P"
        ctx = multiprocessing.get_context("fork")
        parent, child = ctx.Pipe()
        paused, pause_child = ctx.Pipe()
        resume = ctx.Process(target=_resume_blocked, args=(target, child))
        pause = ctx.Process(target=_pause_process, args=(target, pause_child))
        try:
            resume.start()
            assert parent.poll(10) and parent.recv() == "starting"
            pause.start()
            assert paused.poll(10) and paused.recv() == "attempting"
            assert not paused.poll(0.3)
            parent.send("release")
            resume.join(10)
            pause.join(10)
            assert resume.exitcode == pause.exitcode == 0
            assert paused.recv() == "paused"
            directory = L.worker_dir("P", "low") if worker else L.project_dir("P")
            assert (directory / L.PAUSE_FILE).is_file()
        finally:
            for proc in [resume, pause]:
                if proc.is_alive():
                    proc.terminate()
                if proc.pid:
                    proc.join(10)
            for pipe in [parent, child, paused, pause_child]:
                pipe.close()
