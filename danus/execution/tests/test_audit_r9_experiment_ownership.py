from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from danus.core import EvidenceStore
from danus.execution.experiment import run_experiment


RUNNER = r'''from pathlib import Path
import json, os, sys, time
output = Path(sys.argv[1])
scratch = output.parent.parent
count = (scratch / "count.txt")
count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))
(scratch / "unit.pid").write_text(str(os.getpid()))
if (scratch / "fail").exists():
    raise SystemExit(7)
deadline = time.time() + 15
while not (scratch / "release").exists():
    if time.time() >= deadline:
        raise SystemExit(91)
    time.sleep(0.05)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps({"score": 1.0}))
'''

REDUCER = r'''import json

def reduce_metrics(protocol, artifacts):
    return {"score": json.loads(artifacts[0].read_text())["score"]}
'''


def _fixture(root: Path) -> str:
    runner = root / "runner.py"
    reducer = root / "reducer.py"
    runner.write_text(RUNNER, encoding="utf-8")
    reducer.write_text(REDUCER, encoding="utf-8")
    store = EvidenceStore(root)
    reducer_ref = store.artifact_ref(reducer)
    reducer_ref["function"] = "reduce_metrics"
    protocol_id = store.freeze_protocol(
        question="Does the owned unit produce score 1?",
        hypothesis="The frozen unit produces score 1.",
        matrix={
            "units": [{
                "id": "u1",
                "argv": [sys.executable, "runner.py", "raw/u1.json"],
                "artifacts": ["raw/u1.json"],
            }]
        },
        primary_metrics=["score"],
        decision_rule="support iff score == 1",
        author="reviewer",
        metric_reducer=reducer_ref,
        execution_files=["runner.py"],
    )

    (root / "experiment_runtime" / protocol_id / "artifacts/u1").mkdir(parents=True)
    return protocol_id


def _controller(root: Path, protocol_id: str, **kwargs):
    return subprocess.run(
        [sys.executable, "-m", "danus.execution.experiment", str(root), protocol_id],
        text=True,
        **kwargs,
    )


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _pid_alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def test_controller_death_terminates_unit_and_unknown_unit_is_not_rerun(tmp_path: Path):
    protocol_id = _fixture(tmp_path)
    pid_path = tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/unit.pid"
    release = tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/release"
    controller = subprocess.Popen(
        [sys.executable, "-m", "danus.execution.experiment", str(tmp_path), protocol_id],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    unit_pid = None
    try:
        _wait_for(pid_path)
        marker = str(pid_path.parent / "raw/u1.json").encode()
        unit_pid = next(int(p.parent.name) for p in Path("/proc").glob("[0-9]*/cmdline")
                        if marker in p.read_bytes().split(b"\0") and p.read_bytes().split(b"\0")[0] == sys.executable.encode())
        assert _pid_alive(unit_pid)

        os.kill(controller.pid, signal.SIGKILL)
        assert controller.wait(timeout=5) == -signal.SIGKILL

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(unit_pid):
            time.sleep(0.02)
        assert not _pid_alive(unit_pid)

        replay = _controller(tmp_path, protocol_id, capture_output=True, timeout=5)
        assert replay.returncode != 0
        assert "interrupted before safe point; refusing automatic rerun" in replay.stderr
        assert (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/count.txt").read_text() == "1"
    finally:
        release.touch()
        if controller.poll() is None:
            controller.kill()
            controller.wait(timeout=5)
        if unit_pid is not None and _pid_alive(unit_pid):
            os.kill(unit_pid, signal.SIGKILL)


def test_completed_safe_point_is_not_blocked_by_stale_running_status(tmp_path: Path):
    protocol_id = _fixture(tmp_path)
    (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/release").touch()
    first = run_experiment(tmp_path, protocol_id)
    assert (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/count.txt").read_text() == "1"

    status_path = tmp_path / "experiment_runtime" / protocol_id / "status.json"
    status_path.write_text(
        json.dumps({
            "protocol_id": protocol_id,
            "state": "running",
            "completed_units": 0,
            "total_units": 1,
            "current_unit": "u1",
            "attempt": 1,
        }),
        encoding="utf-8",
    )

    again = run_experiment(tmp_path, protocol_id)
    assert again["evidence_id"] == first["evidence_id"]
    assert (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/count.txt").read_text() == "1"


def test_durable_failed_unit_remains_retryable(tmp_path: Path):
    protocol_id = _fixture(tmp_path)
    (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/fail").touch()
    with pytest.raises(RuntimeError, match="u1 failed"):
        run_experiment(tmp_path, protocol_id)
    assert (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/count.txt").read_text() == "1"

    (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/fail").unlink()
    (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/release").touch()
    result = run_experiment(tmp_path, protocol_id)
    assert result["completed_units"] == 1
    assert (tmp_path / "experiment_runtime" / protocol_id / "artifacts/u1/count.txt").read_text() == "2"
