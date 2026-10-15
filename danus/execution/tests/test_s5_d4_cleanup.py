"""Real process ownership, timeout, and abandoned sandbox-home checks."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from danus import sandbox
from danus.execution.tests.test_s5_d3_frozen_bundle import _unit_pids


def _wait(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate()


def test_caller_death_removes_owned_sandbox_home(tmp_path):
    home = tmp_path / "home"
    profiles = home / ".cache/danus-sandbox"
    profiles.mkdir(parents=True)
    marker = tmp_path / "started"
    unit = "import sys,time; from pathlib import Path; Path(sys.argv[1]).touch(); time.sleep(60)"
    code = (
        "from pathlib import Path; import sys; from danus import sandbox; "
        f"sandbox.run_sandboxed([sys.executable, '-c', {unit!r}, {str(marker)!r}], "
        f"cwd=Path({str(tmp_path)!r}), readable_paths=[Path(sys.prefix),Path(sys.base_prefix)])"
    )
    caller = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, env={**os.environ, "HOME": str(home)})
    try:
        _wait(marker.exists)
        assert _unit_pids(marker)
        assert list(profiles.glob("codex-home-*"))
        caller.kill()
        caller.wait(timeout=5)
        _wait(lambda: not _unit_pids(marker))
        _wait(lambda: not list(profiles.glob("codex-home-*")))
    finally:
        if caller.poll() is None:
            caller.kill()
            caller.wait(timeout=5)
        caller.stderr.close()


@pytest.mark.parametrize("finish", ["normal", "timeout"])
def test_double_fork_and_new_session_do_not_escape_cleanup(tmp_path, finish):
    marker = tmp_path / "descendant"
    child = (
        "import os,signal,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path(sys.argv[1]).touch(); time.sleep(60)"
    )
    unit = (
        "import os,sys,time; from pathlib import Path\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    if os.fork() == 0:\n"
        f"        os.execv(sys.executable, [sys.executable, '-c', {child!r}, {str(marker)!r}])\n"
        "    os._exit(0)\n"
        f"while not Path({str(marker)!r}).exists(): time.sleep(.01)\n"
        + ("time.sleep(60)\n" if finish == "timeout" else "")
    )
    command = [sys.executable, "-c", unit]
    kwargs = dict(cwd=tmp_path, readable_paths=[Path(sys.prefix), Path(sys.base_prefix)])
    if finish == "timeout":
        with pytest.raises(TimeoutError):
            sandbox.run_sandboxed(command, timeout=1, **kwargs)
    else:
        assert sandbox.run_sandboxed(command, timeout=8, **kwargs).returncode == 0
    assert marker.exists()
    _wait(lambda: not _unit_pids(marker))


@pytest.mark.parametrize("kind", ["outside", "symlink", "wrong_name"])
def test_monitor_cleanup_is_limited_to_private_profiles(tmp_path, monkeypatch, kind):
    root = tmp_path / "profiles"
    root.mkdir()
    outside = tmp_path / "codex-home-external"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    if kind == "outside":
        home = outside
    elif kind == "symlink":
        home = root / "codex-home-link"
        home.symlink_to(outside, target_is_directory=True)
    else:
        home = root / "user-config"
        home.mkdir()
        (home / "keep").write_text("keep")
    monkeypatch.setattr(sandbox, "_sandbox_home_root", lambda: root)
    monkeypatch.setenv("CODEX_HOME", str(home))
    sandbox._cleanup_monitor_home()
    assert (outside / "keep").read_text() == "keep"
    assert home.exists()


def test_caller_death_while_monitor_output_is_blocked_kills_descendants(tmp_path):
    home = tmp_path / "home"
    profiles = home / ".cache/danus-sandbox"
    profiles.mkdir(parents=True)
    marker, release = tmp_path / "started", tmp_path / "release"
    unit = (
        "import os,sys,time; from pathlib import Path\n"
        "Path(sys.argv[1]).touch()\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(.01)\n"
        "try:\n"
        "    while True: os.write(1, b'x' * 65536)\n"
        "except BrokenPipeError: time.sleep(30)\n"
    )
    code = (
        "from pathlib import Path; import sys; from danus import sandbox; "
        f"sandbox.run_sandboxed([sys.executable, '-c', {unit!r}, {str(marker)!r}], "
        f"cwd=Path({str(tmp_path)!r}), readable_paths=[Path(sys.prefix),Path(sys.base_prefix)])"
    )
    caller = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env={**os.environ, "HOME": str(home)})
    try:
        _wait(marker.exists)
        children = Path(f"/proc/{caller.pid}/task/{caller.pid}/children").read_text().split()
        assert len(children) == 1
        monitor = children[0]
        caller.send_signal(signal.SIGSTOP)
        release.touch()
        _wait(lambda: "pipe_write" in Path(f"/proc/{monitor}/wchan").read_text())
        caller.kill()
        caller.wait(timeout=5)
        _wait(lambda: not _unit_pids(marker))
        _wait(lambda: not list(profiles.glob("codex-home-*")))
    finally:
        if caller.poll() is None:
            caller.kill()
            caller.wait(timeout=5)
        for pid in _unit_pids(marker):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
