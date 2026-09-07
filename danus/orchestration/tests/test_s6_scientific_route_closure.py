from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.execution import context as C
from danus.execution import loop as LOOP
from danus.orchestration import cli as CLI
from danus.orchestration import planning
from danus.orchestration import steering as STEER
from danus.orchestration import supervisor_llm as S
from danus.orchestration.control import build_supervisor_snapshot, reduce_supervisor_snapshot


def _luna(route: str):
    return {
        "status": "completed",
        "error": "",
        "tool_calls": 0,
        "reply": json.dumps({
            "route": route,
            "rationale": "The reviewed evidence requires this bounded route.",
            "basis": ["node:root"],
        }),
        "usage": {"input": 10, "output": 5, "reasoning": 0},
    }


def _bind_layout(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(S.L, "project_dir", lambda project: root)
    monkeypatch.setattr(S.L, "worker_dir", lambda project, worker: root / "workers" / worker)


def _ambiguity(root: Path, *, source_role: str = "experimenter", review_result: str = ""):
    ResearchPlanGraph.create(root, node_id="root", title="Root", summary="reviewed ambiguity")
    ledger = ProgressLedger(root)
    source_worker = "researcher" if source_role == "researcher" else "experimenter"
    source_dir = root / "workers" / source_worker
    source_dir.mkdir(parents=True)
    source_task = "investigate the bounded scientific question\n"
    (source_dir / "TASK.md").write_text(source_task, encoding="utf-8")
    source = ledger.assign(
        worker=source_worker,
        role=source_role,
        node_id="root",
        plan_version=1,
        task=source_task,
    )
    ledger.update(
        worker=source_worker,
        state="BLOCKED",
        actor="runtime",
        note="valid evidence needs disposition",
        outcome="NEEDS_REVIEW",
    )
    C.write_checkpoint(
        source_dir,
        role=source_role,
        round_no=1,
        telemetry={"last_agent_message": "SOURCE: metrics conflict\nDANUS_OUTCOME: NEEDS_REVIEW"},
    )

    handoffs = HandoffRequests(ledger)
    review = handoffs.open(
        kind="REVIEW",
        plan_version=1,
        node_id="root",
        subject_type="ASSIGNMENT",
        subject_id=source["assignment_id"],
        requested_by=source_worker,
        source_role=source_role,
        source_assignment_id=source["assignment_id"],
        request="review the source ambiguity",
        reason="independent scientific judgment required",
    )
    reviewer_dir = root / "workers" / "reviewer"
    reviewer_dir.mkdir(parents=True)
    (reviewer_dir / "TASK.md").write_text(review["request"], encoding="utf-8")
    reviewer = ledger.assign(
        worker="reviewer",
        role="reviewer",
        node_id="root",
        plan_version=1,
        task=review["request"],
        handoff_id=review["handoff_id"],
    )
    ledger.update(
        worker="reviewer",
        state="DONE",
        actor="runtime",
        outcome="COMPLETE",
        result=review_result or (
            "REVIEW: evidence valid, conclusion ambiguous\n"
            "DANUS_REVIEW_CLASS: SCIENTIFIC_AMBIGUITY\n"
            "DANUS_OUTCOME: COMPLETE"
        ),
    )
    C.write_checkpoint(
        reviewer_dir,
        role="reviewer",
        round_no=1,
        telemetry={"last_agent_message": "REVIEW: evidence valid, conclusion ambiguous\nDANUS_OUTCOME: COMPLETE"},
    )
    handoffs.resolve(
        review["handoff_id"],
        assignment_id=reviewer["assignment_id"],
        actor="supervisor",
        summary="generic assignment review completed; source disposition remains explicit",
    )
    report = reduce_supervisor_snapshot(build_supervisor_snapshot(root, []))
    action = next((item for item in report["actions"] if item.get("kind") == "SCIENTIFIC_ROUTE_REQUIRED"), None)
    return ledger, source, action


def _resolved_route(root: Path, monkeypatch, *, source_role: str = "experimenter", route: str = "INTERPRET"):
    _bind_layout(monkeypatch, root)
    ledger, source, action = _ambiguity(root, source_role=source_role)
    monkeypatch.setattr(S, "_run_luna", lambda prompt: _luna(route))
    decision = S.resolve_scientific_routes(root, [action])[0]
    handoffs = HandoffRequests(ledger)
    handoff = handoffs.get(decision["handoff_id"])
    assert handoff is not None

    researcher_dir = root / "workers" / "researcher"
    researcher_dir.mkdir(parents=True, exist_ok=True)
    (researcher_dir / "TASK.md").write_text(handoff["request"], encoding="utf-8")
    route_assignment = ledger.assign(
        worker="researcher",
        role="researcher",
        node_id="root",
        plan_version=1,
        task=handoff["request"],
        handoff_id=handoff["handoff_id"],
    )
    ledger.update(worker="researcher", state="DONE", actor="runtime", outcome="COMPLETE")
    C.write_checkpoint(
        researcher_dir,
        role="researcher",
        round_no=1,
        telemetry={"last_agent_message": f"{route} RESULT: bounded scientific conclusion\nDANUS_OUTCOME: COMPLETE"},
    )
    handoffs.resolve(
        handoff["handoff_id"],
        assignment_id=route_assignment["assignment_id"],
        actor="supervisor",
        summary="scientific route completed",
    )
    return ledger, source, route_assignment, handoffs.get(handoff["handoff_id"]), decision


def _allow_disposition(monkeypatch) -> None:
    monkeypatch.setattr(
        CLI,
        "_do_supervise_unlocked",
        lambda *args, **kwargs: {"project_state": "RUNNING", "budget_stop": None},
    )


def test_route_request_contains_source_and_review_results(tmp_path: Path, monkeypatch):
    _bind_layout(monkeypatch, tmp_path)
    ledger, source, action = _ambiguity(tmp_path)
    monkeypatch.setattr(S, "_run_luna", lambda prompt: _luna("INTERPRET"))

    decision = S.resolve_scientific_routes(tmp_path, [action])[0]
    handoff = HandoffRequests(ledger).get(decision["handoff_id"])
    assert handoff is not None
    assert "SOURCE: metrics conflict" in handoff["request"]
    assert "REVIEW: evidence valid, conclusion ambiguous" in handoff["request"]
    assert source["assignment_id"] in handoff["request"]


def test_experimenter_route_resumes_same_assignment_and_injects_result(tmp_path: Path, monkeypatch):
    ledger, source, _, handoff, _ = _resolved_route(tmp_path, monkeypatch)
    starts = []
    monkeypatch.setattr(CLI, "_start_one", lambda wl: starts.append(wl.name) or "started")

    result = S.apply_scientific_route_dispositions("p")
    current = ledger.current("experimenter", refresh_stale=False)
    assert current is not None
    assert current["assignment_id"] == source["assignment_id"]
    assert current["state"] == "RUNNING"
    assert result[0]["result"] == "resumed"
    assert starts == ["experimenter"]

    context = LOOP._resolved_scientific_route_context("p", source["assignment_id"])
    assert handoff is not None
    assert handoff["handoff_id"] in context
    assert "INTERPRET RESULT: bounded scientific conclusion" in context


def test_crash_after_resume_state_uses_existing_recovery(tmp_path: Path, monkeypatch):
    ledger, source, _, handoff, _ = _resolved_route(tmp_path, monkeypatch)
    monkeypatch.setattr(CLI, "_start_one", lambda wl: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        S.apply_scientific_route_dispositions("p")

    current = ledger.current("experimenter", refresh_stale=False)
    assert current is not None
    assert current["assignment_id"] == source["assignment_id"]
    assert current["state"] == "RUNNING"
    assert S._route_consumed(ledger, source["assignment_id"], handoff["handoff_id"])

    report = reduce_supervisor_snapshot(build_supervisor_snapshot(tmp_path, []))
    start = next(item for item in report["actions"] if item.get("kind") == "START_WORKER")
    assert start["assignment_id"] == source["assignment_id"]
    assert start["worker"] == "experimenter"


def test_second_needs_review_waits_for_human_without_second_route(tmp_path: Path, monkeypatch):
    ledger, source, _, _, _ = _resolved_route(tmp_path, monkeypatch)
    monkeypatch.setattr(CLI, "_start_one", lambda wl: "started")
    S.apply_scientific_route_dispositions("p")
    ledger.update(
        worker="experimenter",
        state="BLOCKED",
        actor="runtime",
        note="ambiguity remains after routed interpretation",
        outcome="NEEDS_REVIEW",
    )

    result = S.apply_scientific_route_dispositions("p")
    assert result[0]["result"] == "awaiting_human"
    assert ledger.current("experimenter", refresh_stale=False)["state"] == "BLOCKED"
    assert not any(
        item.get("kind") == "SCIENTIFIC_ROUTE_REQUIRED"
        for item in reduce_supervisor_snapshot(build_supervisor_snapshot(tmp_path, []))["actions"]
    )
    assert source["assignment_id"]


def test_route_plan_proposal_keeps_existing_governance_in_control(tmp_path: Path, monkeypatch):
    ledger, source, route_assignment, _, _ = _resolved_route(tmp_path, monkeypatch, route="RETHINK")
    proposal = tmp_path / ".danus" / "plan_proposal.json"
    proposal.parent.mkdir(parents=True, exist_ok=True)
    proposal.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(CLI, "_start_one", lambda wl: pytest.fail("source must not resume while proposal exists"))

    result = S.apply_scientific_route_dispositions("p")
    assert result[0]["result"] == "plan_proposal"
    assert ledger.current("experimenter", refresh_stale=False)["state"] == "BLOCKED"
    assert route_assignment["assignment_id"]
    assert source["assignment_id"]


def test_researcher_source_route_completes_node_without_new_assignment(tmp_path: Path, monkeypatch):
    ledger, source, route_assignment, _, _ = _resolved_route(
        tmp_path,
        monkeypatch,
        source_role="researcher",
        route="RETHINK",
    )

    result = S.apply_scientific_route_dispositions("p")
    assert result[0]["result"] == "completed"
    assert "root" in ledger.completed_nodes()
    assert source["assignment_id"] != route_assignment["assignment_id"]
    assert next(item for item in ledger.assignments() if item["assignment_id"] == source["assignment_id"])["state"] == "STALE"


def test_new_same_node_assignment_invalidates_completed_researcher_route(tmp_path: Path, monkeypatch):
    ledger, _, route_assignment, _, _ = _resolved_route(
        tmp_path,
        monkeypatch,
        source_role="researcher",
        route="RETHINK",
    )
    _allow_disposition(monkeypatch)
    newer = ledger.assign(
        worker="researcher",
        role="researcher",
        node_id="root",
        plan_version=1,
        task="independent follow-up work\n",
    )

    assert S.apply_scientific_route_dispositions("p") == []
    assert "root" not in ledger.completed_nodes()
    assert ledger.current("researcher", refresh_stale=False)["assignment_id"] == newer["assignment_id"]
    assert newer["assignment_id"] != route_assignment["assignment_id"]


def test_consumed_researcher_route_cannot_reclose_reopened_node(tmp_path: Path, monkeypatch):
    ledger, _, _, _, _ = _resolved_route(
        tmp_path,
        monkeypatch,
        source_role="researcher",
        route="RETHINK",
    )
    _allow_disposition(monkeypatch)

    assert S.apply_scientific_route_dispositions("p")[0]["result"] == "completed"
    ledger.set_node_state("root", state="OPEN", actor="supervisor", note="manual reopen")

    assert S.apply_scientific_route_dispositions("p") == []
    assert ledger.node_states()["root"]["state"] == "OPEN"
    assert "root" not in ledger.completed_nodes()


def test_reopen_after_route_completion_invalidates_unconsumed_route(tmp_path: Path, monkeypatch):
    ledger, _, _, _, _ = _resolved_route(
        tmp_path,
        monkeypatch,
        source_role="researcher",
        route="RETHINK",
    )
    _allow_disposition(monkeypatch)
    ledger.set_node_state("root", state="OPEN", actor="supervisor", note="manual reopen")

    assert S.apply_scientific_route_dispositions("p") == []
    assert ledger.node_states()["root"]["state"] == "OPEN"
    assert "root" not in ledger.completed_nodes()


def test_manual_pause_uses_project_control_lock(tmp_path: Path, monkeypatch):
    worker_dir = tmp_path / "workers" / "researcher"
    worker_dir.mkdir(parents=True)
    monkeypatch.setattr(CLI.L, "resolve_target", lambda target: ("p", "researcher"))
    monkeypatch.setattr(CLI.L, "target_worker_dirs", lambda target: [worker_dir])
    monkeypatch.setattr(CLI.L, "project_dir", lambda project: tmp_path)
    held = False

    @contextmanager
    def lock(project_dir):
        nonlocal held
        assert project_dir == tmp_path
        assert not held
        held = True
        try:
            yield
        finally:
            held = False

    def pause_one(wl, reason="manual"):
        assert held
        return "paused"

    monkeypatch.setattr(CLI, "project_control_lock", lock)
    monkeypatch.setattr(CLI, "_pause_one", pause_one)

    assert CLI.do_pause("p/researcher") == [{"worker": "researcher", "result": "paused"}]
    assert held is False


def test_escalation_requires_guidance_then_explicit_resume(tmp_path: Path, monkeypatch):
    _bind_layout(monkeypatch, tmp_path)
    ledger, source, action = _ambiguity(tmp_path)
    monkeypatch.setattr(S, "_run_luna", lambda prompt: _luna("ESCALATE"))
    decision = S.resolve_scientific_routes(tmp_path, [action])[0]
    monkeypatch.setattr(STEER, "do_status", lambda target: [{"worker": "experimenter", "pause_reason": ""}])
    resumes = []
    monkeypatch.setattr(STEER, "_legacy_resume", lambda target, stagger=0: resumes.append(target) or [{"worker": "experimenter", "result": "resumed"}])

    assert STEER._resume_worker("p", "experimenter")[0]["result"] == "needs-review"
    STEER._write_guidance("p", "Use the conservative interpretation and continue.")
    assert STEER._resume_worker("p", "experimenter")[0]["result"] == "resumed"

    current = ledger.current("experimenter", refresh_stale=False)
    assert current is not None and current["assignment_id"] == source["assignment_id"]
    assert current["state"] == "RUNNING"
    assert HandoffRequests(ledger).get(decision["handoff_id"])["status"] == "CANCELLED"
    assert resumes == ["p/experimenter"]


def test_research_once_uses_same_s6_route_seam(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(planning, "initialize_research", lambda *args, **kwargs: {"project": "p"})
    monkeypatch.setattr(planning, "planning_step", lambda project, *, token_budget=0: [{"action": "PLAN_NOOP"}])
    monkeypatch.setattr(
        CLI,
        "do_supervise",
        lambda *args, **kwargs: {
            "project_state": "RUNNING",
            "convergence": {"stable": True},
            "budget_stop": None,
            "quota_policy": {"block_new_codex_rounds": False},
            "actions": [],
        },
    )
    monkeypatch.setattr(
        S,
        "_route_report",
        lambda project, report, token_budget: calls.append((project, token_budget)) or {**report, "routed": True},
    )

    assert S.research_main(["p", "--once"]) == 0
    assert calls == [("p", 0)]
    rendered = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert rendered[-1]["routed"] is True
    assert rendered[-1]["planning_applied"] == [{"action": "PLAN_NOOP"}]
