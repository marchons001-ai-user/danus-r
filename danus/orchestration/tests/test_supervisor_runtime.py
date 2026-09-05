"""Offline A2-7d Supervisor Runtime tests. No LLM, network, or real experiment."""
from __future__ import annotations

import json
import os
import signal
from contextlib import contextmanager
from pathlib import Path

from danus.core import EvidenceStore, ProgressLedger, ResearchPlanGraph
from danus.execution import layout as L
from danus.execution import loop
from danus.orchestration import cli
from danus.orchestration.control import reconcile


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


def _project(tmp_path: Path, workers: str = "high:1"):
    root = tmp_path / "projects"
    ctx = _agents_root(root)
    ctx.__enter__()
    cli.do_new("P", roles=workers)
    pdir = root / "P"
    plan = ResearchPlanGraph.create(pdir, node_id="root", title="Root")
    return ctx, pdir, plan


def _assign(pdir: Path, worker: str = "high") -> ProgressLedger:
    wl = L.WorkerLayout(pdir / "workers" / worker)
    wl.role.write_text("ROLE=researcher\nREASONING_EFFORT=low\n", encoding="utf-8")
    task = "work on root\n"
    wl.task.write_text(task, encoding="utf-8")
    ledger = ProgressLedger(pdir)
    ledger.assign(
        worker=worker,
        role="researcher",
        node_id="root",
        plan_version=ResearchPlanGraph(pdir).snapshot()["plan_version"],
        task=task,
    )
    return ledger


def test_pause_blocks_start_and_resume_reuses_start(tmp_path: Path, monkeypatch):
    ctx, pdir, _ = _project(tmp_path)
    try:
        wl = L.WorkerLayout(pdir / "workers" / "high")
        assert cli.do_pause("P/high")[0]["result"] == "paused"
        assert wl.pause.read_text(encoding="utf-8").strip() == "manual"
        assert cli.do_start("P/high")[0]["result"] == "paused"

        spawned = []
        monkeypatch.setattr(cli, "spawn_loop", lambda path, lock_fd=None: spawned.append(Path(path)) or 424242)
        resumed = cli.do_resume("P/high")
        assert resumed[0]["result"] == "started"
        assert spawned == [wl.dir]
        assert not wl.pause.exists()
    finally:
        ctx.__exit__(None, None, None)


def test_review_is_applied_as_pause_and_remains_visible(tmp_path: Path):
    ctx, pdir, _ = _project(tmp_path)
    try:
        ledger = _assign(pdir)
        ledger.update(worker="high", state="BLOCKED", actor="runtime", outcome="NEEDS_REVIEW")

        report = cli.do_supervise("P", apply=True)
        review = next(d for d in report["applied"] if d["action"] == "REVIEW")
        assert review["result"] == "paused"
        wl = L.WorkerLayout(pdir / "workers" / "high")
        assert wl.pause.read_text(encoding="utf-8").strip() == "review:assignment_needs_review"

        again = cli.do_supervise("P")
        decision = next(d for d in again["decisions"] if d.get("worker") == "high")
        assert decision["action"] == "REVIEW"
        assert decision["reason"] == "assignment_needs_review"
    finally:
        ctx.__exit__(None, None, None)


def test_dead_assignment_retask_restarts_same_worker(tmp_path: Path, monkeypatch):
    ctx, pdir, _ = _project(tmp_path)
    try:
        _assign(pdir)
        spawned = []
        monkeypatch.setattr(cli, "spawn_loop", lambda path, lock_fd=None: spawned.append(Path(path)) or 434343)

        report = cli.do_supervise("P", apply=True)
        retask = next(d for d in report["applied"] if d.get("worker") == "high")
        assert retask["action"] == "RETASK"
        assert retask["result"] == "started"
        assert spawned == [pdir / "workers" / "high"]
    finally:
        ctx.__exit__(None, None, None)


def test_ready_unassigned_node_never_invents_task(tmp_path: Path):
    ctx, _, _ = _project(tmp_path)
    try:
        report = cli.do_supervise("P", apply=True)
        pending = next(d for d in report["applied"] if d["action"] == "RETASK")
        assert pending["worker"] is None
        assert pending["result"] == "pending_assignment"
    finally:
        ctx.__exit__(None, None, None)


def test_token_and_experiment_budget_snapshot(tmp_path: Path):
    ctx, pdir, _ = _project(tmp_path)
    try:
        _assign(pdir)
        wl = L.WorkerLayout(pdir / "workers" / "high")
        wl.status.write_text(
            json.dumps({"state": "idle", "round": 3, "context_tokens_total": 120}),
            encoding="utf-8",
        )

        store = EvidenceStore(pdir)
        protocol_id = store.freeze_protocol(
            question="q",
            hypothesis="h",
            matrix={"units": [{"id": "u1"}, {"id": "u2"}]},
            primary_metrics=["m"],
            decision_rule="d",
            author="reviewer",
        )
        checkpoint = pdir / "experiment_runtime" / protocol_id / "checkpoint.json"
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_text(
            json.dumps({
                "protocol_id": protocol_id,
                "completed": {"u1": []},
                "failures": [{"unit_id": "u2", "returncode": 7}],
                "evidence_id": None,
            }),
            encoding="utf-8",
        )

        report = cli.do_supervise("P", token_budget=100)
        assert report["budget"]["tokens"] == {"used": 120, "limit": 100, "exceeded": True}
        assert report["budget_stop"] == "resource_token_budget"
        assert all(d["action"] == "STOP" for d in report["decisions"])
        exp = report["budget"]["experiments"][0]
        assert exp["planned_units"] == 2
        assert exp["completed_units"] == 1
        assert exp["failures"] == 1
    finally:
        ctx.__exit__(None, None, None)


def test_deadline_budget_stops_before_token_reason(tmp_path: Path):
    ctx, pdir, _ = _project(tmp_path)
    try:
        _assign(pdir)
        (pdir / L.DEADLINE_FILE).write_text("1\n", encoding="utf-8")
        report = cli.do_supervise("P", token_budget=1)
        assert report["budget"]["time"]["exceeded"] is True
        assert report["budget_stop"] == "resource_time_budget"
    finally:
        ctx.__exit__(None, None, None)


def test_manual_pause_is_not_retasked(tmp_path: Path):
    ctx, pdir, _ = _project(tmp_path)
    try:
        _assign(pdir)
        wl = L.WorkerLayout(pdir / "workers" / "high")
        wl.pause.write_text("manual\n", encoding="utf-8")
        row = cli.worker_status(wl)
        report = reconcile(pdir, [row])
        decision = next(d for d in report["decisions"] if d.get("worker") == "high")
        assert decision["action"] == "CONTINUE"
        assert decision["reason"] == "worker_paused"
    finally:
        ctx.__exit__(None, None, None)


def test_control_signature_ignores_age_only_changes(tmp_path: Path):
    ctx, pdir, _ = _project(tmp_path)
    try:
        _assign(pdir)
        rows = cli._project_rows("P")
        budget = cli._resource_snapshot(pdir, rows, 0)
        first = cli._control_signature("P", rows, budget)
        rows[0]["age_s"] = 999999.0
        second = cli._control_signature("P", rows, budget)
        assert first == second
    finally:
        ctx.__exit__(None, None, None)


def test_control_signature_changes_when_deadline_crosses(tmp_path: Path, monkeypatch):
    ctx, pdir, _ = _project(tmp_path)
    try:
        _assign(pdir)
        (pdir / L.DEADLINE_FILE).write_text("100\n", encoding="utf-8")
        rows = cli._project_rows("P")

        monkeypatch.setattr(cli.time, "time", lambda: 99.0)
        before = cli._resource_snapshot(pdir, rows, 0)
        sig_before = cli._control_signature("P", rows, before)
        assert before["time"]["exceeded"] is False

        monkeypatch.setattr(cli.time, "time", lambda: 101.0)
        after = cli._resource_snapshot(pdir, rows, 0)
        sig_after = cli._control_signature("P", rows, after)
        assert after["time"]["exceeded"] is True
        assert sig_after != sig_before
    finally:
        ctx.__exit__(None, None, None)


def test_watch_reconciles_on_change_then_quota_refresh(tmp_path: Path, monkeypatch):
    ctx, pdir, _ = _project(tmp_path)
    try:
        times = iter([0.0, 301.0])
        monkeypatch.setattr(cli.time, "monotonic", lambda: next(times))
        monkeypatch.setattr(cli.time, "sleep", lambda _: None)
        refreshes = []
        monkeypatch.setattr(cli.Q, "refresh_quota", lambda path: refreshes.append(Path(path)) or {})

        gen = cli.watch_supervisor("P", heartbeat=1800.0, poll=1.0)
        next(gen)
        next(gen)
        events = [
            json.loads(line)
            for line in (pdir / "supervisor" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [event["trigger"] for event in events[-2:]] == ["event", "quota"]
        assert refreshes == [pdir, pdir]
    finally:
        ctx.__exit__(None, None, None)


def test_worker_restart_keeps_round_logs_and_accumulates_tokens(tmp_path: Path, monkeypatch):
    wdir = tmp_path / "project" / "workers" / "high"
    wdir.mkdir(parents=True)
    wl = L.WorkerLayout(wdir)
    wl.role.write_text("ROLE=researcher\nREASONING_EFFORT=low\n", encoding="utf-8")
    wl.task.write_text("task\n", encoding="utf-8")

    monkeypatch.setattr(loop.scaffold, "sync_worker_harness", lambda *a, **k: None)
    monkeypatch.setattr(loop.scaffold, "write_codex_config", lambda *a, **k: None)
    monkeypatch.setattr(loop, "kickoff", lambda *a, **k: "prompt")
    monkeypatch.setenv("DANUS_ROUND_BEAT", "0")
    monkeypatch.setenv("DANUS_MAX_ROUNDS", "1")
    monkeypatch.setenv("DANUS_CONTEXT_MAX_TOKENS", "0")
    monkeypatch.setenv("DANUS_CONTEXT_MAX_TOOL_CALLS", "0")
    monkeypatch.setenv("DANUS_CONTEXT_MAX_COMPACTIONS", "0")
    monkeypatch.setenv("DANUS_CONTEXT_MAX_SECONDS", "0")

    def fake_round(wl, role, prompt, log_path, hard_timeout, context_budget=False):
        events = [
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
            {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "continue"}},
        ]
        log_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(loop, "run_round", fake_round)

    old = signal.getsignal(signal.SIGTERM)
    try:
        assert loop.main(str(wdir)) == 0
        first = json.loads(wl.status.read_text(encoding="utf-8"))
        assert first["state"] == "max_rounds"
        assert first["round"] == 1
        assert first["context_tokens_total"] == 15
        assert (wl.logs / "round_1.log").is_file()

        assert loop.main(str(wdir)) == 0
        second = json.loads(wl.status.read_text(encoding="utf-8"))
        assert second["state"] == "max_rounds"
        assert second["round"] == 2
        assert second["context_tokens_total"] == 30
        assert (wl.logs / "round_1.log").is_file()
        assert (wl.logs / "round_2.log").is_file()
    finally:
        signal.signal(signal.SIGTERM, old)
