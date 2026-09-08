"""A2-7e deterministic recovery audit across the A2-7 runtime stack.

These are fault-injection integration tests, not another runtime layer.  They
exercise the existing Agent checkpoint, Experiment checkpoint, Progress events,
Supervisor control, context rollover, and Verification paths together.  No LLM,
network, or real research experiment is used.
"""
from __future__ import annotations

import json
import os
import signal
from contextlib import contextmanager
from pathlib import Path

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.execution import context as C
from danus.execution import layout as L
from danus.execution import loop
from danus.execution.experiment import run_experiment
from danus.execution.tests.test_experiment_runtime import _fixture as experiment_fixture
from danus.orchestration import cli
from danus.verify.empirical import check_empirical_evidence


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


def _commit(plan: ResearchPlanGraph, operations) -> None:
    change_id = plan.propose_change(
        base_plan_version=plan.snapshot()["plan_version"],
        proposer="researcher",
        reason="recovery audit plan change",
        operations=operations,
    )
    plan.review_change(
        change_id,
        reviewer="reviewer",
        approve=True,
        rationale="deterministic recovery audit",
    )
    plan.commit_change(change_id, committer="supervisor")


def _project(tmp_path: Path):
    root = tmp_path / "projects"
    ctx = _agents_root(root)
    ctx.__enter__()
    cli.do_new("P", roles="high:1")
    pdir = root / "P"
    plan = ResearchPlanGraph.create(pdir, node_id="root", title="Root")
    _commit(
        plan,
        [
            {"op": "add_node", "node_id": "leaf", "title": "Leaf", "parent": "root"},
            {"op": "add_node", "node_id": "other", "title": "Other", "parent": "root"},
        ],
    )
    wl = L.WorkerLayout(pdir / "workers" / "high")
    wl.role.write_text("ROLE=researcher\nREASONING_EFFORT=low\n", encoding="utf-8")
    task = "continue the bounded recovery-audit task\n"
    wl.task.write_text(task, encoding="utf-8")
    ledger = ProgressLedger(pdir)
    ledger.assign(
        worker="high",
        role="researcher",
        node_id="leaf",
        plan_version=plan.snapshot()["plan_version"],
        task=task,
    )
    return ctx, pdir, plan, wl, ledger


def _runtime_env(monkeypatch) -> None:
    monkeypatch.setattr(loop.scaffold, "sync_worker_harness", lambda *a, **k: None)
    monkeypatch.setattr(loop.scaffold, "write_codex_config", lambda *a, **k: None)
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


def _write_round(log_path: Path, message: str, tokens: int = 10) -> None:
    events = [
        {
            "type": "turn.completed",
            "usage": {"input_tokens": max(0, tokens - 2), "output_tokens": min(tokens, 2)},
        },
        {
            "type": "item.completed",
            "item": {"id": "message", "type": "agent_message", "text": message},
        },
    ]
    log_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def test_agent_crash_retask_resumes_from_checkpoint_without_replaying_round(tmp_path: Path, monkeypatch):
    ctx, pdir, _, wl, ledger = _project(tmp_path)
    try:
        _runtime_env(monkeypatch)
        monkeypatch.setenv("DANUS_MAX_CONSEC_FAILURES", "1")
        phase = {"value": "crash"}
        resumed_prompts = []

        def fake_round(wl, role, prompt, log_path, hard_timeout, context_budget=False):
            if phase["value"] == "crash":
                _write_round(log_path, "PARTIAL_RECOVERY_STATE", tokens=25)
                return 7
            resumed_prompts.append(prompt)
            _write_round(log_path, "Recovered and finished.\nDANUS_OUTCOME: COMPLETE", tokens=10)
            return 0

        monkeypatch.setattr(loop, "run_round", fake_round)

        assert _run_loop(wl) == 1
        current = ledger.current("high")
        assert current and current["state"] == "RUNNING"
        checkpoint = C.checkpoint_for_prompt(wl.dir, "researcher")
        assert checkpoint and checkpoint["last_agent_message"] == "PARTIAL_RECOVERY_STATE"
        assert (wl.logs / "round_1.log").is_file()

        dry = cli.do_supervise("P")
        decision = next(d for d in dry["decisions"] if d.get("worker") == "high")
        assert decision["action"] == "RETASK" and decision["reason"] == "worker_not_running"

        monkeypatch.setattr(cli, "spawn_loop", lambda path, lock_fd=None: os.getpid())
        applied = cli.do_supervise("P", apply=True)
        retask = next(d for d in applied["applied"] if d.get("worker") == "high")
        assert retask["result"] == "started"

        phase["value"] = "recover"
        assert _run_loop(wl) == 0
        assert len(resumed_prompts) == 1
        assert "RUNTIME_AGENT_CHECKPOINT" in resumed_prompts[0]
        assert "PARTIAL_RECOVERY_STATE" in resumed_prompts[0]
        assert (wl.logs / "round_1.log").is_file()
        assert (wl.logs / "round_2.log").is_file()
        current = ledger.current("high")
        assert current and current["state"] == "DONE" and current["outcome"] == "COMPLETE"
    finally:
        ctx.__exit__(None, None, None)


def test_plan_revision_stales_old_assignment_before_old_task_can_run(tmp_path: Path, monkeypatch):
    ctx, _, plan, wl, ledger = _project(tmp_path)
    try:
        _commit(
            plan,
            [{"op": "set_dependencies", "node_id": "leaf", "depends_on": ["other"]}],
        )
        current = ledger.current("high", refresh_stale=True)
        assert current and current["state"] == "STALE"

        calls = []
        _runtime_env(monkeypatch)
        monkeypatch.setattr(loop, "run_round", lambda *a, **k: calls.append(1) or 0)
        assert _run_loop(wl) == 0
        assert calls == []
    finally:
        ctx.__exit__(None, None, None)


def test_context_rollover_migrates_into_fresh_round_and_completes(tmp_path: Path, monkeypatch):
    ctx, _, _, wl, ledger = _project(tmp_path)
    try:
        _runtime_env(monkeypatch)
        monkeypatch.setenv("DANUS_MAX_ROUNDS", "2")
        calls = {"count": 0}
        second_prompt = []
        migration = {
            "current_objective": "MIGRATION_RECOVERY_MARKER",
            "established_results": ["first session preserved"],
            "evidence_refs": [],
            "fact_refs": [],
            "decisions_already_made": ["continue same bounded task"],
            "failed_paths": [],
            "open_questions": [],
            "next_action": "finish in a fresh session",
            "files_that_matter": ["TASK.md"],
        }

        def fake_round(wl, role, prompt, log_path, hard_timeout, context_budget=False):
            calls["count"] += 1
            if calls["count"] == 1:
                _write_round(log_path, "session reached context boundary", tokens=15)
                C.write_rollover_request(
                    wl.dir,
                    "tokens:15/15",
                    C.inspect_log(log_path),
                )
                return 125
            second_prompt.append(prompt)
            _write_round(log_path, "Fresh session finished.\nDANUS_OUTCOME: COMPLETE", tokens=8)
            return 0

        monkeypatch.setattr(loop, "run_round", fake_round)
        monkeypatch.setattr(loop, "_run_context_migration", lambda *a, **k: migration)

        assert _run_loop(wl) == 0
        assert calls["count"] == 2
        assert len(second_prompt) == 1
        assert "RUNTIME_AGENT_CHECKPOINT" in second_prompt[0]
        assert "MIGRATION_RECOVERY_MARKER" in second_prompt[0]
        assert (wl.logs / "round_1.log").is_file()
        assert (wl.logs / "round_2.log").is_file()
        current = ledger.current("high")
        assert current and current["state"] == "DONE"
    finally:
        ctx.__exit__(None, None, None)


def test_experiment_recovery_is_idempotent_and_verification_recomputes_tampered_metric(tmp_path: Path):
    store, protocol_id = experiment_fixture(tmp_path)
    (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u2/fail_u2").touch()

    with pytest.raises(RuntimeError, match="u2 failed"):
        run_experiment(tmp_path, protocol_id)
    assert (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/counts/u1.txt").read_text() == "1"

    (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u2/fail_u2").unlink()
    result = run_experiment(tmp_path, protocol_id)
    assert result["primary_metrics"] == {"mean_score": 2.0}
    assert (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/counts/u1.txt").read_text() == "1"
    assert (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u2/counts/u2.txt").read_text() == "2"

    again = run_experiment(tmp_path, protocol_id)
    assert again["evidence_id"] == result["evidence_id"]
    assert (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/counts/u1.txt").read_text() == "1"
    assert (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u2/counts/u2.txt").read_text() == "2"

    good = store.get_record(result["evidence_id"])
    assert good
    verified = check_empirical_evidence(
        tmp_path,
        scope={"audit": "recovery"},
        evidence_refs=[result["evidence_id"]],
    )
    assert verified["passed"] is True

    bad_id = store.add_record(
        protocol_id=protocol_id,
        author="experimenter",
        matrix_coverage=good["matrix_coverage"],
        primary_metrics={"mean_score": 999.0},
        failures=good["failures"],
        deviations=good["deviations"],
        artifacts=good["artifacts"],
    )
    rejected = check_empirical_evidence(
        tmp_path,
        scope={"audit": "tampered metric"},
        evidence_refs=[bad_id],
    )
    assert rejected["passed"] is False
    assert any("primary_metrics_recompute" in error for error in rejected["errors"])


def test_supervisor_restart_recovers_from_files_without_resuming_paused_work(tmp_path: Path):
    ctx, pdir, _, wl, _ = _project(tmp_path)
    try:
        wl.pause.write_text("manual\n", encoding="utf-8")

        first = cli.watch_supervisor("P", heartbeat=1800.0, poll=1.0)
        first_report = next(first)
        first.close()
        first_decision = next(d for d in first_report["decisions"] if d.get("worker") == "high")
        assert first_decision["action"] == "CONTINUE"
        assert first_decision["reason"] == "worker_paused"
        assert wl.pause.is_file() and not wl.pid.exists()

        # A brand-new generator represents a Supervisor process restart: no in-memory
        # signature/checkpoint is carried across, only the project files survive.
        second = cli.watch_supervisor("P", heartbeat=1800.0, poll=1.0)
        second_report = next(second)
        second.close()
        second_decision = next(d for d in second_report["decisions"] if d.get("worker") == "high")
        assert second_decision["action"] == "CONTINUE"
        assert second_decision["reason"] == "worker_paused"
        assert wl.pause.is_file() and not wl.pid.exists()

        events = [
            json.loads(line)
            for line in (pdir / "supervisor" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert len(events) == 2
        assert all(event["event"] == "RECONCILE" for event in events)
        assert all(event["trigger"] == "event" for event in events)
    finally:
        ctx.__exit__(None, None, None)


def test_completed_turn_token_usage_is_not_live_killed(tmp_path: Path, monkeypatch):
    ctx, _, _, wl, _ = _project(tmp_path)
    try:
        fake = tmp_path / "fake_codex"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, time\n"
            "print(json.dumps({'type':'item.completed','item':{'id':'m','type':'agent_message','text':'done'}}), flush=True)\n"
            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':20,'output_tokens':5}}), flush=True)\n"
            "time.sleep(1)\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        monkeypatch.setenv("DANUS_CODEX_BIN", str(fake))
        monkeypatch.setenv("CODEX_BACKEND", "api")
        monkeypatch.setenv("DANUS_CONTEXT_MAX_TOKENS", "1")
        monkeypatch.setenv("DANUS_CONTEXT_MAX_TOOL_CALLS", "0")
        monkeypatch.setenv("DANUS_CONTEXT_MAX_COMPACTIONS", "0")
        monkeypatch.setenv("DANUS_CONTEXT_MAX_SECONDS", "0")

        rc = loop.run_round(
            wl,
            {"ROLE": "researcher", "MODEL": "m", "REASONING_EFFORT": "low"},
            "prompt",
            wl.logs / "token_boundary.log",
            hard_timeout=5,
            context_budget=True,
        )
        assert rc == 0
        assert C.pop_rollover_request(wl.dir) is None
    finally:
        ctx.__exit__(None, None, None)


def test_terminal_outcome_wins_over_context_rollover_return_code(tmp_path: Path, monkeypatch):
    ctx, _, _, wl, ledger = _project(tmp_path)
    try:
        _runtime_env(monkeypatch)
        monkeypatch.setenv("DANUS_MAX_ROUNDS", "1")
        migration_calls = []

        def fake_round(wl, role, prompt, log_path, hard_timeout, context_budget=False):
            _write_round(log_path, "Finished at boundary.\nDANUS_OUTCOME: COMPLETE", tokens=50)
            C.write_rollover_request(
                wl.dir,
                "tokens:50/10",
                C.inspect_log(log_path),
            )
            return 125

        monkeypatch.setattr(loop, "run_round", fake_round)
        monkeypatch.setattr(
            loop,
            "_run_context_migration",
            lambda *a, **k: migration_calls.append(1),
        )

        assert _run_loop(wl) == 0
        current = ledger.current("high")
        assert current and current["state"] == "DONE"
        assert current["outcome"] == "COMPLETE"
        assert migration_calls == []
        status = json.loads(wl.status.read_text(encoding="utf-8"))
        assert status["terminal_outcome"] == "COMPLETE"
        assert status["last_rc"] == 125
    finally:
        ctx.__exit__(None, None, None)
