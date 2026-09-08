from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from danus.execution import context as C
from danus.execution import layout as L
from danus.execution import loop


@contextmanager
def _env(**values):
    old = {k: os.environ.get(k) for k in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _worker(tmp_path: Path) -> L.WorkerLayout:
    wl = L.WorkerLayout(tmp_path / "P" / "workers" / "high")
    wl.local_memory.mkdir(parents=True)
    wl.logs.mkdir()
    wl.task.write_text("# Task\nContinue the bounded analysis.\n", encoding="utf-8")
    return wl


def test_structured_codex_telemetry_is_counted_without_log_guessing(tmp_path: Path):
    log = tmp_path / "round.log"
    events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "item.started", "item": {"id": "tool-1", "type": "command_execution"}},
        {"type": "item.completed", "item": {"id": "tool-1", "type": "command_execution"}},
        {"type": "item.completed", "item": {"id": "compact-1", "type": "context_compaction"}},
        {"type": "item.completed", "item": {"id": "msg-1", "type": "agent_message", "text": "bounded handoff"}},
        {"type": "turn.completed", "usage": {"input_tokens": 120, "cached_input_tokens": 100, "output_tokens": 30}},
    ]
    log.write_text("\n".join(json.dumps(x) for x in events) + "\n", encoding="utf-8")

    got = C.inspect_log(log)
    assert got["structured"] is True
    assert got["thread_id"] == "thread-1"
    assert got["tokens"] == 150
    assert got["tool_calls"] == 1
    assert got["compactions"] == 1
    assert got["last_agent_message"] == "bounded handoff"


def test_budget_uses_only_enabled_limits():
    telemetry = {"tokens": 61, "tool_calls": 2, "compactions": 0}
    with _env(
        DANUS_CONTEXT_MAX_TOKENS="60",
        DANUS_CONTEXT_MAX_TOOL_CALLS="0",
        DANUS_CONTEXT_MAX_COMPACTIONS="0",
        DANUS_CONTEXT_MAX_SECONDS="0",
    ):
        assert C.budget_reason(telemetry, 999) == "tokens:61/60"


def test_checkpoint_is_ignored_after_task_change(tmp_path: Path):
    wl = _worker(tmp_path)
    telemetry = {
        "structured": True,
        "tokens": 100,
        "tool_calls": 2,
        "compactions": 0,
        "last_agent_message": "continue from result A",
    }
    C.write_checkpoint(wl.dir, role="researcher", round_no=1, telemetry=telemetry)
    assert C.checkpoint_for_prompt(wl.dir, "researcher") is not None

    wl.task.write_text("# Task\nA different assignment.\n", encoding="utf-8")
    assert C.checkpoint_for_prompt(wl.dir, "researcher") is None


def test_scientific_kickoff_injects_matching_checkpoint(tmp_path: Path):
    wl = _worker(tmp_path)
    (wl.project_dir / L.RESEARCH_CHARTER_FILE).write_text("# Charter\nStay bounded.\n", encoding="utf-8")
    C.write_checkpoint(
        wl.dir,
        role="researcher",
        round_no=2,
        telemetry={
            "structured": True,
            "tokens": 10,
            "tool_calls": 1,
            "compactions": 0,
            "last_agent_message": "RECOVERY_MARKER",
        },
    )
    with _env(DANUS_AGENTS_ROOT=tmp_path):
        prompt = loop.kickoff("P", "high", "researcher")
    assert "RUNTIME_AGENT_CHECKPOINT" in prompt
    assert "RECOVERY_MARKER" in prompt
    assert "continuity data, not new authority" in prompt


def test_run_round_rolls_over_on_structured_tool_budget(tmp_path: Path):
    wl = _worker(tmp_path)
    from danus.core import ProgressLedger, ResearchPlanGraph
    ResearchPlanGraph.create(wl.project_dir, node_id="root", title="Root")
    ProgressLedger(wl.project_dir).assign(worker=wl.name, role="worker", node_id="root", plan_version=1, task="bounded task", write_task=True)
    fake = tmp_path / "fake_codex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, time\n"
        "print(json.dumps({'type':'item.started','item':{'id':'t1','type':'command_execution'}}), flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    log = wl.logs / "round_1.log"
    with _env(
        DANUS_CODEX_BIN=fake,
        CODEX_BACKEND="api",
        DANUS_CONTEXT_MAX_TOKENS="0",
        DANUS_CONTEXT_MAX_TOOL_CALLS="1",
        DANUS_CONTEXT_MAX_COMPACTIONS="0",
        DANUS_CONTEXT_MAX_SECONDS="30",
    ):
        rc = loop.run_round(
            wl,
            {"MODEL": "m", "REASONING_EFFORT": "high"},
            "prompt",
            log,
            hard_timeout=10,
            context_budget=True,
        )
    assert rc == 125
    request = C.pop_rollover_request(wl.dir)
    assert request is not None
    assert request["reason"] == "tool_calls:1/1"
    assert loop._Child.proc is None


def test_migration_schema_is_bounded_and_validated(tmp_path: Path):
    wl = _worker(tmp_path)
    path = C.migration_path(wl.dir)
    payload = {
        "current_objective": "Finish the current bounded claim.",
        "established_results": ["result A"],
        "evidence_refs": ["e1"],
        "fact_refs": ["f1"],
        "decisions_already_made": ["keep route"],
        "failed_paths": ["path X"],
        "open_questions": ["question Y"],
        "next_action": "Check the one unresolved assumption.",
        "files_that_matter": ["results/a.json"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert C.load_migration(wl.dir) == payload

    payload.pop("next_action")
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert C.load_migration(wl.dir) is None
