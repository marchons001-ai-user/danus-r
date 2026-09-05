from __future__ import annotations

import json
import signal

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.execution import context as C
from danus.execution import loop as LOOP
from danus.orchestration import supervisor_llm as S
from danus.orchestration.tests.test_s6_scientific_route_closure import (
    _ambiguity,
    _bind_layout,
    _luna,
)


def _worker(root, role: str):
    wd = root / "workers" / role
    wd.mkdir(parents=True, exist_ok=True)
    (wd / ".role").write_text(
        f"ROLE={role}\nMODEL=test\nREASONING_EFFORT=low\n",
        encoding="utf-8",
    )
    return wd


def test_expected_assignment_update_rejects_rebound_worker(tmp_path):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    first = ledger.assign(
        worker="experimenter",
        role="experimenter",
        node_id="root",
        plan_version=1,
        task="experiment A",
    )
    second = ledger.assign(
        worker="experimenter",
        role="experimenter",
        node_id="root",
        plan_version=1,
        task="experiment B",
    )

    with pytest.raises(ValueError, match="assignment changed"):
        ledger.update(
            worker="experimenter",
            state="DONE",
            actor="runtime",
            outcome="COMPLETE",
            expected_assignment_id=first["assignment_id"],
            result="A finished\nDANUS_OUTCOME: COMPLETE",
        )

    current = ledger.current("experimenter", refresh_stale=False)
    assert current is not None
    assert current["assignment_id"] == second["assignment_id"]
    assert current["state"] == "ASSIGNED"


def test_same_task_new_assignment_rejects_old_checkpoint(tmp_path):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    wd = _worker(tmp_path, "researcher")
    task = "Repeat a bounded measurement\n"
    (wd / "TASK.md").write_text(task, encoding="utf-8")

    old = ledger.assign(
        worker="researcher",
        role="researcher",
        node_id="root",
        plan_version=1,
        task=task,
    )
    C.write_checkpoint(
        wd,
        role="researcher",
        round_no=1,
        telemetry={"last_agent_message": "old conclusion"},
        assignment=old,
    )
    new = ledger.assign(
        worker="researcher",
        role="researcher",
        node_id="root",
        plan_version=1,
        task=task,
    )

    assert new["assignment_id"] != old["assignment_id"]
    assert C.checkpoint_for_prompt(wd, "researcher") is None


def test_same_assignment_checkpoint_still_recovers(tmp_path):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    wd = _worker(tmp_path, "researcher")
    task = "Continue the same bounded measurement\n"
    (wd / "TASK.md").write_text(task, encoding="utf-8")
    assignment = ledger.assign(
        worker="researcher",
        role="researcher",
        node_id="root",
        plan_version=1,
        task=task,
    )
    C.write_checkpoint(
        wd,
        role="researcher",
        round_no=3,
        telemetry={"last_agent_message": "same-assignment progress"},
        assignment=assignment,
    )

    checkpoint = C.checkpoint_for_prompt(wd, "researcher")
    assert checkpoint is not None
    assert checkpoint["assignment_id"] == assignment["assignment_id"]
    assert checkpoint["last_agent_message"] == "same-assignment progress"


def test_rebound_round_cannot_complete_new_assignment(tmp_path, monkeypatch):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    wd = _worker(tmp_path, "experimenter")
    task_a = "Execute experiment A\n"
    task_b = "Execute unrelated experiment B\n"
    (wd / "TASK.md").write_text(task_a, encoding="utf-8")
    first = ledger.assign(
        worker="experimenter",
        role="experimenter",
        node_id="root",
        plan_version=1,
        task=task_a,
    )
    rebound = []

    def external_round(wl, role, prompt, log_path, hard_timeout, context_budget=False):
        second = ledger.assign(
            worker="experimenter",
            role="experimenter",
            node_id="root",
            plan_version=1,
            task=task_b,
        )
        rebound.append(second)
        (wd / "TASK.md").write_text(task_b, encoding="utf-8")
        event = {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Experiment A finished\nDANUS_OUTCOME: COMPLETE",
            },
        }
        log_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(LOOP, "run_round", external_round)
    monkeypatch.setattr(LOOP.scaffold, "sync_worker_harness", lambda *args, **kwargs: None)
    monkeypatch.setattr(LOOP.scaffold, "write_codex_config", lambda *args, **kwargs: None)
    monkeypatch.setenv("DANUS_MAX_ROUNDS", "1")
    monkeypatch.setenv("DANUS_ROUND_BEAT", "0")
    saved = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    try:
        assert LOOP.main(str(wd)) == 0
    finally:
        signal.signal(signal.SIGTERM, saved[0])
        signal.signal(signal.SIGINT, saved[1])

    current = ledger.current("experimenter", refresh_stale=False)
    assert current is not None
    assert current["assignment_id"] == rebound[0]["assignment_id"]
    assert current["state"] != "DONE"
    raw = json.loads(C._path(wd, C.CHECKPOINT_NAME).read_text(encoding="utf-8"))
    assert raw["assignment_id"] == first["assignment_id"]
    assert "Experiment A finished" in raw["last_agent_message"]
    assert C.checkpoint_for_prompt(wd, "experimenter") is None


def test_reused_reviewer_keeps_completed_result_for_route(tmp_path, monkeypatch):
    _bind_layout(monkeypatch, tmp_path)
    ledger, _, action = _ambiguity(tmp_path)
    reviewer = next(
        item
        for item in ledger.assignments()
        if str(item.get("role") or "").lower() == "reviewer"
        and item.get("state") == "DONE"
    )
    immutable = "REVIEW A: evidence valid, conclusion ambiguous\nDANUS_OUTCOME: COMPLETE"
    ledger.update(
        worker="reviewer",
        state="DONE",
        actor="runtime",
        outcome="COMPLETE",
        expected_assignment_id=reviewer["assignment_id"],
        result=immutable,
    )

    reviewer_dir = tmp_path / "workers" / "reviewer"
    next_task = "Review the next independent assignment\n"
    (reviewer_dir / "TASK.md").write_text(next_task, encoding="utf-8")
    next_assignment = ledger.assign(
        worker="reviewer",
        role="reviewer",
        node_id="root",
        plan_version=1,
        task=next_task,
    )
    C.write_checkpoint(
        reviewer_dir,
        role="reviewer",
        round_no=2,
        telemetry={"last_agent_message": "REVIEW B: unrelated"},
        assignment=next_assignment,
    )

    monkeypatch.setattr(S, "_run_luna", lambda prompt: _luna("INTERPRET"))
    decision = S.resolve_scientific_routes(tmp_path, [action])[0]
    assert decision["status"] == "valid"
    handoff = HandoffRequests(ledger).get(decision["handoff_id"])
    assert handoff is not None
    assert "REVIEW A: evidence valid, conclusion ambiguous" in handoff["request"]
    assert "REVIEW B: unrelated" not in handoff["request"]
