"""A2-7g context/cost telemetry audit. Offline; no model or network calls."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from danus.execution import context as C


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


def test_codex_usage_breakdown_preserves_cached_input(tmp_path: Path):
    log = tmp_path / "round.log"
    events = [
        {"type": "thread.started", "thread_id": "thread-audit"},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 52_000,
                "cached_input_tokens": 49_000,
                "cache_write_input_tokens": 700,
                "output_tokens": 800,
                "reasoning_output_tokens": 500,
            },
        },
    ]
    log.write_text("\n".join(json.dumps(x) for x in events) + "\n", encoding="utf-8")

    got = C.inspect_log(log)
    assert got["tokens"] == 52_800
    assert got["input_tokens"] == 52_000
    assert got["cached_input_tokens"] == 49_000
    assert got["cache_write_input_tokens"] == 700
    assert got["output_tokens"] == 800
    assert got["reasoning_output_tokens"] == 500
    assert got["uncached_tokens"] == 3_800


def test_context_token_guard_uses_uncached_usage_and_preserves_live_race_boundary():
    telemetry = {
        "tokens": 52_800,
        "uncached_tokens": 3_800,
        "tool_calls": 0,
        "compactions": 0,
    }
    with _env(
        DANUS_CONTEXT_MAX_TOKENS="4_000",
        DANUS_CONTEXT_MAX_TOOL_CALLS="0",
        DANUS_CONTEXT_MAX_COMPACTIONS="0",
        DANUS_CONTEXT_MAX_SECONDS="0",
    ):
        assert C.budget_reason(telemetry, 0) is None

    with _env(
        DANUS_CONTEXT_MAX_TOKENS="3_800",
        DANUS_CONTEXT_MAX_TOOL_CALLS="0",
        DANUS_CONTEXT_MAX_COMPACTIONS="0",
        DANUS_CONTEXT_MAX_SECONDS="0",
    ):
        assert C.budget_reason(telemetry, 0) == "tokens:3800/3800"
        live = dict(telemetry)
        live["tokens"] = 0
        assert C.budget_reason(live, 0) is None


def test_checkpoint_keeps_usage_breakdown_without_expanding_schema(tmp_path: Path):
    worker = tmp_path / "P" / "workers" / "high"
    (worker / "local_memory").mkdir(parents=True)
    (worker / "TASK.md").write_text("# Task\nAudit telemetry.\n", encoding="utf-8")
    telemetry = {
        "structured": True,
        "tokens": 52_800,
        "uncached_tokens": 3_800,
        "input_tokens": 52_000,
        "cached_input_tokens": 49_000,
        "cache_write_input_tokens": 700,
        "output_tokens": 800,
        "reasoning_output_tokens": 500,
        "tool_calls": 2,
        "compactions": 0,
        "last_agent_message": "done",
    }

    path = C.write_checkpoint(
        worker,
        role="researcher",
        round_no=1,
        telemetry=telemetry,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))["telemetry"]
    assert saved == {
        "cache_write_input_tokens": 700,
        "cached_input_tokens": 49_000,
        "compactions": 0,
        "input_tokens": 52_000,
        "output_tokens": 800,
        "reasoning_output_tokens": 500,
        "structured": True,
        "tokens": 52_800,
        "tool_calls": 2,
        "uncached_tokens": 3_800,
    }
