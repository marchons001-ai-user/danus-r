"""Offline A2-7c runtime -> Progress Ledger integration tests. No LLM/network."""
from __future__ import annotations

import json
import signal
from pathlib import Path

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.execution import layout as L
from danus.execution import loop
from danus.orchestration.control import reconcile


def _commit(plan: ResearchPlanGraph, operations) -> None:
    cid = plan.propose_change(
        base_plan_version=plan.snapshot()["plan_version"],
        proposer="researcher",
        reason="test plan change",
        operations=operations,
    )
    plan.review_change(cid, reviewer="reviewer", approve=True, rationale="needed")
    plan.commit_change(cid, committer="supervisor")


def _fixture(tmp_path: Path):
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


def _patch_runtime(monkeypatch, text: str, rc: int = 0):
    monkeypatch.setattr(loop.scaffold, "sync_worker_harness", lambda *a, **k: None)
    monkeypatch.setattr(loop.scaffold, "write_codex_config", lambda *a, **k: None)
    monkeypatch.setattr(loop, "kickoff", lambda *a, **k: "test prompt")

    def fake_round(wl, role, prompt, log_path, hard_timeout, context_budget=False):
        event = {
            "type": "item.completed",
            "item": {"id": "msg-1", "type": "agent_message", "text": text},
        }
        log_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        return rc

    monkeypatch.setattr(loop, "run_round", fake_round)
    monkeypatch.setenv("DANUS_ROUND_BEAT", "0")
    monkeypatch.setenv("DANUS_CONTEXT_MAX_TOKENS", "0")
    monkeypatch.setenv("DANUS_CONTEXT_MAX_TOOL_CALLS", "0")
    monkeypatch.setenv("DANUS_CONTEXT_MAX_COMPACTIONS", "0")
    monkeypatch.setenv("DANUS_CONTEXT_MAX_SECONDS", "0")


def _run(wl: L.WorkerLayout) -> int:
    old = signal.getsignal(signal.SIGTERM)
    try:
        return loop.main(str(wl.dir))
    finally:
        signal.signal(signal.SIGTERM, old)


def _events(ledger: ProgressLedger):
    return [json.loads(line) for line in ledger.path.read_text(encoding="utf-8").splitlines()]


def test_complete_marks_assignment_done_but_not_plan_node(tmp_path: Path, monkeypatch):
    _, _, wl, ledger = _fixture(tmp_path)
    _patch_runtime(monkeypatch, "Finished.\nDANUS_OUTCOME: COMPLETE")

    assert _run(wl) == 0
    current = ledger.current("high")
    assert current and current["state"] == "DONE" and current["outcome"] == "COMPLETE"
    assert ledger.completed_nodes() == []  # semantic node closure remains Supervisor-owned

    events = _events(ledger)
    assert any(e.get("event") == "RUNTIME" and e.get("kind") == "STARTED" for e in events)
    assert any(
        e.get("event") == "RUNTIME"
        and e.get("kind") == "EXITED"
        and e.get("details", {}).get("outcome") == "COMPLETE"
        for e in events
    )


def test_blocked_maps_to_blocked_without_becoming_done(tmp_path: Path, monkeypatch):
    root, _, wl, ledger = _fixture(tmp_path)
    _patch_runtime(monkeypatch, "Missing external input.\nDANUS_OUTCOME: BLOCKED")

    assert _run(wl) == 0
    current = ledger.current("high")
    assert current and current["state"] == "BLOCKED" and current["outcome"] == "BLOCKED"
    assert ledger.completed_nodes() == []

    out = reconcile(root, [{"worker": "high", "alive": False, "label": "stopped"}])
    decision = next(d for d in out["decisions"] if d.get("worker") == "high")
    assert decision["action"] == "REVIEW" and decision["reason"] == "assignment_blocked"


def test_needs_review_maps_to_blocked_and_supervisor_review(tmp_path: Path, monkeypatch):
    root, _, wl, ledger = _fixture(tmp_path)
    _patch_runtime(monkeypatch, "Evidence conflicts.\nDANUS_OUTCOME: NEEDS_REVIEW")

    assert _run(wl) == 0
    current = ledger.current("high")
    assert current and current["state"] == "BLOCKED"
    assert current["outcome"] == "NEEDS_REVIEW"

    out = reconcile(root, [{"worker": "high", "alive": False, "label": "stopped"}])
    decision = next(d for d in out["decisions"] if d.get("worker") == "high")
    assert decision["action"] == "REVIEW"
    assert decision["reason"] == "assignment_needs_review"


def test_operational_failure_records_exit_but_remains_retaskable(tmp_path: Path, monkeypatch):
    root, _, wl, ledger = _fixture(tmp_path)
    _patch_runtime(monkeypatch, "backend failed", rc=7)
    monkeypatch.setenv("DANUS_MAX_CONSEC_FAILURES", "1")

    assert _run(wl) == 1
    current = ledger.current("high")
    assert current and current["state"] == "RUNNING" and current.get("outcome") is None

    exits = [
        e for e in _events(ledger)
        if e.get("event") == "RUNTIME" and e.get("kind") == "EXITED"
    ]
    assert exits[-1]["note"] == "consecutive_failed_rounds"
    assert exits[-1]["details"]["returncode"] == 7

    out = reconcile(root, [{"worker": "high", "alive": False, "label": "error"}])
    decision = next(d for d in out["decisions"] if d.get("worker") == "high")
    assert decision["action"] == "RETASK" and decision["reason"] == "worker_not_running"


def test_stale_assignment_exits_before_any_round(tmp_path: Path, monkeypatch):
    _, plan, wl, ledger = _fixture(tmp_path)
    _commit(
        plan,
        [{"op": "set_dependencies", "node_id": "leaf", "depends_on": ["other"]}],
    )

    calls = []
    _patch_runtime(monkeypatch, "should never run")
    monkeypatch.setattr(loop, "run_round", lambda *a, **k: calls.append(1) or 0)

    assert _run(wl) == 0
    assert calls == []
    current = ledger.current("high")
    assert current and current["state"] == "STALE"
    assert any(
        e.get("event") == "RUNTIME"
        and e.get("kind") == "EXITED"
        and e.get("note") == "assignment_stale"
        for e in _events(ledger)
    )


def test_missing_or_ambiguous_marker_never_guesses_terminal_state(tmp_path: Path, monkeypatch):
    _, _, wl, ledger = _fixture(tmp_path)
    _patch_runtime(monkeypatch, "The task appears complete, but no machine marker.")
    monkeypatch.setenv("DANUS_MAX_ROUNDS", "1")

    assert _run(wl) == 0
    current = ledger.current("high")
    assert current and current["state"] == "RUNNING" and current.get("outcome") is None

    assert loop._terminal_outcome({"last_agent_message": "DANUS_OUTCOME: COMPLETE\nDANUS_OUTCOME: BLOCKED"}) is None
