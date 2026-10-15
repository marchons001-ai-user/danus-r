"""Finite resource probes through the official sandbox; no model calls."""
import os
from pathlib import Path
import sys

import pytest

from danus import sandbox


@pytest.mark.parametrize("stream", [1, 2, 0])
def test_sandbox_output_is_bounded(tmp_path, stream):
    code = f"import os; os.write({stream}, b'x' * (9 * 1024 * 1024))"
    if stream == 0:
        code = "import os; os.write(1, b'x' * (5 * 1024 * 1024)); os.write(2, b'y' * (4 * 1024 * 1024))"
    with pytest.raises(sandbox.SandboxInterrupted, match="output limit"):
        sandbox.run_sandboxed(
            [sys.executable, "-c", code],
            cwd=tmp_path, readable_paths=[Path(sys.prefix), Path(sys.base_prefix)], timeout=10,
        )


def test_launches_and_start_failure_release_fds_and_profiles(tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(sandbox, "_sandbox_home_root", lambda: profiles)
    before = set(Path("/proc/self/fd").iterdir())
    for _ in range(3):
        result = sandbox.run_sandboxed(["/bin/true"], cwd=tmp_path, timeout=10)
        assert result.returncode == 0
        assert not list(profiles.iterdir())
    def fail(*args, **kwargs):
        raise OSError("spawn unavailable")
    monkeypatch.setattr(sandbox.subprocess, "Popen", fail)
    with pytest.raises(OSError, match="spawn unavailable"):
        sandbox.run_sandboxed(["/bin/true"], cwd=tmp_path, timeout=10)
    assert not list(profiles.iterdir())
    assert set(Path("/proc/self/fd").iterdir()) == before


def test_output_limit_does_not_allow_experiment_replay(tmp_path):
    from danus.execution.tests.test_s5_d3_frozen_bundle import _fixture
    from danus.execution.experiment import run_experiment
    root, _, store, pid = _fixture(tmp_path, 'os.write(1, b"x" * (9 * 1024 * 1024))')
    with pytest.raises(sandbox.SandboxInterrupted, match="output limit"):
        run_experiment(root, pid)
    count = root / "experiment_runtime" / pid / "artifacts/u1/raw/count.txt"
    assert count.read_text() == "1"
    with pytest.raises(RuntimeError, match="interrupted before safe point"):
        run_experiment(root, pid)
    assert count.read_text() == "1"
    assert not list(store.records_dir.glob("*.json"))
