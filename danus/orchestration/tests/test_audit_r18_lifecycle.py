import subprocess
import sys

from danus.core import ProgressLedger
from danus.core.handoff import HandoffRequests
from danus.execution import layout as L
from danus.gateway import server
from danus.orchestration import cli, planning, autonomy as A, supervisor_llm as S
from danus.orchestration.control import build_supervisor_snapshot, reduce_supervisor_snapshot
from danus.orchestration.tests.test_s4_scientific_workflow_e2e import _root, _gateway, _finish
from danus.orchestration.tests.test_s2_contract_and_generic_handoffs import _contract
from danus.orchestration.tests.test_s6_scientific_route_closure import _resolved_route, _luna


def _supervise():
    return cli.do_supervise("P", apply=True)


def _task(root, role, text):
    path = root / "tasks" / "v1" / role / "P0.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _review(root, ledger, contract):
    _supervise()
    with _gateway(root, "low2", "reviewer"):
        server.implementation_review_contract(contract["contract_id"], verdict="APPROVE", rationale="bounded")
    _finish("P", "low2")
    report = cli.do_supervise("P", apply=False)
    assert report["project_state"] == "RUNNING"
    assert any(a["kind"] == "ASSIGN_CONTRACT" for a in report["actions"])
    _supervise()
    execution = ledger.current("low3")
    assert execution["implementation_contract_id"] == contract["contract_id"]
    return execution


def test_86_production_supervisor_waits_for_exact_execution_and_interpretation(tmp_path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        monkeypatch.setattr(cli, "_stop_one", lambda *a, **k: "not-running")
        planning.initialize_research("P", brief="bounded question")
        root = L.project_dir("P")
        ledger = ProgressLedger(root)
        _task(root, "researcher", (root / "workers/low/TASK.md").read_text())
        contract = _contract(ledger)
        _finish("P", "low")
        execution = _review(root, ledger, contract)
        assert "P0" in ledger.completed_nodes()
        assert _supervise()["project_state"] == "RUNNING"
        ledger.update(worker="low3", state="RUNNING", actor="runtime")
        assert _supervise()["project_state"] == "RUNNING"
        handoffs = HandoffRequests(ledger)
        route = handoffs.open(kind="INTERPRET", plan_version=1, node_id="P0", subject_type="ASSIGNMENT",
                              subject_id=execution["assignment_id"], requested_by="low3", source_role="experimenter",
                              source_assignment_id=execution["assignment_id"], request="interpret bounded evidence", reason="interpret required")
        _finish("P", "low3")
        assert _supervise()["project_state"] == "RUNNING"
        assert ledger.current("low")["handoff_id"] == route["handoff_id"]
        _finish("P", "low")
        assert _supervise()["project_state"] == "COMPLETE"
        assert handoffs.get(route["handoff_id"])["status"] == "RESOLVED"


def test_93_reopen_creates_fresh_reviewed_contract_without_restoring_old_authority(tmp_path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        monkeypatch.setattr(cli, "_stop_one", lambda *a, **k: "not-running")
        planning.initialize_research("P", brief="bounded question")
        root = L.project_dir("P")
        ledger = ProgressLedger(root)
        source_task = _task(root, "researcher", (root / "workers/low/TASK.md").read_text())
        old = _contract(ledger)
        _finish("P", "low")
        first = _review(root, ledger, old)
        _finish("P", "low3")
        assert _supervise()["project_state"] == "COMPLETE"
        source_task.unlink()
        _task(root, "experimenter", old["task"])
        ledger.set_node_state("P0", state="OPEN", actor="operator")
        for _ in range(2):
            _supervise()
        handoffs = HandoffRequests(ledger)
        routes = [h for h in handoffs.all() if h.get("reauthorization")]
        assert len(routes) == 1 and routes[0]["status"] == "ASSIGNED"
        assert ledger.current("low")["handoff_id"] == routes[0]["handoff_id"]
        assert ledger.implementation_contract(old["contract_id"])["status"] == "STALE"
        with _gateway(root, "low", "researcher"):
            fresh = server.implementation_submit_contract(node_id="P0", plan_version=1, goal=old["goal"],
                must_preserve_invariants=old["must_preserve_invariants"], forbidden_behaviors=old["forbidden_behaviors"],
                acceptance_conditions=old["acceptance_conditions"])
        assert fresh["contract_id"] != old["contract_id"]
        fresh = ledger.implementation_contract(fresh["contract_id"])
        assert fresh["replaces_contract_id"] == old["contract_id"]
        _finish("P", "low")
        second = _review(root, ledger, fresh)
        assert second["assignment_id"] != first["assignment_id"]
        assert ledger.implementation_contract(old["contract_id"])["status"] == "STALE"
        assert handoffs.get(routes[0]["handoff_id"])["status"] == "RESOLVED"


def test_91_two_review_epochs_each_have_one_review_and_route_after_restart(tmp_path, monkeypatch):
    ledger, source, _, old_route, _ = _resolved_route(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
    S.apply_scientific_route_dispositions("p")
    ledger.update(worker="experimenter", state="BLOCKED", outcome="NEEDS_REVIEW", actor="runtime", note="independent ambiguity")
    assert ledger.current("experimenter")["review_epoch"] == 2
    assert HandoffRequests(ledger).get(old_route["handoff_id"])["status"] == "STALE"
    def report():
        return reduce_supervisor_snapshot(build_supervisor_snapshot(tmp_path, []))
    action = next(a for a in report()["actions"] if a["kind"] == "OPEN_HANDOFF" and a.get("subject_type") == "ASSIGNMENT")
    for _ in range(2):
        A._open_nonplan_handoff(ProgressLedger(tmp_path), HandoffRequests(ProgressLedger(tmp_path)), action)
    handoffs = HandoffRequests(ledger)
    review = next(h for h in handoffs.all() if h["kind"] == "REVIEW" and h.get("review_epoch") == 2)
    reviewer = ledger.assign(worker="reviewer", role="reviewer", node_id="root", plan_version=1,
                             task=review["request"], handoff_id=review["handoff_id"])
    ledger.update(worker="reviewer", state="DONE", outcome="COMPLETE", actor="runtime",
                  result="DANUS_REVIEW_CLASS: SCIENTIFIC_AMBIGUITY\nDANUS_OUTCOME: COMPLETE")
    handoffs.resolve(review["handoff_id"], assignment_id=reviewer["assignment_id"], actor="supervisor", summary="independent review")
    action = next(a for a in report()["actions"] if a["kind"] == "SCIENTIFIC_ROUTE_REQUIRED")
    calls = []
    monkeypatch.setattr(S, "_run_luna", lambda prompt: calls.append(1) or _luna("INTERPRET"))
    first = S.resolve_scientific_routes(tmp_path, [action])[0]
    repeated = S.resolve_scientific_routes(tmp_path, [action])[0]
    assert first["handoff_id"] == repeated["handoff_id"] != old_route["handoff_id"]
    assert calls == [1]
    code = "from pathlib import Path; from danus.core import ProgressLedger; from danus.core.handoff import HandoffRequests; import sys; hs=HandoffRequests(ProgressLedger(Path(sys.argv[1]))).all(); assert len([h for h in hs if h.get('review_epoch')==2 and h['kind']=='REVIEW'])==1; assert len([h for h in hs if h.get('review_epoch')==2 and h['kind']=='INTERPRET'])==1"
    assert subprocess.run([sys.executable, "-c", code, str(tmp_path)], timeout=15).returncode == 0
