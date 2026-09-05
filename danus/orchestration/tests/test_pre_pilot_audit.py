"""A2-7f pre-pilot concurrency/reproducibility fault audit. No LLM or network."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from danus.core import EvidenceStore, ProgressLedger
from danus.core._util import append_jsonl, read_jsonl
from danus.execution import context as C
from danus.execution import layout as L
from danus.execution import loop
from danus.execution.experiment import run_experiment
from danus.execution.tests.test_experiment_runtime import _fixture as experiment_fixture
from danus.orchestration import cli
from danus.orchestration.control import reconcile
from danus.orchestration.tests.test_recovery_audit import (
    _commit,
    _project,
    _run_loop,
    _runtime_env,
    _write_round,
)

_RUNNER = r'''from pathlib import Path
import json, sys, time
output, data, started = map(Path, sys.argv[1:4])
delay = float(sys.argv[4])
counter = Path("count.txt")
counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))
started.write_text("started")
time.sleep(delay)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps({"score": float(data.read_text())}))
'''

_REDUCER = r'''import json

def reduce_metrics(protocol, artifacts):
    values = [json.loads(path.read_text())["score"] for path in artifacts]
    return {"mean_score": sum(values) / len(values)}
'''


def _protocol(root: Path, *, delay: float = 0.0):
    runner = root / "runner.py"
    data = root / "input.txt"
    reducer = root / "reducer.py"
    runner.write_text(_RUNNER, encoding="utf-8")
    data.write_text("2", encoding="utf-8")
    reducer.write_text(_REDUCER, encoding="utf-8")

    store = EvidenceStore(root)
    reducer_ref = store.artifact_ref(reducer)
    reducer_ref["function"] = "reduce_metrics"
    inputs = [store.artifact_ref(runner), store.artifact_ref(data)]
    protocol_id = store.freeze_protocol(
        question="Does the pinned execution produce score 2?",
        hypothesis="The pinned score is 2.",
        matrix={
            "units": [
                {
                    "id": "u1",
                    "argv": [
                        sys.executable,
                        "runner.py",
                        "raw/u1.json",
                        "input.txt",
                        "started.txt",
                        str(delay),
                    ],
                    "artifacts": ["raw/u1.json"],
                    "inputs": inputs,
                }
            ]
        },
        primary_metrics=["mean_score"],
        decision_rule="support iff mean_score == 2",
        author="reviewer",
        metric_reducer=reducer_ref,
    )
    return store, protocol_id


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def test_concurrent_experiment_runtime_has_one_executor(tmp_path: Path):
    _, protocol_id = _protocol(tmp_path, delay=1.0)
    proc = subprocess.Popen(
        [sys.executable, "-m", "danus.execution.experiment", str(tmp_path), protocol_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(tmp_path / "started.txt")
        with pytest.raises(RuntimeError, match="already running"):
            run_experiment(tmp_path, protocol_id)
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, stderr or stdout
        assert (tmp_path / "count.txt").read_text() == "1"

        # The lock is released by normal exit; a repeated completed invocation is
        # still idempotent and does not execute the unit again.
        result = run_experiment(tmp_path, protocol_id)
        assert result["completed_units"] == 1
        assert (tmp_path / "count.txt").read_text() == "1"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.parametrize("changed", ["runner.py", "input.txt"])
def test_frozen_code_or_input_change_before_execution_is_rejected(tmp_path: Path, changed: str):
    _, protocol_id = _protocol(tmp_path)
    path = tmp_path / changed
    path.write_text(path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="frozen unit input changed: u1"):
        run_experiment(tmp_path, protocol_id)
    assert not (tmp_path / "raw" / "u1.json").exists()
    assert not (tmp_path / "count.txt").exists()


def test_input_change_during_unit_never_reaches_safe_point(tmp_path: Path):
    store, protocol_id = _protocol(tmp_path, delay=1.0)
    proc = subprocess.Popen(
        [sys.executable, "-m", "danus.execution.experiment", str(tmp_path), protocol_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(tmp_path / "started.txt")
        (tmp_path / "input.txt").write_text("9", encoding="utf-8")
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode != 0
        assert "frozen unit input changed: u1" in (stdout + stderr)

        checkpoint = tmp_path / "experiment_runtime" / protocol_id / "checkpoint.json"
        if checkpoint.exists():
            obj = json.loads(checkpoint.read_text(encoding="utf-8"))
            assert "u1" not in obj.get("completed", {})
            assert obj.get("evidence_id") is None
        assert not list(store.records_dir.glob("*.json")) if store.records_dir.exists() else True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_stray_partial_checkpoint_temp_does_not_replace_last_safe_point(tmp_path: Path):
    _, protocol_id = experiment_fixture(tmp_path)
    (tmp_path / "fail_u2").touch()
    with pytest.raises(RuntimeError):
        run_experiment(tmp_path, protocol_id)

    checkpoint = tmp_path / "experiment_runtime" / protocol_id / "checkpoint.json"
    before = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert set(before["completed"]) == {"u1"}
    checkpoint.with_suffix(".json.tmp").write_text('{"broken":', encoding="utf-8")

    (tmp_path / "fail_u2").unlink()
    result = run_experiment(tmp_path, protocol_id)
    assert result["completed_units"] == 2
    assert (tmp_path / "counts" / "u1.txt").read_text() == "1"


def test_two_supervisor_passes_cannot_start_two_worker_processes(tmp_path: Path, monkeypatch):
    ctx, pdir, _, wl, _ = _project(tmp_path)
    spawned = []
    try:
        def real_spawn(_path, lock_fd=None):
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            spawned.append(proc)
            return proc.pid

        monkeypatch.setattr(cli, "spawn_loop", real_spawn)
        with ThreadPoolExecutor(max_workers=2) as pool:
            reports = list(pool.map(lambda _: cli.do_supervise("P", apply=True), range(2)))

        assert len(spawned) == 1
        assert cli._alive(spawned[0].pid)
        assert len(read_jsonl(pdir / "supervisor" / "events.jsonl")) == 2
        assert any(
            applied.get("result") == "started"
            for report in reports
            for applied in report.get("applied", [])
        )
    finally:
        if spawned and cli._alive(spawned[0].pid):
            cli._stop_one(wl, force=True)
        ctx.__exit__(None, None, None)


def test_force_stop_kills_descendants_in_worker_process_group(tmp_path: Path):
    wdir = tmp_path / "P" / "workers" / "high"
    wdir.mkdir(parents=True)
    wl = L.WorkerLayout(wdir)
    child_pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess,sys,time; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"Path({str(child_pid_file)!r}).write_text(str(p.pid)); time.sleep(60)"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        wl.pid.write_text(str(leader.pid), encoding="utf-8")
        _wait_for(child_pid_file)
        child_pid = int(child_pid_file.read_text())
        assert cli._alive(leader.pid) and cli._alive(child_pid)

        assert cli._stop_one(wl, force=True) == "killed"
        leader.wait(timeout=5)
        assert not cli._alive(leader.pid)
        assert not cli._alive(child_pid)
    finally:
        if cli._alive(leader.pid):
            os.killpg(os.getpgid(leader.pid), signal.SIGKILL)


def test_plan_revision_wins_if_committed_before_terminal_state_write(tmp_path: Path, monkeypatch):
    ctx, _, plan, wl, ledger = _project(tmp_path)
    try:
        _runtime_env(monkeypatch)
        monkeypatch.setenv("DANUS_MAX_ROUNDS", "1")

        def fake_round(wl, role, prompt, log_path, hard_timeout, context_budget=False):
            _write_round(log_path, "finished old task\nDANUS_OUTCOME: COMPLETE")
            _commit(
                plan,
                [{"op": "set_dependencies", "node_id": "leaf", "depends_on": ["other"]}],
            )
            return 0

        monkeypatch.setattr(loop, "run_round", fake_round)
        assert _run_loop(wl) == 0
        current = ledger.current("high", refresh_stale=True)
        assert current and current["state"] == "STALE"
        status = json.loads(wl.status.read_text(encoding="utf-8"))
        assert status["ignored_terminal_outcome"] == "COMPLETE"
    finally:
        ctx.__exit__(None, None, None)


def test_plan_revision_after_terminal_does_not_complete_revised_node(tmp_path: Path, monkeypatch):
    ctx, pdir, plan, wl, ledger = _project(tmp_path)
    try:
        _runtime_env(monkeypatch)
        monkeypatch.setenv("DANUS_MAX_ROUNDS", "1")

        def fake_round(wl, role, prompt, log_path, hard_timeout, context_budget=False):
            _write_round(log_path, "finished\nDANUS_OUTCOME: COMPLETE")
            return 0

        monkeypatch.setattr(loop, "run_round", fake_round)
        assert _run_loop(wl) == 0
        assert ledger.current("high")["state"] == "DONE"

        _commit(
            plan,
            [{"op": "set_dependencies", "node_id": "leaf", "depends_on": ["other"]}],
        )
        report = reconcile(pdir, [cli.worker_status(wl)])
        decision = next(d for d in report["decisions"] if d.get("worker") == "high")
        assert decision["action"] == "STOP"
        assert decision["reason"] == "dependencies_not_complete"
        assert "leaf" not in report["completed_nodes"]
    finally:
        ctx.__exit__(None, None, None)


def test_failed_migration_degrades_to_checkpoint_and_next_round_can_finish(tmp_path: Path, monkeypatch):
    ctx, _, _, wl, ledger = _project(tmp_path)
    try:
        _runtime_env(monkeypatch)
        monkeypatch.setenv("DANUS_MAX_ROUNDS", "2")
        calls = {"round": 0, "migration": 0}
        second_prompt = []

        def fake_round(wl, role, prompt, log_path, hard_timeout, context_budget=False):
            calls["round"] += 1
            if calls["round"] == 1:
                _write_round(log_path, "bounded state survives migration failure", tokens=15)
                C.write_rollover_request(wl.dir, "tokens:15/15", C.inspect_log(log_path))
                return 125
            second_prompt.append(prompt)
            _write_round(log_path, "recovered\nDANUS_OUTCOME: COMPLETE", tokens=8)
            return 0

        def failed_migration(*args, **kwargs):
            calls["migration"] += 1
            return None

        monkeypatch.setattr(loop, "run_round", fake_round)
        monkeypatch.setattr(loop, "_run_context_migration", failed_migration)
        assert _run_loop(wl) == 0
        assert calls == {"round": 2, "migration": 1}
        assert "bounded state survives migration failure" in second_prompt[0]
        current = ledger.current("high")
        assert current and current["state"] == "DONE" and current["outcome"] == "COMPLETE"
    finally:
        ctx.__exit__(None, None, None)


def test_jsonl_append_recovers_torn_tail_and_preserves_next_event(tmp_path: Path):
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(b'{"event":"TORN"')
    append_jsonl(path, {"event": "VALID", "n": 1})
    assert read_jsonl(path) == [{"event": "VALID", "n": 1}]


def test_jsonl_concurrent_writers_preserve_every_complete_event(tmp_path: Path):
    path = tmp_path / "events.jsonl"

    def write_batch(worker: int) -> None:
        for n in range(50):
            append_jsonl(path, {"worker": worker, "n": n})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write_batch, range(8)))

    events = read_jsonl(path)
    assert len(events) == 400
    assert {(e["worker"], e["n"]) for e in events} == {
        (worker, n) for worker in range(8) for n in range(50)
    }
