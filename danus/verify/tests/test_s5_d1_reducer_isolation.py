"""Adversarial S5-D1 tests using the real Codex sandbox when available."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from danus.core import EvidenceStore
from danus.core.metrics import _reduce_metrics_in_process
from danus.execution import sandbox
from danus.verify.empirical import check_empirical_evidence


def _codex_available() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        sandbox._resolved_codex()
    except (FileNotFoundError, RuntimeError):
        return False
    return True


requires_sandbox = pytest.mark.skipif(
    not _codex_available(), reason="official Linux Codex sandbox is unavailable"
)


def _project(root: Path, source: str, *, value: float = 1.0):
    store = EvidenceStore(root)
    reducer = root / "workspace" / "reduce_metrics.py"
    artifact = root / "workspace" / "artifact.json"
    reducer.parent.mkdir(parents=True)
    reducer.write_text(source, encoding="utf-8")
    artifact.write_text('{"score": 1}\n', encoding="utf-8")
    reducer_ref = store.artifact_ref(reducer)
    reducer_ref["function"] = "reduce_metrics"
    protocol_id = store.freeze_protocol(
        question="Does the isolated reducer produce score 1?",
        hypothesis="The reducer produces score 1.",
        matrix={"unit": [1]},
        primary_metrics=["score"],
        decision_rule="support iff score == 1",
        author="reviewer",
        metric_reducer=reducer_ref,
    )
    evidence_id = store.add_record(
        protocol_id=protocol_id,
        author="experimenter",
        matrix_coverage={"planned": 1, "completed": 1},
        primary_metrics={"score": value},
        artifacts=[store.artifact_ref(artifact)],
    )
    return store, reducer, artifact, evidence_id


def _check(root: Path, evidence_id: str):
    return check_empirical_evidence(
        root,
        scope={"dataset": "s5-d1"},
        evidence_refs=[evidence_id],
    )


def _normal_source() -> str:
    return """import json

def reduce_metrics(protocol, artifacts):
    return {"score": json.loads(artifacts[0].read_text())["score"]}
"""


@requires_sandbox
def test_normal_reducer_matches_old_metrics_and_empirical_result(tmp_path: Path, monkeypatch):
    store, reducer, artifact, evidence_id = _project(tmp_path, _normal_source())
    protocol = store.get_protocol(store.get_record(evidence_id)["protocol_id"])
    assert protocol is not None
    refs = store.get_record(evidence_id)["artifacts"]
    assert _reduce_metrics_in_process(tmp_path, protocol, refs) == {"score": 1.0}
    monkeypatch.setattr(
        "danus.core.metrics._reduce_metrics_in_process",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("empirical verifier used the in-process reducer")
        ),
    )
    out = _check(tmp_path, evidence_id)
    assert out["passed"] is True
    assert out["errors"] == []
    assert reducer.read_text(encoding="utf-8") == _normal_source()
    assert artifact.read_text(encoding="utf-8") == '{"score": 1}\n'


@requires_sandbox
def test_top_level_reducer_cannot_modify_progress(tmp_path: Path):
    progress = tmp_path / "progress" / "ledger.jsonl"
    progress.parent.mkdir()
    progress.write_text("ORIGINAL\n", encoding="utf-8")
    source = f'''from pathlib import Path

def _must_be_denied():
    try:
        Path({str(progress)!r}).write_text("FORGED", encoding="utf-8")
    except OSError:
        return
    raise RuntimeError("progress write unexpectedly succeeded")

_must_be_denied()

def reduce_metrics(protocol, artifacts):
    return {{"score": 1.0}}
'''
    _, _, _, evidence_id = _project(tmp_path, source)
    out = _check(tmp_path, evidence_id)
    assert out["passed"] is True
    assert progress.read_text(encoding="utf-8") == "ORIGINAL\n"


@requires_sandbox
def test_function_reducer_cannot_modify_control_truth_or_pause(tmp_path: Path):
    plan = tmp_path / "plan" / "current.json"
    fact = tmp_path / "fact_graph" / "facts" / "fact.json"
    pause = tmp_path / "PAUSE"
    for path in (plan, fact, pause):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ORIGINAL", encoding="utf-8")
    source = f'''from pathlib import Path

def _must_be_denied(path):
    try:
        Path(path).write_text("FORGED", encoding="utf-8")
    except OSError:
        return
    raise RuntimeError("control write unexpectedly succeeded")

def reduce_metrics(protocol, artifacts):
    for path in ({str(plan)!r}, {str(fact)!r}, {str(pause)!r}):
        _must_be_denied(path)
    return {{"score": 1.0}}
'''
    _, _, _, evidence_id = _project(tmp_path, source)
    out = _check(tmp_path, evidence_id)
    assert out["passed"] is True
    assert [path.read_text(encoding="utf-8") for path in (plan, fact, pause)] == [
        "ORIGINAL",
        "ORIGINAL",
        "ORIGINAL",
    ]


@requires_sandbox
def test_reducer_does_not_see_verifier_secret_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DANUS_TEST_SECRET", "verifier-secret")
    source = """import os

def reduce_metrics(protocol, artifacts):
    if os.environ.get("DANUS_TEST_SECRET") is not None:
        raise RuntimeError("verifier secret crossed boundary")
    return {"score": 1.0}
"""
    _, _, _, evidence_id = _project(tmp_path, source)
    assert _check(tmp_path, evidence_id)["passed"] is True


@requires_sandbox
def test_reducer_cannot_use_loopback_or_external_network(tmp_path: Path):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    source = f'''import socket

def reduce_metrics(protocol, artifacts):
    for address in (("127.0.0.1", {port}), ("203.0.113.1", 80)):
        try:
            sock = socket.create_connection(address, timeout=1)
        except OSError:
            continue
        sock.close()
        raise RuntimeError("network unexpectedly succeeded")
    return {{"score": 1.0}}
'''
    try:
        _, _, _, evidence_id = _project(tmp_path, source)
        assert _check(tmp_path, evidence_id)["passed"] is True
    finally:
        listener.close()


@requires_sandbox
def test_reducer_child_process_inherits_denial(tmp_path: Path):
    progress = tmp_path / "progress" / "ledger.jsonl"
    progress.parent.mkdir()
    progress.write_text("ORIGINAL\n", encoding="utf-8")
    source = f'''import subprocess
import sys

def reduce_metrics(protocol, artifacts):
    child = subprocess.run([
        sys.executable, "-c",
        "from pathlib import Path; Path(__import__('sys').argv[1]).write_text('FORGED')",
        {str(progress)!r},
    ], check=False)
    if child.returncode == 0:
        raise RuntimeError("child escaped sandbox")
    return {{"score": 1.0}}
'''
    _, _, _, evidence_id = _project(tmp_path, source)
    assert _check(tmp_path, evidence_id)["passed"] is True
    assert progress.read_text(encoding="utf-8") == "ORIGINAL\n"


@requires_sandbox
def test_infinite_reducer_times_out_and_child_dies(tmp_path: Path, monkeypatch):
    token = f"danus-s5-d1-child-{os.getpid()}"
    source = f'''import subprocess
import sys

def reduce_metrics(protocol, artifacts):
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", {token!r}])
    while True:
        pass
'''
    _, _, _, evidence_id = _project(tmp_path, source)
    monkeypatch.setattr("danus.core.metrics.REDUCER_TIMEOUT_SECONDS", 1.0)
    out = _check(tmp_path, evidence_id)
    assert out["passed"] is False
    time.sleep(0.2)
    survivors = []
    for proc_dir in Path("/proc").glob("[0-9]*"):
        try:
            command = (proc_dir / "cmdline").read_bytes().decode(errors="ignore")
        except OSError:
            continue
        if token in command:
            survivors.append(proc_dir)
    assert survivors == []


def test_missing_codex_fails_closed_without_running_reducer(tmp_path: Path, monkeypatch):
    sentinel = tmp_path / "ran"
    source = f'''from pathlib import Path
Path({str(sentinel)!r}).write_text("ran", encoding="utf-8")

def reduce_metrics(protocol, artifacts):
    return {{"score": 1.0}}
'''
    _, _, _, evidence_id = _project(tmp_path, source)
    monkeypatch.setattr(sandbox.codex, "resolve_bin", lambda: "/missing/codex")
    out = _check(tmp_path, evidence_id)
    assert out["passed"] is False
    assert not sentinel.exists()


@pytest.mark.parametrize(
    "source",
    [
        "def reduce_metrics(protocol, artifacts):\n    return {'score': float('nan')}\n",
        "def reduce_metrics(protocol, artifacts):\n    return {'wrong': 1.0}\n",
        "def reduce_metrics(protocol, artifacts):\n    return {'score': object()}\n",
        "import os\ndef reduce_metrics(protocol, artifacts):\n    os.write(1, b'DANUS_REDUCER_RESULT={bad}\\n')\n    return {'score': 1.0}\n",
    ],
)
@requires_sandbox
def test_invalid_reducer_output_is_rejected(tmp_path: Path, source: str):
    _, _, _, evidence_id = _project(tmp_path, source)
    out = _check(tmp_path, evidence_id)
    assert out["passed"] is False
    assert any("primary_metrics_recompute" in error for error in out["errors"])
