"""A2-7i workspace binding / pilot-isolation tests. Offline; no model/network calls."""
from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from danus.core import EvidenceStore
from danus.execution import layout as L
from danus.execution.experiment import run_experiment
from danus.execution.scaffold import do_new


RUNNER = r'''from pathlib import Path
import json, sys
output = Path(sys.argv[1])
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps({"score": 2.0, "cwd": str(Path.cwd())}))
'''

REDUCER = r'''import json

def reduce_metrics(protocol, artifacts):
    value = json.loads(artifacts[0].read_text())["score"]
    return {"score": value}
'''


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


def _protocol(control: Path, workspace: Path, *, artifact: str = "raw/result.json") -> str:
    runner = workspace / "runner.py"
    reducer = workspace / "reducer.py"
    runner.write_text(RUNNER, encoding="utf-8")
    reducer.write_text(REDUCER, encoding="utf-8")
    store = EvidenceStore(control)
    reducer_ref = store.artifact_ref(reducer)
    reducer_ref["function"] = "reduce_metrics"
    return store.freeze_protocol(
        question="Does the isolated workspace run produce score 2?",
        hypothesis="The score is 2.",
        matrix={"units": [{
            "id": "u1",
            "argv": [sys.executable, "runner.py", artifact],
            "artifacts": [artifact],
            "inputs": [store.artifact_ref(runner)],
        }]},
        primary_metrics=["score"],
        decision_rule="support iff score == 2",
        author="reviewer",
        metric_reducer=reducer_ref,
    )


def test_bound_project_keeps_control_files_outside_workspace(tmp_path: Path):
    agents = tmp_path / "control"
    workspace = tmp_path / "scientific"
    readonly = tmp_path / "raw-source"
    workspace.mkdir()
    readonly.mkdir()

    with _env(DANUS_AGENTS_ROOT=agents):
        result = do_new(
            "P", roles="high:1", model="m",
            workspace=str(workspace), readonly_inputs=[str(readonly)],
        )
        control = L.project_dir("P")
        wl = L.WorkerLayout(L.worker_dir("P", "high"))

        assert Path(result["workspace"]) == workspace.resolve()
        assert L.scientific_workspace(control) == workspace.resolve()
        assert L.readonly_inputs(control) == [readonly.resolve()]
        assert wl.workspace.is_symlink()
        assert wl.workspace.resolve() == workspace.resolve()
        assert wl.task.is_file() and wl.codex_config.is_file()
        assert not (workspace / "TASK.md").exists()
        assert not (workspace / "AGENTS.md").exists()
        assert not (workspace / "project.json").exists()

        meta = json.loads((control / L.PROJECT_META_FILE).read_text(encoding="utf-8"))
        assert meta["workspace"] == str(workspace.resolve())
        assert meta["readonly_inputs"] == [str(readonly.resolve())]


def test_danus_new_can_bind_from_existing_env_surface(tmp_path: Path):
    agents = tmp_path / "control"
    workspace = tmp_path / "scientific"
    readonly = tmp_path / "raw-source"
    workspace.mkdir()
    readonly.mkdir()

    with _env(
        DANUS_AGENTS_ROOT=agents,
        DANUS_SCIENTIFIC_WORKSPACE=workspace,
        DANUS_READONLY_INPUTS=readonly,
    ):
        result = do_new("P", roles="high:1", model="m")
        assert result["workspace"] == str(workspace.resolve())
        assert result["readonly_inputs"] == [str(readonly.resolve())]


def test_experiment_runtime_executes_and_writes_only_relative_science_in_workspace(tmp_path: Path):
    agents = tmp_path / "control-root"
    workspace = tmp_path / "scientific"
    workspace.mkdir()

    with _env(DANUS_AGENTS_ROOT=agents):
        do_new("P", roles="high:1", model="m", workspace=str(workspace))
        control = L.project_dir("P")
        protocol_id = _protocol(control, workspace)
        result = run_experiment(control, protocol_id)

    assert result["primary_metrics"] == {"score": 2.0}
    payload = json.loads((workspace / "raw" / "result.json").read_text(encoding="utf-8"))
    assert Path(payload["cwd"]) == workspace.resolve()
    assert not (control / "raw" / "result.json").exists()
    assert (control / "experiment_runtime" / protocol_id / "checkpoint.json").is_file()
    assert (control / "evidence" / "records" / f"{result['evidence_id']}.json").is_file()

    record = EvidenceStore(control).get_record(result["evidence_id"])
    assert record is not None
    assert Path(record["artifacts"][0]["path"]) == (workspace / "raw" / "result.json").resolve()


def test_declared_readonly_input_cannot_be_an_artifact_target(tmp_path: Path):
    agents = tmp_path / "control-root"
    workspace = tmp_path / "scientific"
    readonly = tmp_path / "raw-source"
    workspace.mkdir()
    readonly.mkdir()
    forbidden = readonly / "should-not-write.json"

    with _env(DANUS_AGENTS_ROOT=agents):
        do_new(
            "P", roles="high:1", model="m",
            workspace=str(workspace), readonly_inputs=[str(readonly)],
        )
        control = L.project_dir("P")
        protocol_id = _protocol(control, workspace, artifact=str(forbidden))
        with pytest.raises(ValueError, match="read-only input"):
            run_experiment(control, protocol_id)

    assert not forbidden.exists()


def test_workspace_and_readonly_inputs_must_be_separate(tmp_path: Path):
    agents = tmp_path / "control-root"
    workspace = tmp_path / "scientific"
    workspace.mkdir()
    nested = workspace / "source"
    nested.mkdir()

    with _env(DANUS_AGENTS_ROOT=agents):
        with pytest.raises(SystemExit, match="must not overlap"):
            do_new(
                "P", roles="high:1", model="m",
                workspace=str(workspace), readonly_inputs=[str(nested)],
            )
