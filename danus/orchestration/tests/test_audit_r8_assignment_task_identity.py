from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.execution import layout as L
from danus.execution import loop as LOOP
from danus.orchestration import cli


def _worker(tmp_path: Path, monkeypatch, task: str = "TASK_A"):
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(tmp_path))
    root = tmp_path / "P"
    ResearchPlanGraph.create(root, node_id="root", title="Root")
    worker = root / "workers" / "researcher"
    worker.mkdir(parents=True)
    (worker / ".role").write_text(
        "ROLE=researcher\nMODEL=test\nREASONING_EFFORT=low\n",
        encoding="utf-8",
    )
    assignment = cli.do_assign("P/researcher", task, node_id="root")
    monkeypatch.setattr(LOOP.scaffold, "sync_worker_harness", lambda *args: None)
    monkeypatch.setattr(LOOP.scaffold, "write_codex_config", lambda *args: None)
    monkeypatch.setattr(LOOP, "kickoff", lambda *args: "prompt")
    monkeypatch.setenv("DANUS_ROUND_BEAT", "0")
    return root, worker, ProgressLedger(root), assignment


def _run(worker: Path) -> int:
    saved = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    try:
        return LOOP.main(str(worker))
    finally:
        signal.signal(signal.SIGTERM, saved[0])
        signal.signal(signal.SIGINT, saved[1])


def _message(log_path: Path, text: str) -> None:
    log_path.write_text(
        json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
        )
        + "\n",
        encoding="utf-8",
    )


def test_assign_crash_cannot_execute_new_task_under_old_assignment(tmp_path: Path, monkeypatch):
    root, worker, ledger, old = _worker(tmp_path, monkeypatch)
    code = (
        "import os; "
        "from danus.core import ProgressLedger; "
        "from danus.orchestration import cli; "
        "from pathlib import Path; original=Path.replace\n"
        "def interrupted(self, target):\n"
        "    result=original(self, target)\n"
        "    if Path(target).name == 'TASK.md': os._exit(73)\n"
        "    return result\n"
        "Path.replace=interrupted\n"
        "cli.do_assign('P/researcher', 'TASK_B', node_id='root')"
    )
    process = subprocess.run([sys.executable, "-c", code], timeout=10)
    assert process.returncode == 73
    assert ledger.current("researcher")["assignment_id"] == old["assignment_id"]
    assert (worker / "TASK.md").read_text(encoding="utf-8") == "TASK_B\n"

    monkeypatch.setenv("DANUS_MAX_ROUNDS", "1")
    calls = []
    def recovered_round(wl, role, prompt, log_path, hard_timeout, **kwargs):
        current = ledger.current("researcher")
        assert current["assignment_id"] != old["assignment_id"]
        assert current["task_sha256"] == ledger.task_sha256(wl.task.read_text())
        calls.append(current["assignment_id"])
        _message(log_path, "TASK_B complete\nDANUS_OUTCOME: COMPLETE")
        return 0
    monkeypatch.setattr(LOOP, "run_round", recovered_round)

    assert _run(worker) == 0
    current = ledger.current("researcher", refresh_stale=False)
    assert current is not None
    assert calls == [current["assignment_id"]]
    assert current["assignment_id"] != old["assignment_id"]
    assert current["state"] == "DONE"
    assert current["outcome"] == "COMPLETE"
    assert len(ledger.assignments()) == 2
    assert next(a for a in ledger.assignments() if a["assignment_id"] == old["assignment_id"])["state"] == "STALE"

    status = json.loads(L.WorkerLayout(worker).status.read_text(encoding="utf-8"))
    assert status["state"] == "stopped"


def test_matching_assignment_task_still_completes_normally(tmp_path: Path, monkeypatch):
    _, worker, ledger, assignment = _worker(tmp_path, monkeypatch)
    monkeypatch.setenv("DANUS_MAX_ROUNDS", "1")

    def run_round(wl, role, prompt, log_path, hard_timeout, **kwargs):
        _message(log_path, "TASK_A complete\nDANUS_OUTCOME: COMPLETE")
        return 0

    monkeypatch.setattr(LOOP, "run_round", run_round)
    assert _run(worker) == 0

    current = ledger.current("researcher", refresh_stale=False)
    assert current is not None
    assert current["assignment_id"] == assignment["assignment_id"]
    assert current["state"] == "DONE"
    assert current["outcome"] == "COMPLETE"


def test_task_drift_between_rounds_blocks_before_second_execution(tmp_path: Path, monkeypatch):
    _, worker, ledger, assignment = _worker(tmp_path, monkeypatch)
    monkeypatch.setenv("DANUS_MAX_ROUNDS", "2")
    calls = []

    def run_round(wl, role, prompt, log_path, hard_timeout, **kwargs):
        calls.append(wl.task.read_text(encoding="utf-8"))
        _message(log_path, "bounded progress")
        wl.task.write_text("TASK_B\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(LOOP, "run_round", run_round)
    assert _run(worker) == 0

    assert calls == ["TASK_A\n"]
    current = ledger.current("researcher", refresh_stale=False)
    assert current is not None
    assert current["assignment_id"] == assignment["assignment_id"]
    assert current["state"] == "BLOCKED"
    assert current["outcome"] == "BLOCKED"
