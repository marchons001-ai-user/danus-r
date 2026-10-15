"""Completion is distinct from unresolved operator escalations.

Use the real ledger, HandoffRequests and pure Supervisor reducer. No model calls.
"""
from __future__ import annotations

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.orchestration import cli
from danus.orchestration.control import reconcile
from danus.tui.state import load_project_snapshot


def _finished_with_open_main(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    project = projects / "P"
    project.mkdir(parents=True)
    worker = project / "workers" / "low3"
    worker.mkdir(parents=True)
    (worker / ".role").write_text("ROLE=experimenter\n", encoding="utf-8")
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(projects))
    ResearchPlanGraph.create(project, node_id="P0", title="Pilot")
    ledger = ProgressLedger(project)
    assignment = ledger.assign(
        worker="low3", role="experimenter", node_id="P0",
        plan_version=1, task="One small pilot",
    )
    ledger.update(
        worker="low3", state="DONE", actor="runtime",
        outcome="COMPLETE", result="Finished experimental pilot",
    )
    ledger.set_node_state(
        "P0", state="DONE", actor="supervisor",
        expected_assignment_id=assignment["assignment_id"],
    )
    handoff = HandoffRequests(ledger).open(
        kind="ESCALATE", plan_version=1, node_id="P0",
        subject_type="ASSIGNMENT", subject_id=assignment["assignment_id"],
        source_assignment_id=assignment["assignment_id"],
        requested_by="low3", source_role="experimenter",
        request="An older environment issue requires operator acknowledgment.",
        reason="The old issue is not yet closed by Main.",
    )
    return project, ledger, handoff


def test_finished_science_with_main_escalation_is_action_required(tmp_path, monkeypatch):
    project, ledger, handoff = _finished_with_open_main(tmp_path, monkeypatch)
    before_ledger = ledger.path.read_bytes()
    before_handoff = HandoffRequests(ledger).path.read_bytes()

    report = reconcile(project, [])
    assert report["scientific_complete"] is True
    assert report["project_state"] == "ACTION_REQUIRED"
    assert report["decisions"] == []
    assert report["actions"] == []
    assert report["pending_main_escalations"][0]["handoff_id"] == handoff["handoff_id"]

    snapshot = load_project_snapshot("P")
    assert snapshot["project_state"] == report["project_state"]
    assert snapshot["scientific_complete"] is True
    assert snapshot["pending_main_escalations"] == report["pending_main_escalations"]
    assert HandoffRequests(ledger).get(handoff["handoff_id"])["status"] == "OPEN"
    assert ledger.path.read_bytes() == before_ledger
    assert HandoffRequests(ledger).path.read_bytes() == before_handoff


def test_legal_operator_cancellation_results_in_complete(tmp_path, monkeypatch):
    project, ledger, handoff = _finished_with_open_main(tmp_path, monkeypatch)
    HandoffRequests(ledger).cancel(
        handoff["handoff_id"], actor="main",
        reason="Operator checked evidence and marked superseded; not a scientific reapproval.",
    )
    assert reconcile(project, [])["project_state"] == "COMPLETE"
    assert load_project_snapshot("P")["project_state"] == "COMPLETE"


def test_non_main_pending_work_must_not_be_hidden(tmp_path, monkeypatch):
    project, ledger, handoff = _finished_with_open_main(tmp_path, monkeypatch)
    HandoffRequests(ledger).open(
        kind="RETHINK", plan_version=1, node_id="P0",
        subject_type="ASSIGNMENT",
        subject_id=handoff["subject_id"],
        source_assignment_id=handoff["source_assignment_id"],
        requested_by="supervisor", source_role="experimenter",
        request="Another unresolved research prerequisite",
        reason="Must not be treated as an operator-only backlog.",
    )
    report = reconcile(project, [])
    assert report["scientific_complete"] is True
    assert report["project_state"] == "RUNNING"
    assert load_project_snapshot("P")["project_state"] == "RUNNING"


def test_watch_supervisor_exits_on_action_required_without_mutation(tmp_path, monkeypatch):
    project, ledger, handoff = _finished_with_open_main(tmp_path, monkeypatch)
    # The real _project_rows can be read without starting any worker; patch
    # only telemetry/control invocations to isolate this watch-loop condition.
    monkeypatch.setattr(cli.Q, "refresh_quota", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_project_rows", lambda *a, **kw: [])
    monkeypatch.setattr(cli, "_resource_snapshot", lambda *a, **kw: {"tokens": {}, "time": {}})
    monkeypatch.setattr(cli, "_control_signature", lambda *a, **kw: "static")
    monkeypatch.setattr(cli, "do_supervise", lambda *a, **kw: {
        **reconcile(project, []), "budget": {"tokens": {}, "time": {}},
    })
    reports = list(cli.watch_supervisor("P", heartbeat=2, poll=0.01))
    assert len(reports) == 1
    assert reports[0]["project_state"] == "ACTION_REQUIRED"
    assert HandoffRequests(ledger).get(handoff["handoff_id"])["status"] == "OPEN"
