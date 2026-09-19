"""Real frozen-file execution and official OS isolation; no model calls."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from danus.core import EvidenceStore, bundle
from danus.execution.experiment import run_experiment
from danus.verify.empirical import check_empirical_evidence


def _fixture(tmp_path, extra=""):
    source = tmp_path / "science"
    source.mkdir()
    root = tmp_path / "control"
    root.mkdir()
    reducer = source / "reducer.py"
    reducer.write_text("import json\ndef reduce_metrics(protocol, artifacts):\n    return {'score': json.loads(artifacts[0].read_text())['score']}\n")
    (source / "config.json").write_text('{"value": 10}')
    (source / "input.txt").write_text("2")
    (source / "runner.py").write_text(
        "import json,sys,os,socket\nfrom pathlib import Path\n"
        "output=Path(sys.argv[1])\noutput.parent.mkdir(parents=True,exist_ok=True)\n"
        "count=output.parent/'count.txt'\ncount.write_text(str(int(count.read_text())+1 if count.exists() else 1))\n"
        + extra + "\n"
        "output.write_text(json.dumps({'score':json.loads(Path('config.json').read_text())['value']+int(Path('input.txt').read_text())}))\n"
    )
    store = EvidenceStore(root)
    def ref(name):
        return {**store.artifact_ref(source / name), "path": name}
    protocol_id = store.freeze_protocol(
        question="Frozen content?", hypothesis="A stays A", decision_rule="score == 12",
        primary_metrics=["score"], author="reviewer",
        matrix={"units": [{"id": "u1", "argv": [sys.executable, "runner.py", "raw/result.json"],
                           "artifacts": ["raw/result.json"], "inputs": [ref("input.txt")]}]},
        metric_reducer={**ref("reducer.py"), "function": "reduce_metrics"},
        execution_files=["runner.py", "config.json"], source_root=source,
    )
    return root, source, store, protocol_id


def test_approved_bytes_survive_dirty_workspace_and_all_source_replacements(tmp_path):
    root, source, store, pid = _fixture(tmp_path)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "fixture"], check=True)
    protocol = store.get_protocol(pid)
    _, manifest = bundle.load(root, protocol["bundle_id"])
    assert protocol["bundle_id"] == hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    for name in ("runner.py", "config.json", "input.txt", "reducer.py"):
        (source / name).write_text("raise RuntimeError('B')")
    assert subprocess.run(["git", "-C", str(source), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
    result = run_experiment(root, pid)
    assert result["primary_metrics"] == {"score": 12.0}
    assert check_empirical_evidence(root, scope={"matrix": "one"}, evidence_refs=[result["evidence_id"]])["passed"]
    again = subprocess.run([sys.executable, "-m", "danus.execution.experiment", str(root), pid], capture_output=True, text=True, timeout=20)
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["evidence_id"] == result["evidence_id"]
    assert (root / "experiment_runtime" / pid / "artifacts/u1/raw/count.txt").read_text() == "1"


def test_experiment_cannot_read_live_or_control_write_frozen_or_use_network(tmp_path, monkeypatch):
    live = tmp_path / "science" / "undeclared.txt"
    control = tmp_path / "control" / "secret"
    attack = f'''assert os.environ.get("DANUS_TEST_SECRET") is None
for name in {[str(live), str(control)]!r}:
    try: Path(name).read_bytes()
    except OSError: pass
    else: raise AssertionError("read escaped")
try: Path("config.json").write_text("B")
except OSError: pass
else: raise AssertionError("frozen write escaped")
try: socket.create_connection(("127.0.0.1", 9), timeout=1)
except OSError as e: assert e.errno in (1,13), e
else: raise AssertionError("network escaped")
try: Path("undeclared.txt").read_bytes()
except OSError: pass
else: raise AssertionError("undeclared file visible")
'''
    root, source, _, pid = _fixture(tmp_path, attack)
    live.write_text("LIVE_SECRET")
    control.write_text("CONTROL_SECRET")
    monkeypatch.setenv("DANUS_TEST_SECRET", "HOST_SECRET")
    assert run_experiment(root, pid)["primary_metrics"] == {"score": 12.0}
    assert control.read_text() == "CONTROL_SECRET"


@pytest.mark.parametrize("kind", ["symlink", "parent", "absolute", "directory_symlink"])
def test_freeze_rejects_source_escapes(tmp_path, kind):
    root, source, _, _ = _fixture(tmp_path)
    external = tmp_path / "external"
    external.write_text("EXTERNAL")
    if kind == "symlink":
        (source / "escape").symlink_to(external)
        name = "escape"
    elif kind == "directory_symlink":
        (source / "escape").symlink_to(tmp_path, target_is_directory=True)
        name = "escape/external"
    else:
        name = "../external" if kind == "parent" else str(external)
    with pytest.raises((OSError, ValueError)):
        bundle.freeze(root, source, [name], [])


@pytest.mark.parametrize("kind", ["file", "manifest", "symlink"])
def test_corrupt_frozen_bundle_is_never_executed(tmp_path, kind):
    root, source, store, pid = _fixture(tmp_path)
    directory, _ = bundle.load(root, store.get_protocol(pid)["bundle_id"])
    if kind == "manifest":
        (directory.parent / "manifest.json").write_text('{}')
    elif kind == "symlink":
        (directory / "runner.py").unlink()
        (directory / "runner.py").symlink_to(source / "runner.py")
    else:
        (directory / "runner.py").write_text("B")
    with pytest.raises((OSError, ValueError)):
        run_experiment(root, pid)
    assert not (root / "experiment_runtime" / pid / "artifacts").exists()


def test_freeze_retry_is_identical_and_snapshot_precedes_execution(tmp_path, monkeypatch):
    root, source, store, pid = _fixture(tmp_path)
    from danus.execution import experiment
    protocol = store.get_protocol(pid)
    assert bundle.freeze(root, source, ["runner.py", "config.json"],
                         [protocol["metric_reducer"], *protocol["matrix"]["units"][0]["inputs"]]) == protocol["bundle_id"]
    original = experiment.sandbox.run_sandboxed
    def replaced(*args, **kwargs):
        frozen, _ = bundle.load(root, store.get_protocol(pid)["bundle_id"])
        (frozen / "runner.py").write_text("raise RuntimeError('after snapshot')")
        return original(*args, **kwargs)
    monkeypatch.setattr(experiment.sandbox, "run_sandboxed", replaced)
    assert run_experiment(root, pid)["primary_metrics"] == {"score": 12.0}


def _assigned_worker(root, pid):
    from danus.core import ResearchPlanGraph, ProgressLedger
    from danus.execution import layout as L
    wl = L.WorkerLayout(root / "workers/r")
    wl.dir.mkdir(parents=True)
    ResearchPlanGraph.create(root, node_id="root", title="Root")
    ProgressLedger(root).assign(worker="r", role="experimenter", node_id="root", plan_version=1,
                                task=f"Execute frozen protocol.\nEXPERIMENT_PROTOCOL_ID={pid}", write_task=True)
    return wl, {"ROLE": "experimenter", "MODEL": "test", "REASONING_EFFORT": "low"}


def test_real_bound_gateway_executes_only_assigned_protocol_and_reuses_evidence(tmp_path):
    from danus.execution import worker as W
    from danus.execution.tests.test_s5_d2_worker_isolation import _attack
    root, _, store, pid = _fixture(tmp_path)
    wl, role = _assigned_worker(root, pid)
    with W.worker_session(wl, role) as session:
        with _attack(session, f'''assert failed(call("experiment_run", {{"protocol_id":"0000000000000000"}}))
assert not failed(call("experiment_run", {{"protocol_id":{pid!r}}}))
assert not failed(call("experiment_run", {{"protocol_id":{pid!r}}}))
''') as proc:
            out, err = proc.communicate(timeout=30)
            assert proc.returncode == 0, (out, err)
    assert len(list(store.records_dir.glob("*.json"))) == 1
    assert (root / "experiment_runtime" / pid / "artifacts/u1/raw/count.txt").read_text() == "1"


def _unit_pids(marker):
    found = []
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = path.read_bytes().split(b"\0")
            if args[0] == sys.executable.encode() and str(marker).encode() in args:
                found.append(int(path.parent.name))
        except OSError:
            pass
    return found


@pytest.mark.parametrize("change", ["pause", "gateway", "proxy"])
def test_gateway_experiment_revocation_kills_unit_and_prevents_ambiguous_replay(tmp_path, change):
    import time
    from danus.execution import worker as W
    from danus.execution.tests.test_s5_d2_worker_isolation import _attack
    from danus.orchestration.cli import project_control_lock
    root, _, store, pid = _fixture(tmp_path, 'import time\n(output.parent / "started").touch()\ntime.sleep(60)')
    wl, role = _assigned_worker(root, pid)
    scratch = root / "experiment_runtime" / pid / "artifacts/u1/raw"
    with W.worker_session(wl, role) as session:
        with _attack(session, f'assert failed(call("experiment_run", {{"protocol_id":{pid!r}}}))') as proc:
            deadline = time.monotonic() + 10
            while not (scratch / "started").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert (scratch / "started").exists()
            assert _unit_pids(scratch / "result.json")
            if change == "pause":
                with project_control_lock(root):
                    wl.pause.touch()
                out, err = proc.communicate(timeout=10)
                assert proc.returncode == 0, (out, err)
            elif change == "gateway":
                session["gateway"].kill()
                session["gateway"].wait(timeout=5)
            else:
                W.sandbox._terminate_process_tree(proc)
            deadline = time.monotonic() + 5
            while _unit_pids(scratch / "result.json") and time.monotonic() < deadline:
                time.sleep(0.02)
            assert not _unit_pids(scratch / "result.json")
    assert not list(store.records_dir.glob("*.json"))
    assert (scratch / "count.txt").read_text() == "1"
    with pytest.raises(RuntimeError, match="interrupted before safe point"):
        run_experiment(root, pid)
    assert (scratch / "count.txt").read_text() == "1"


def _refreeze(store, pid, source, **changes):
    from danus.core.evidence import protocol_identity
    identity = protocol_identity(store.get_protocol(pid))
    identity.pop("bundle_id")
    identity.pop("execution_runtime", None)
    identity.pop("runtime_id", None)
    identity.update(changes)
    return store.freeze_protocol(**identity, execution_files=["runner.py", "config.json"], source_root=source)


def test_freeze_captures_uncommitted_bytes_without_using_git_head(tmp_path):
    root, source, store, pid = _fixture(tmp_path)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "fixture"], check=True)
    (source / "config.json").write_text('{"value": 30}')
    dirty = _refreeze(store, pid, source)
    assert dirty != pid
    (source / "config.json").write_text('{"value": 99}')
    assert run_experiment(root, dirty)["primary_metrics"] == {"score": 32.0}
    assert run_experiment(root, pid)["primary_metrics"] == {"score": 12.0}


@pytest.mark.parametrize("kind", ["script", "inline", "absolute_artifact", "parent_artifact"])
def test_execution_requires_declared_script_and_contained_artifacts(tmp_path, kind):
    root, source, store, pid = _fixture(tmp_path)
    matrix = store.get_protocol(pid)["matrix"]
    unit = matrix["units"][0]
    if kind in {"script", "inline"}:
        unit["argv"][1] = "undeclared.py" if kind == "script" else "-c"
        (source / "undeclared.py").write_text("raise RuntimeError('undeclared')")
    else:
        unit["artifacts"] = [str(tmp_path / "escape") if kind == "absolute_artifact" else "../escape"]
    invalid = _refreeze(store, pid, source, matrix=matrix)
    with pytest.raises(ValueError):
        run_experiment(root, invalid)
    assert not (root / "experiment_runtime" / invalid / "artifacts").exists()


def test_symlink_artifact_is_rejected_before_host_reads_it(tmp_path):
    protected = tmp_path / "outside-secret"
    protected.write_text("SECRET")
    root, _, store, pid = _fixture(tmp_path, f"output.symlink_to({str(protected)!r})\nraise SystemExit(0)")
    with pytest.raises(ValueError, match="symlink"):
        run_experiment(root, pid)
    assert protected.read_text() == "SECRET"
    assert not list(store.records_dir.glob("*.json"))


@pytest.mark.parametrize("task", ["Do not execute {pid}", "EXPERIMENT_PROTOCOL_ID={pid}\nEXPERIMENT_PROTOCOL_ID={pid}"])
def test_task_prose_or_ambiguous_binding_grants_no_execution(tmp_path, task):
    from danus.core import ProgressLedger
    from danus.execution import worker as W
    from danus.execution.tests.test_s5_d2_worker_isolation import _attack
    root, _, store, pid = _fixture(tmp_path)
    wl, role = _assigned_worker(root, pid)
    ProgressLedger(root).assign(worker="r", role="experimenter", node_id="root", plan_version=1,
                                task=task.format(pid=pid), write_task=True)
    with W.worker_session(wl, role) as session:
        with _attack(session, f'assert failed(call("experiment_run", {{"protocol_id":{pid!r}}}))') as proc:
            out, err = proc.communicate(timeout=10)
            assert proc.returncode == 0, (out, err)
    assert not (root / "experiment_runtime").exists()
    assert not list(store.records_dir.glob("*.json"))
