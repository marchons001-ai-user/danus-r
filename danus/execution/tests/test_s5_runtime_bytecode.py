"""Real RECORD bytecode drift checks with the official sandbox; no model calls."""

import base64
import hashlib
from importlib import metadata, util
import json
import marshal
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import sysconfig
import tempfile
import venv

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    # Host libraries serve only the controller; the sandbox gets the clean venv.
    sys.path.append(sys.argv[3])

import pytest


@pytest.mark.parametrize("change", ["pyc", "source", "none"])
def test_record_dependency_drift_before_execution_and_verification(change):
    # The sandbox must see the disposable interpreter under its allowed runtime.
    with tempfile.TemporaryDirectory(prefix="danus-runtime-", dir=Path.home() / ".cache") as directory:
        root = Path(directory)
        venv.EnvBuilder(with_pip=False).create(root / "venv")
        result = subprocess.run(
            [str(root / "venv/bin/python"), "-B", str(Path(__file__).resolve()),
             str(root), change, sysconfig.get_path("purelib")],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def _check(root, change):
    from danus.core import EvidenceStore, runtime
    from danus.execution.experiment import run_experiment
    from danus.verify.empirical import check_empirical_evidence

    site = Path(sysconfig.get_path("purelib"))
    module = site / "r19_dependency.py"
    module.write_text("VALUE = 1\n")
    cache = Path(util.cache_from_source(str(module)))
    py_compile.compile(str(module), doraise=True)
    info = site / "r19_dependency-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: r19_dependency\nVersion: 1.0\n")
    digest = base64.urlsafe_b64encode(hashlib.sha256(module.read_bytes()).digest()).rstrip(b"=").decode()
    record = info / "RECORD"
    record.write_text(
        f"r19_dependency.py,sha256={digest},{module.stat().st_size}\n"
        f"{cache.relative_to(site).as_posix()},,\n"
        f"{info.name}/METADATA,,\n{info.name}/RECORD,,\n"
    )
    dist = metadata.distribution("r19_dependency")
    entry = next(item for item in dist.files if Path(dist.locate_file(item)) == cache)
    assert entry.hash is None
    print(f"RECORD cache: {entry}; hash={entry.hash}", flush=True)

    original_source, original_cache, original_record = module.read_bytes(), cache.read_bytes(), record.read_bytes()
    def imported_value():
        return subprocess.check_output(
            [sys.executable, "-I", "-B", "-c", "import r19_dependency; print(r19_dependency.VALUE)"],
            text=True,
        ).strip()
    assert imported_value() == "1"

    control, source = root / "control", root / "science"
    control.mkdir()
    source.mkdir()
    (source / "runner.py").write_text(
        "import json,sys,r19_dependency\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps({'score': r19_dependency.VALUE}))\n"
    )
    (source / "reducer.py").write_text(
        "import json\ndef reduce_metrics(protocol, artifacts):\n"
        "    return json.loads(artifacts[0].read_text())\n"
    )
    store = EvidenceStore(control)
    pid = store.freeze_protocol(
        question="Runtime dependency stays fixed", hypothesis="score == 1",
        primary_metrics=["score"], decision_rule="score == 1", author="reviewer",
        matrix={"units": [{"id": "u1", "argv": [sys.executable, "runner.py", "result.json"],
                           "artifacts": ["result.json"]}]},
        metric_reducer={**store.artifact_ref(source / "reducer.py"),
                        "path": "reducer.py", "function": "reduce_metrics"},
        execution_files=["runner.py"], source_root=source,
    )
    protocol = store.get_protocol(pid)
    before = runtime.identity()
    assert before == runtime.identity() == protocol["execution_runtime"]

    def mutate():
        if change == "pyc":
            cache.write_bytes(original_cache[:16] + marshal.dumps(compile("VALUE = 2\n", str(module), "exec")))
            assert cache.read_bytes()[:16] == original_cache[:16]
            assert module.read_bytes() == original_source
            assert record.read_bytes() == original_record
            assert imported_value() == "2"
            assert runtime.identity()["dependencies_sha256"] != before["dependencies_sha256"]
        elif change == "source":
            module.write_text("VALUE = 2\n")

    error = "runtime changed" if change == "pyc" else "dependency changed"
    if change != "none":
        mutate()
        with pytest.raises(ValueError, match=error):
            run_experiment(control, pid)
        assert not (control / "experiment_runtime" / pid / "artifacts").exists()
        assert not list(store.records_dir.glob("*.json"))
        if change == "source":
            module.write_bytes(original_source)
        cache.write_bytes(original_cache)
        assert runtime.identity() == before

    result = run_experiment(control, pid)
    assert result["primary_metrics"] == {"score": 1.0}
    assert check_empirical_evidence(control, scope={"unit": "u1"}, evidence_refs=[result["evidence_id"]])["passed"]
    assert runtime.identity() == before
    mutate()
    report = check_empirical_evidence(control, scope={"unit": "u1"}, evidence_refs=[result["evidence_id"]])
    if change == "none":
        assert report["passed"]
        assert run_experiment(control, pid)["evidence_id"] == result["evidence_id"]
        assert runtime.identity() == before
    else:
        assert not report["passed"]
        assert any(error in item for item in report["errors"]), report
        with pytest.raises(ValueError, match=error):
            run_experiment(control, pid)


if __name__ == "__main__":
    _check(Path(sys.argv[1]), sys.argv[2])
