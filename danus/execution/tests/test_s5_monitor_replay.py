"""Experiment checkpoint transitions with a deterministic host-monitor double.

These tests exercise real status/checkpoint/artifact writes, without Codex or
claims about OS isolation and process cleanup.
"""
import json
import os
from pathlib import Path
import sys
from collections import deque

import pytest

from danus import sandbox
from danus.core import EvidenceStore, runtime
from danus.execution import experiment


@pytest.fixture
def execution(tmp_path, monkeypatch):
    root = tmp_path / "control"
    root.mkdir()
    workspace = tmp_path / "snapshot"
    workspace.mkdir()
    store = EvidenceStore(root)
    protocol_id = store.freeze_protocol(
        question="Checkpoint transitions", hypothesis="No ambiguous replay",
        matrix={"units": [
            {"id": name, "argv": [sys.executable, "runner.py", "raw/result.json"],
             "artifacts": ["raw/result.json"]}
            for name in ("u1", "u2")
        ]},
        primary_metrics=["score"], decision_rule="score == 1", author="test",
    )
    protocol = store.get_protocol(protocol_id)
    manifest = {"files": [{"path": "runner.py"}]}
    runtime_dir = root / "experiment_runtime" / protocol_id
    outcomes = deque()
    calls = []
    engine = tmp_path / "codex"
    engine.touch()
    profiles = tmp_path / "profiles"
    profiles.mkdir()

    class Monitor:
        pid = 12345

        def __init__(self, argv, **kwargs):
            output = Path(argv[-1])
            output.parent.mkdir(parents=True, exist_ok=True)
            unit = output.parent.parent.name
            calls.append(unit)
            counter = output.parent / "count.txt"
            counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))
            monitor_rc, unit_rc = outcomes.popleft()
            if unit_rc == 0:
                output.write_text('{"score": 1}')
            # The completion fd is separate from command stdout/stderr.
            has_completion_fd = len(kwargs["pass_fds"]) == 3
            if has_completion_fd and unit_rc is not None:
                os.write(int(argv[4]), str(unit_rc).encode("ascii"))
            self.returncode = monitor_rc
            if not has_completion_fd and monitor_rc == 0 and unit_rc is not None:
                self.returncode = unit_rc

        def communicate(self, timeout=None):
            return "", ""

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(sandbox, "_resolved_codex", lambda: engine)
    monkeypatch.setattr(sandbox, "_sandbox_home_root", lambda: profiles)
    monkeypatch.setattr(sandbox.os, "pidfd_open", lambda pid: os.open(os.devnull, os.O_RDONLY), raising=False)
    monkeypatch.setattr(sandbox.subprocess, "Popen", Monitor)
    monkeypatch.setattr(sandbox, "_send_tree_signal", lambda *args: None)
    monkeypatch.setattr(runtime, "validate", lambda protocol: None)
    monkeypatch.setattr(experiment, "reduce_metrics", lambda *args: {"score": 1.0})

    def run():
        return experiment._run_frozen(
            root, protocol_id, protocol, store, workspace, manifest, "test", 0, None,
        )

    return run, runtime_dir, outcomes, calls, store


def test_ordinary_unit_failure_retries_without_repeating_completed_unit(execution):
    run, directory, outcomes, calls, store = execution
    outcomes.extend([(0, 0), (0, 7), (0, 0)])
    with pytest.raises(RuntimeError, match="u2 failed with rc=7"):
        run()
    assert json.loads((directory / "status.json").read_text())["state"] == "failed"
    checkpoint = json.loads((directory / "checkpoint.json").read_text())
    assert set(checkpoint["completed"]) == {"u1"}
    assert checkpoint["failures"][0]["returncode"] == 7
    result = run()
    assert result["completed_units"] == 2
    assert calls == ["u1", "u2", "u2"]
    assert (directory / "artifacts/u1/raw/count.txt").read_text() == "1"
    assert (directory / "artifacts/u2/raw/count.txt").read_text() == "2"
    assert run()["evidence_id"] == result["evidence_id"]
    assert calls == ["u1", "u2", "u2"]


@pytest.mark.parametrize("monitor_rc,unit_rc", [(-9, None), (1, None), (0, None), (0, -9)])
def test_infrastructure_interruption_refuses_restart_before_safe_point(execution, monitor_rc, unit_rc):
    run, directory, outcomes, calls, store = execution
    outcomes.extend([(0, 0), (monitor_rc, unit_rc), (0, 0)])
    with pytest.raises(RuntimeError):
        run()
    # A second invocation must not consume the queued successful attempt.
    try:
        run()
    except RuntimeError as exc:
        assert "interrupted before safe point" in str(exc)
    else:
        pytest.fail(f"interrupted unit replayed: calls={calls}, count=" +
                    (directory / "artifacts/u2/raw/count.txt").read_text())
    assert calls == ["u1", "u2"]
    assert (directory / "artifacts/u2/raw/count.txt").read_text() == "1"
    status = json.loads((directory / "status.json").read_text())
    assert status["state"] == "running" and status["current_unit"] == "u2"
    checkpoint = json.loads((directory / "checkpoint.json").read_text())
    assert set(checkpoint["completed"]) == {"u1"}
    assert checkpoint["failures"] == []
    assert not list(store.records_dir.glob("*.json"))


def test_completed_safe_point_wins_over_stale_running_status(execution):
    run, directory, outcomes, calls, store = execution
    outcomes.extend([(0, 0), (0, 0)])
    result = run()
    status_path = directory / "status.json"
    status = json.loads(status_path.read_text())
    status.update(state="running", current_unit="u2")
    status_path.write_text(json.dumps(status))
    assert run()["evidence_id"] == result["evidence_id"]
    assert calls == ["u1", "u2"]


@pytest.mark.parametrize("unit_rc", [0, 7])
@pytest.mark.parametrize("already_exited", [False, True])
def test_monitor_reports_command_result_without_passing_private_fds(tmp_path, monkeypatch, unit_rc, already_exited):
    import select
    import tempfile

    inherited = []
    class Child:
        pid = 12345
        def wait(self):
            return unit_rc

    def spawn(argv, *, pass_fds):
        assert argv == ["engine", "sandbox", "command"]
        inherited.extend(pass_fds)
        return Child()

    def pidfd_open(pid):
        if already_exited:
            raise ProcessLookupError
        return os.open(os.devnull, os.O_RDONLY)

    monkeypatch.setattr(sandbox.subprocess, "Popen", spawn)
    monkeypatch.setattr(sandbox.os, "pidfd_open", pidfd_open, raising=False)
    monkeypatch.setattr(select, "select", lambda reads, *args: (reads[1:], [], []))
    with tempfile.TemporaryFile() as completion:
        monkeypatch.setattr(sys, "argv", ["monitor", "101", str(completion.fileno()),
                                         "102", "engine", "sandbox", "command"])
        assert sandbox._watch_command() == 0
        completion.seek(0)
        assert completion.read() == str(unit_rc).encode("ascii")
        assert inherited == [102]
