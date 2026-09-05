from __future__ import annotations

import os
import signal
import subprocess
import sys

from danus.execution import layout as L
from danus.orchestration import cli
from danus.orchestration.tests.test_orchestration import _fake_codex, _project_env, _wait_until


def _terminate(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    if _wait_until(lambda: not cli._alive(pid), timeout=8):
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    assert _wait_until(lambda: not cli._alive(pid), timeout=5)


def test_controller_crash_before_parent_pid_write_keeps_single_worker(tmp_path):
    fake_codex = _fake_codex(tmp_path)
    with _project_env(
        tmp_path,
        DANUS_CODEX_BIN=str(fake_codex),
        DANUS_ROUND_BEAT="0",
        DANUS_MAX_ROUNDS="0",
        FAKE_CODEX_SLEEP="30",
    ):
        cli.do_new("P", roles="high:1")
        wl = L.WorkerLayout(L.worker_dir("P", "high"))
        controller = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os\n"
                    "from danus.execution import layout as L\n"
                    "from danus.orchestration import cli\n"
                    "wl=L.WorkerLayout(L.worker_dir('P','high'))\n"
                    "cli._write_pid=lambda *args, **kwargs: os._exit(23)\n"
                    "cli._start_one(wl)\n"
                ),
            ],
            env=os.environ.copy(),
            timeout=10,
        )
        assert controller.returncode == 23

        assert _wait_until(lambda: wl.pid.is_file() and cli._worker_alive(wl), timeout=10)
        first_pid = cli._read_pid(wl)
        assert first_pid is not None

        # Simulate the exact F03 blind spot even after worker self-registration:
        # liveness metadata is absent, but the real worker still owns wl.lock.
        wl.pid.unlink()
        assert cli._start_one(wl) == "locked"
        assert cli._alive(first_pid)

        _terminate(first_pid)
        assert cli._start_one(wl) == "started"
        assert _wait_until(lambda: cli._worker_alive(wl), timeout=10)
        second_pid = cli._read_pid(wl)
        assert second_pid is not None
        cli._stop_one(wl, force=True)
        assert _wait_until(lambda: not cli._alive(second_pid), timeout=8)
