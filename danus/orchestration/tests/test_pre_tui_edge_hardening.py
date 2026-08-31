"""A2-8g pre-TUI edge hardening regressions. No LLM/network."""
from __future__ import annotations

import json
import signal
from pathlib import Path

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.execution import context as C
from danus.execution import layout as L
from danus.execution import loop
from danus.orchestration import cli, planning


def _commit(plan: ResearchPlanGraph, operations) -> None:
    cid = plan.propose_change(
        base_plan_version=plan.snapshot()["plan_version"],
        proposer="researcher",
        reason="edge hardening test",
        operations=operations,
    )
    plan.review_change(cid, reviewer="reviewer", approve=True, rationale="needed")
    plan.commit_change(cid, committer="supervisor")


def _planned_worker(tmp_path: Path):
    root = tmp_path / "proj"
    plan = ResearchPlanGraph.create(root, node_id="root", title="Root")
    _commit(
        plan,
        [
            {
                "op": "add_node",
                "node_id": "leaf",
                "title": "Leaf",
                "summary": "",
                "parent": "root",
                "depends_on": [],
            },
            {
                "op": "add_node",
                "node_id": "other",
                "title": "Other",
                "summary": "",
                "parent": "root",
                "depends_on": [],
            },
        ],
    )
    wl = L.WorkerLayout(root / "workers" / "high")
    wl.dir.mkdir(parents=True)
    wl.logs.mkdir()
    wl.role.write_text("ROLE=researcher\nREASONING_EFFORT=low\n", encoding="utf-8")
    wl.task.write_text("do the assigned scientific task\n", encoding="utf-8")
    ledger = ProgressLedger(root)
    ledger.assign(
        worker="high",
        role="researcher",
        node_id="leaf",
        plan_version=plan.snapshot()["plan_version"],
        task=wl.task.read_text(encoding="utf-8"),
    )
    return root, plan, wl, ledger


def _patch_loop(monkeypatch, *, outcome: str | None = None, rc: int = 0) -> None:
    monkeypatch.setattr(loop.scaffold, "sync_worker_harness", lambda *a, **k: None)
    monkeypatch.setattr(loop.scaffold, "write_codex_config", lambda *a, **k: None)
    monkeypatch.setattr(loop, "kickoff", lambda *a, **k: "prompt")

    def fake_round(wl, role, prompt, log_path, hard_timeout, context_budget=False):
        text = "finished" if outcome is None else f"finished\nDANUS_OUTCOME: {outcome}"
        event = {
            "type": "item.completed",
            "item": {"id": "msg", "type": "agent_message", "text": text},
        }
        log_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        return rc

    monkeypatch.setattr(loop, "run_round", fake_round)
    monkeypatch.setenv("DANUS_ROUND_BEAT", "0")
    monkeypatch.setenv("DANUS_CONTEXT_MAX_TOKENS", "0")
    monkeypatch.setenv("DANUS_CONTEXT_MAX_TOOL_CALLS", "0")
    monkeypatch.setenv("DANUS_CONTEXT_MAX_COMPACTIONS", "0")
    monkeypatch.setenv("DANUS_CONTEXT_MAX_SECONDS", "0")


def _run_loop(wl: L.WorkerLayout) -> int:
    old = signal.getsignal(signal.SIGTERM)
    try:
        return loop.main(str(wl.dir))
    finally:
        signal.signal(signal.SIGTERM, old)


def _events(ledger: ProgressLedger):
    return [json.loads(line) for line in ledger.path.read_text(encoding="utf-8").splitlines()]


def test_terminal_outcome_stale_race_stops_cleanly(tmp_path: Path, monkeypatch):
    _, plan, wl, ledger = _planned_worker(tmp_path)
    _patch_loop(monkeypatch, outcome="COMPLETE")
    original_update = ProgressLedger.update
    revised = False

    def racing_update(self, **kwargs):
        nonlocal revised
        if kwargs.get("worker") == "high" and kwargs.get("state") == "DONE" and not revised:
            revised = True
            _commit(
                plan,
                [{"op": "set_dependencies", "node_id": "leaf", "depends_on": ["other"]}],
            )
        return original_update(self, **kwargs)

    monkeypatch.setattr(ProgressLedger, "update", racing_update)

    assert _run_loop(wl) == 0
    current = ledger.current("high")
    assert current and current["state"] == "STALE"
    status = json.loads(wl.status.read_text(encoding="utf-8"))
    assert status["state"] == "stopped"
    assert status["progress_state"] == "STALE"
    assert status["ignored_terminal_outcome"] == "COMPLETE"
    assert "error" not in status
    exits = [e for e in _events(ledger) if e.get("event") == "RUNTIME" and e.get("kind") == "EXITED"]
    assert exits[-1]["note"] == "assignment_stale_after_round"


def test_consecutive_context_migration_failures_use_existing_failure_fuse(
    tmp_path: Path, monkeypatch
):
    wl = L.WorkerLayout(tmp_path / "proj" / "workers" / "high")
    wl.dir.mkdir(parents=True)
    wl.logs.mkdir()
    wl.role.write_text("ROLE=researcher\nREASONING_EFFORT=low\n", encoding="utf-8")
    wl.task.write_text("continue\n", encoding="utf-8")
    monkeypatch.setattr(loop.scaffold, "sync_worker_harness", lambda *a, **k: None)
    monkeypatch.setattr(loop.scaffold, "write_codex_config", lambda *a, **k: None)
    monkeypatch.setattr(loop, "kickoff", lambda *a, **k: "prompt")
    calls = 0

    def rollover(wl, role, prompt, log_path, hard_timeout, context_budget=False):
        nonlocal calls
        calls += 1
        log_path.write_text("", encoding="utf-8")
        C.write_rollover_request(wl.dir, "tool_calls:1/1", {"tokens": 0})
        return 125

    monkeypatch.setattr(loop, "run_round", rollover)
    monkeypatch.setattr(loop, "_run_context_migration", lambda *a, **k: None)
    monkeypatch.setenv("DANUS_ROUND_BEAT", "0")
    monkeypatch.setenv("DANUS_MAX_CONSEC_FAILURES", "2")

    assert _run_loop(wl) == 1
    assert calls == 2
    status = json.loads(wl.status.read_text(encoding="utf-8"))
    assert status["state"] == "error"
    assert status["error"] == "2 consecutive context migration failures"


def test_runtime_role_drift_routes_assignment_to_review(tmp_path: Path, monkeypatch):
    root, _, wl, ledger = _planned_worker(tmp_path)
    wl.role.write_text("ROLE=reviewer\nREASONING_EFFORT=low\n", encoding="utf-8")
    calls = []
    _patch_loop(monkeypatch, outcome="COMPLETE")
    monkeypatch.setattr(loop, "run_round", lambda *a, **k: calls.append(1) or 0)

    assert _run_loop(wl) == 0
    assert calls == []
    current = ledger.current("high")
    assert current and current["state"] == "BLOCKED"
    assert current["outcome"] == "NEEDS_REVIEW"
    status = json.loads(wl.status.read_text(encoding="utf-8"))
    assert status["assignment_role"] == "researcher"
    assert status["worker_role"] == "reviewer"
    report = cli.reconcile(root, [{"worker": "high", "alive": False, "label": "stopped"}])
    decision = next(d for d in report["decisions"] if d.get("worker") == "high")
    assert decision["action"] == "REVIEW"
    assert decision["reason"] == "assignment_needs_review"


def test_watch_research_refreshes_report_after_planning_action(monkeypatch):
    planning_calls = 0
    supervise_calls = []

    def fake_planning_step(project):
        nonlocal planning_calls
        planning_calls += 1
        if planning_calls == 1:
            return [{"action": "PLAN_ASSIGN", "result": "existing"}]
        if planning_calls == 2:
            return [{"action": "PLAN_COMMIT", "result": "committed", "plan_version": 2}]
        return []

    def fake_watch(*args, **kwargs):
        yield {"project_state": "RUNNING", "plan_version": 1, "decisions": []}

    def fake_supervise(project, **kwargs):
        supervise_calls.append(kwargs)
        return {"project_state": "RUNNING", "plan_version": 2, "decisions": []}

    monkeypatch.setattr(planning, "planning_step", fake_planning_step)
    monkeypatch.setattr(cli, "watch_supervisor", fake_watch)
    monkeypatch.setattr(cli, "do_supervise", fake_supervise)

    report = next(planning.watch_research("P", poll=0.01))
    assert report["plan_version"] == 2
    assert [a["action"] for a in report["planning_applied"]] == ["PLAN_ASSIGN", "PLAN_COMMIT"]
    assert supervise_calls == [{"apply": True, "token_budget": 0, "trigger": "research_planning"}]


def test_assign_crash_window_is_recovered_by_next_supervisor(tmp_path: Path, monkeypatch):
    agents = tmp_path / "agents"
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(agents))
    root = agents / "P"
    plan = ResearchPlanGraph.create(root, node_id="root", title="Root")
    _commit(
        plan,
        [{
            "op": "add_node",
            "node_id": "leaf",
            "title": "Leaf",
            "summary": "",
            "parent": "root",
            "depends_on": [],
        }],
    )
    wl = L.WorkerLayout(root / "workers" / "high")
    wl.dir.mkdir(parents=True)
    wl.role.write_text("ROLE=researcher\nREASONING_EFFORT=low\n", encoding="utf-8")
    wl.task.write_text("old task\n", encoding="utf-8")
    task = "new task\n"
    spec = root / "tasks" / f"v{plan.snapshot()['plan_version']}" / "researcher" / "leaf.md"
    spec.parent.mkdir(parents=True)
    spec.write_text(task, encoding="utf-8")
    original_assign = ProgressLedger.assign

    def crash_assign(self, **kwargs):
        raise KeyboardInterrupt("simulated kill window")

    monkeypatch.setattr(ProgressLedger, "assign", crash_assign)
    with pytest.raises(KeyboardInterrupt):
        cli.do_assign("P/high", task, node_id="leaf")
    assert wl.task.read_text(encoding="utf-8") == task
    assert ProgressLedger(root).current("high") is None

    monkeypatch.setattr(ProgressLedger, "assign", original_assign)
    monkeypatch.setattr(cli, "_start_one", lambda _wl: "started")
    report = cli.do_supervise("P", apply=True)
    dispatched = next(x for x in report["autonomy_applied"] if x.get("node_id") == "leaf")
    assert dispatched["result"] == "assigned"
    current = ProgressLedger(root).current("high")
    assert current and current["task_sha256"] == ProgressLedger.task_sha256(task)


def test_blocked_worker_is_not_silently_recycled(tmp_path: Path, monkeypatch):
    agents = tmp_path / "agents"
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(agents))
    root = agents / "P"
    plan = ResearchPlanGraph.create(root, node_id="root", title="Root")
    _commit(
        plan,
        [
            {"op": "add_node", "node_id": "A", "title": "A", "summary": "", "parent": "root", "depends_on": []},
            {"op": "add_node", "node_id": "B", "title": "B", "summary": "", "parent": "root", "depends_on": []},
        ],
    )
    wl = L.WorkerLayout(root / "workers" / "high")
    wl.dir.mkdir(parents=True)
    wl.role.write_text("ROLE=researcher\nREASONING_EFFORT=low\n", encoding="utf-8")
    task_a = "blocked task\n"
    wl.task.write_text(task_a, encoding="utf-8")
    ledger = ProgressLedger(root)
    ledger.assign(
        worker="high",
        role="researcher",
        node_id="A",
        plan_version=plan.snapshot()["plan_version"],
        task=task_a,
    )
    ledger.update(
        worker="high",
        state="BLOCKED",
        actor="runtime",
        outcome="NEEDS_REVIEW",
    )
    spec = root / "tasks" / f"v{plan.snapshot()['plan_version']}" / "researcher" / "B.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("ready task\n", encoding="utf-8")

    report = cli.do_supervise("P", apply=True)
    waiting = next(x for x in report["autonomy_applied"] if x.get("node_id") == "B")
    assert waiting["result"] == "no_idle_worker"
    current = ledger.current("high")
    assert current and current["node_id"] == "A" and current["state"] == "BLOCKED"
    assert wl.pause.is_file()
