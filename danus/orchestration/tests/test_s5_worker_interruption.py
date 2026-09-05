"""S5-A worker interruption recovery: real signals/processes, no LLM/network."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from danus.execution import layout as L
from danus.execution import loop


def _worker(tmp_path: Path, *, role: str = "researcher") -> L.WorkerLayout:
    wl = L.WorkerLayout(tmp_path / "proj" / "workers" / "low")
    wl.dir.mkdir(parents=True)
    wl.logs.mkdir()
    wl.role.write_text(f"ROLE={role}\nREASONING_EFFORT=low\n", encoding="utf-8")
    wl.task.write_text("continue the same assignment\n", encoding="utf-8")
    return wl


def _restore_worker_signals():
    return signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)


def _reset_worker_signals(saved) -> None:
    signal.signal(signal.SIGTERM, saved[0])
    signal.signal(signal.SIGINT, saved[1])


def _pid_running(pid: int) -> bool:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return False
    return bool(fields) and fields[0] != "Z"


def test_sigint_terminates_inflight_child_and_cleans_worker(tmp_path: Path, monkeypatch):
    wl = _worker(tmp_path)
    wl.pid.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(loop.scaffold, "sync_worker_harness", lambda *a, **k: None)
    monkeypatch.setattr(loop.scaffold, "write_codex_config", lambda *a, **k: None)
    monkeypatch.setattr(loop, "kickoff", lambda *a, **k: "prompt")

    class Child:
        terminated = False

        def terminate(self):
            self.terminated = True

    child = Child()

    def interrupted_round(*args, **kwargs):
        loop._Child.proc = child
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(1)
        return 0

    monkeypatch.setattr(loop, "run_round", interrupted_round)
    monkeypatch.setenv("DANUS_ROUND_BEAT", "0")
    saved = _restore_worker_signals()
    try:
        with pytest.raises(SystemExit) as exc:
            loop.main(str(wl.dir))
        assert exc.value.code == 0
    finally:
        loop._Child.proc = None
        _reset_worker_signals(saved)

    assert child.terminated
    assert not wl.pid.exists()
    assert json.loads(wl.status.read_text(encoding="utf-8"))["state"] == "terminated"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux parent-death contract")
def test_sigkill_worker_cannot_leave_live_model_child(tmp_path: Path):
    """Kill the outer worker itself and prove its active model subprocess dies too."""
    wl = _worker(tmp_path)
    child_pid_file = tmp_path / "model.pid"
    fake = tmp_path / "fake_codex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import os, time\n"
        f"open({str(child_pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    round_log = wl.logs / "orphan.log"
    code = (
        "from pathlib import Path\n"
        "from danus.execution import layout as L, loop\n"
        f"wl=L.WorkerLayout(Path({str(wl.dir)!r}))\n"
        f"raise SystemExit(loop.run_round(wl, {{'MODEL':'m','REASONING_EFFORT':'low'}}, "
        f"'prompt', Path({str(round_log)!r}), hard_timeout=60, context_budget=True))\n"
    )
    env = os.environ.copy()
    env["DANUS_CODEX_BIN"] = str(fake)
    env.pop("CODEX_BACKEND", None)
    parent = subprocess.Popen([sys.executable, "-c", code], env=env)
    child_pid = None
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not child_pid_file.is_file():
            if parent.poll() is not None:
                break
            time.sleep(0.02)
        assert child_pid_file.is_file(), "model child never started"
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        assert _pid_running(child_pid)

        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_running(child_pid):
            time.sleep(0.02)
        assert not _pid_running(child_pid), "model child survived outer-worker SIGKILL"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if child_pid is not None and _pid_running(child_pid):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_scientific_hard_timeouts_trip_existing_failure_fuse(tmp_path: Path, monkeypatch):
    wl = _worker(tmp_path)
    monkeypatch.setattr(loop.scaffold, "sync_worker_harness", lambda *a, **k: None)
    monkeypatch.setattr(loop.scaffold, "write_codex_config", lambda *a, **k: None)
    monkeypatch.setattr(loop, "kickoff", lambda *a, **k: "prompt")
    calls = 0

    def timed_out_round(wl, role, prompt, log_path, hard_timeout, context_budget=False):
        nonlocal calls
        calls += 1
        log_path.write_text("", encoding="utf-8")
        return 124

    monkeypatch.setattr(loop, "run_round", timed_out_round)
    monkeypatch.setenv("DANUS_ROUND_BEAT", "0")
    monkeypatch.setenv("DANUS_MAX_ROUNDS", "0")
    monkeypatch.setenv("DANUS_MAX_CONSEC_FAILURES", "2")
    saved = _restore_worker_signals()
    try:
        assert loop.main(str(wl.dir)) == 1
    finally:
        _reset_worker_signals(saved)

    assert calls == 2
    status = json.loads(wl.status.read_text(encoding="utf-8"))
    assert status["state"] == "error"
    assert status["last_rc"] == 124
    assert status["error"] == "2 consecutive failed rounds"
