import subprocess
import sys

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.execution import layout as L
from danus.gateway import server
from danus.gateway.tests.test_plan_gate import _env
from danus.orchestration import cli, planning
from danus.orchestration.control import reconcile
from danus.orchestration.tests.test_s2_plan_handoffs import _root, _start_plan_review
from danus.orchestration.tests.test_s2_contract_and_generic_handoffs import _contract, _apply


def _crash(code):
    assert subprocess.run([sys.executable, "-c", code], timeout=20).returncode == 73


def _approved(tmp_path, monkeypatch):
    pdir, captured, review = _start_plan_review(tmp_path, monkeypatch)
    graph, ledger = ResearchPlanGraph(pdir), ProgressLedger(pdir)
    aid = review["assigned_assignment_id"]
    with _env(DANUS_PROJECT_DIR=pdir, DANUS_AUTHOR="low2", DANUS_ROLE="reviewer", DANUS_ASSIGNMENT_ID=aid):
        server.plan_review_change(captured["change_id"], verdict="APPROVE", rationale="bounded")
    ledger.update(worker="low2", state="DONE", actor="runtime", outcome="COMPLETE")
    HandoffRequests(ledger).resolve(review["handoff_id"], assignment_id=aid, actor="supervisor", summary="done")
    return pdir, graph, ledger, captured["change_id"], review


@pytest.mark.parametrize("entry", ["planning", "supervise", "watch", "initialize"])
@pytest.mark.parametrize("stop", ["tokens", "deadline", "pause"])
def test_prepared_recovery_precedes_every_policy(tmp_path, monkeypatch, entry, stop):
    with _root(tmp_path):
        pdir, graph, ledger, cid, review = _approved(tmp_path, monkeypatch)
        other = graph.propose_change(base_plan_version=1, proposer="low", reason="[proposal:other] competing",
            operations=[{"op": "add_node", "node_id": "other", "title": "Other", "parent": "P0"}])
        graph.review_change(other, reviewer="v", verdict="APPROVE", rationale="ok")
        _crash("import os; from pathlib import Path; from danus.core import ResearchPlanGraph; "
               "ResearchPlanGraph._write_current=lambda *a: os._exit(73); "
               f"ResearchPlanGraph(Path({str(pdir)!r})).commit_change({cid!r}, committer='supervisor')")
        assert graph.snapshot()["plan_version"] == 1
        count = len(ledger.assignments())
        if stop == "deadline":
            (pdir / L.DEADLINE_FILE).write_text("0")
        if stop == "pause":
            (pdir / L.PAUSE_FILE).write_text("manual")
        rows = cli._project_rows
        def used(project):
            return [{**r, "usage_tokens_total": 100, "usage_complete": True} for r in rows(project)]
        monkeypatch.setattr(cli, "_project_rows", used)
        def recovered():
            assert graph.snapshot()["plan_version"] == 2
            assert graph.get_change(cid)["status"] == "COMMITTED"
            assert (pdir / "tasks/v2/researcher/R1.md").is_file()
        load = cli.Q.load_quota
        def quota(*args, **kwargs):
            recovered()
            return load(*args, **kwargs)
        monkeypatch.setattr(cli.Q, "load_quota", quota)
        monkeypatch.setattr(cli.Q, "refresh_quota", lambda *a: recovered())
        monkeypatch.setattr(cli, "_start_one", lambda *a: pytest.fail("recovery must not start work"))
        monkeypatch.setattr(planning, "_capture_proposal", lambda *a: pytest.fail("stopped project must not capture work"))
        if entry == "planning":
            actions = planning.planning_step("P", token_budget=1)
            assert actions and all(a["action"] == "PLAN_TASK_RECOVERY" for a in actions)
        elif entry == "supervise":
            cli.do_supervise("P", apply=True, token_budget=1)
        elif entry == "initialize":
            planning.initialize_research("P")
        else:
            watcher = cli.watch_supervisor("P", token_budget=1)
            next(watcher)
            watcher.close()
        recovered()
        assert len(ledger.assignments()) == count
        assert graph.get_change(other)["status"] == "STALE"
        assert not graph._revision_path(3).exists()


@pytest.mark.parametrize("cut", ["current", "change"])
def test_transaction_recovery_survives_another_process_crash(tmp_path, monkeypatch, cut):
    with _root(tmp_path):
        pdir, graph, ledger, cid, review = _approved(tmp_path, monkeypatch)
        _crash("import os; from pathlib import Path; from danus.core import ResearchPlanGraph; "
               "ResearchPlanGraph._write_current=lambda *a: os._exit(73); "
               f"ResearchPlanGraph(Path({str(pdir)!r})).commit_change({cid!r}, committer='supervisor')")
        method = "_write_current" if cut == "current" else "_write_json"
        ledger.set_node_state("P0", state="OPEN", actor="operator")
        _crash("import os; from danus.core import ResearchPlanGraph; from danus.orchestration import cli; "
               f"write=ResearchPlanGraph.{method}; ResearchPlanGraph.{method}=lambda *a: (write(*a),os._exit(73)); "
               "cli.do_supervise('P')")
        (pdir / L.PAUSE_FILE).write_text("manual")
        for _ in range(2):
            cli.do_supervise("P")
        assert graph.get_change(cid)["status"] == "COMMITTED"
        assert (pdir / "tasks/v2/researcher/R1.md").is_file()
        assert not graph._revision_path(3).exists()
        assert ledger.current("low2")["state"] == "STALE"


@pytest.mark.parametrize("cut", ["none", "epoch", "handoff", "cleared"])
def test_resolved_review_revocation_creates_crash_safe_fresh_epoch(tmp_path, monkeypatch, cut):
    with _root(tmp_path):
        pdir, graph, ledger, cid, old = _approved(tmp_path, monkeypatch)
        _crash("import os; from pathlib import Path; from danus.core import ResearchPlanGraph; "
               "ResearchPlanGraph._write_revision=lambda *a: os._exit(73); "
               f"ResearchPlanGraph(Path({str(pdir)!r})).commit_change({cid!r}, committer='supervisor')")
        ledger.set_node_state("P0", state="OPEN", actor="operator")
        if cut == "cleared":
            record = graph.get_change(cid)
            record.update(status="PROPOSED", reviewer=None, review_verdict=None,
                          review_rationale="", reviewed_at=None)
            record.pop("review_assignment_id", None)
            graph._write_json(graph._change_path(cid), record)
        elif cut == "epoch":
            _crash("import os; from danus.core import ResearchPlanGraph; from danus.orchestration import planning; "
                   "write=ResearchPlanGraph._write_json; ResearchPlanGraph._write_json=lambda *a: (write(*a),os._exit(73)); "
                   "planning.planning_step('P')")
        elif cut == "handoff":
            _crash("import os; from danus.core.handoff import HandoffRequests; from danus.orchestration import planning; "
                   "opened=HandoffRequests.open; HandoffRequests.open=lambda *a,**k: (opened(*a,**k),os._exit(73)); "
                   "planning.planning_step('P')")
        for _ in range(3):
            planning.planning_step("P")
        change = graph.get_change(cid)
        assert change["status"] == "PROPOSED" and change["review_epoch"] == 1
        handoffs = HandoffRequests(ledger)
        fresh = planning._plan_handoff(pdir, "REVIEW", cid)
        assert fresh["status"] == "ASSIGNED" and fresh["handoff_id"] != old["handoff_id"]
        assert handoffs.get(old["handoff_id"])["status"] == "STALE"
        with pytest.raises(ValueError):
            handoffs.reopen(old["handoff_id"], assignment_id=old["assigned_assignment_id"], actor="supervisor", reason="retry")
        with _env(DANUS_PROJECT_DIR=pdir, DANUS_AUTHOR="low2", DANUS_ROLE="reviewer", DANUS_ASSIGNMENT_ID=old["assigned_assignment_id"]):
            with pytest.raises(ValueError):
                server.plan_review_change(cid, verdict="APPROVE", rationale="bounded")
        with _env(DANUS_PROJECT_DIR=pdir, DANUS_AUTHOR="low2", DANUS_ROLE="reviewer", DANUS_ASSIGNMENT_ID=fresh["assigned_assignment_id"]):
            server.plan_review_change(cid, verdict="APPROVE", rationale="fresh")
        ledger.update(worker="low2", state="DONE", actor="runtime", outcome="COMPLETE")
        for _ in range(2):
            planning.planning_step("P")
        assert graph.get_change(cid)["status"] == "COMMITTED"
        assert graph.get_change(cid)["review_assignment_id"] == fresh["assigned_assignment_id"]
        assert handoffs.get(old["handoff_id"])["status"] == "STALE"


def test_direct_commit_cannot_use_revoked_resolved_review(tmp_path, monkeypatch):
    with _root(tmp_path):
        pdir, graph, ledger, cid, old = _approved(tmp_path, monkeypatch)
        ledger.set_node_state("P0", state="OPEN", actor="operator")
        with pytest.raises(ValueError, match="review authority"):
            graph.commit_change(cid, committer="supervisor")
        assert graph.snapshot()["plan_version"] == 1
        assert not graph._revision_path(2).exists()


@pytest.mark.parametrize("kind", ["REVIEW", "REVISION", "RETHINK"])
@pytest.mark.parametrize("assigned", [False, True])
def test_stale_contract_retires_handoffs_before_retry(tmp_path, monkeypatch, kind, assigned):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda *a: "started")
        planning.initialize_research("P", brief="bounded")
        pdir = L.project_dir("P")
        ledger = ProgressLedger(pdir)
        ledger.update(worker="low", state="DONE", actor="runtime", outcome="COMPLETE")
        contract = _contract(ledger)
        cid = contract["contract_id"]
        if kind != "REVIEW":
            ledger.review_implementation_contract(cid, reviewer="low2", verdict="REVISE" if kind == "REVISION" else "REJECT", rationale="correct")
        handoffs = HandoffRequests(ledger)
        old = handoffs.open(kind=kind, plan_version=1, node_id="P0", subject_type="IMPLEMENTATION_CONTRACT",
            subject_id=cid, requested_by="supervisor", source_role="researcher", request="bounded review", reason="required")
        who = "low2" if kind == "REVIEW" else "low"
        if assigned:
            ledger.assign(worker=who, role=old["target_role"], node_id="P0", plan_version=1,
                          task=old["request"], handoff_id=old["handoff_id"])
        ledger.set_node_state("P0", state="OPEN", actor="operator")
        for _ in range(3):
            _apply("P")
        assert handoffs.get(old["handoff_id"])["status"] == "STALE"
        assert not handoffs.pending()
        with pytest.raises(ValueError):
            ledger.assign(worker=who, role=old["target_role"], node_id="P0", plan_version=1,
                          task=old["request"], handoff_id=old["handoff_id"])
        fresh = _contract(ledger)
        assert fresh["contract_id"] != cid
        for _ in range(2):
            _apply("P")
        new_review = next(h for h in handoffs.pending() if h["subject_id"] == fresh["contract_id"])
        assert new_review["status"] == "ASSIGNED"
        assert handoffs.get(old["handoff_id"])["status"] == "STALE"
        assert all(a.get("handoff_id") != old["handoff_id"] for a in reconcile(pdir, cli._project_rows("P"))["actions"])
        new_assignment = ledger.current("low2")
        with _env(DANUS_PROJECT_DIR=pdir, DANUS_AUTHOR="low2", DANUS_ROLE="reviewer", DANUS_ASSIGNMENT_ID=new_assignment["assignment_id"]):
            with pytest.raises(ValueError):
                server.implementation_review_contract(cid, "APPROVE", "old")
            server.implementation_review_contract(fresh["contract_id"], "APPROVE", "fresh")
