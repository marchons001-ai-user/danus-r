"""Final boundary regressions with real processes."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import pytest
from danus import sandbox

def _marked_pids(marker):
    found = []
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            if str(marker).encode() in path.read_bytes().split(b"\0"):
                found.append(int(path.parent.name))
        except OSError:
            pass
    return found

def test_monitor_sigkill_removes_sandbox_descendants_with_caller_alive(tmp_path):
    marker = tmp_path / "monitor-child"
    ready = tmp_path / "ready"
    source = ("import subprocess,sys,time; from pathlib import Path; "
              f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', {str(marker)!r}], start_new_session=True); "
              f"Path({str(ready)!r}).touch(); time.sleep(60)")
    command = [sys.executable, "-I", "-c", source, str(marker)]
    caller_source = ("from pathlib import Path; from danus.sandbox import run_sandboxed; "
        f"run_sandboxed({command!r}, cwd=Path({str(tmp_path)!r}), "
        f"readable_paths=[Path({sys.prefix!r}),Path({sys.base_prefix!r})], timeout=20); "
        "import time; time.sleep(60)")
    caller = subprocess.Popen([sys.executable, "-c", caller_source], start_new_session=True)
    monitor = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        children = Path(f"/proc/{caller.pid}/task/{caller.pid}/children").read_text().split()
        assert len(children) == 1
        monitor = int(children[0])
        assert b"sandbox.py" in Path(f"/proc/{monitor}/cmdline").read_bytes()
        descendants = [pid for pid in _marked_pids(marker) if pid != monitor]
        assert len(descendants) >= 2
        os.kill(monitor, signal.SIGKILL)
        deadline = time.monotonic() + 3
        while _marked_pids(marker) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert caller.poll() is None, "caller must remain alive"
        assert not _marked_pids(marker), "sandbox descendants survived monitor SIGKILL"
    finally:
        if monitor:
            try:
                os.killpg(monitor, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pid in _marked_pids(marker):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        sandbox._terminate_process_tree(caller)


def test_default_new_legacy_worker_runs_without_plan_or_assignment(tmp_path, monkeypatch):
    from danus.execution import layout as L, loop
    from danus.orchestration import cli
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(tmp_path))
    monkeypatch.setenv("CODEX_BACKEND", "chatgpt")
    cli.do_new("Legacy")
    wl = L.WorkerLayout(next((tmp_path / "Legacy/workers").iterdir()))
    role = loop._read_role(wl)
    assert role["ROLE"] == "worker"
    assert not (wl.project_dir / "plan").exists()
    assert not (wl.project_dir / "progress/ledger.jsonl").exists()
    marker = tmp_path / "legacy-ran"
    agent = tmp_path / "fake-agent"
    agent.write_text("#!/usr/bin/python3\nfrom pathlib import Path\n" + f"Path({str(marker)!r}).touch()\n")
    agent.chmod(0o755)
    monkeypatch.setenv("DANUS_CODEX_BIN", str(agent))
    wl.logs.mkdir(exist_ok=True)
    assert loop.run_round(wl, role, "bounded", wl.logs / "legacy.log", hard_timeout=5) == 0
    assert marker.exists()
    assert not (wl.dir / ".gateway-epoch").exists()


@pytest.mark.parametrize("tool", ["analytical", "empirical", "search"])
@pytest.mark.parametrize("change", ["none", "pause", "assignment", "epoch"])
def test_slow_external_io_releases_control_lock_and_rechecks_authority(tmp_path, monkeypatch, change, tool):
    import json
    import multiprocessing
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from danus.core import GlobalMemory
    from danus.execution.tests.test_s5_d2_worker_isolation import _worker
    from danus.gateway import capability, server
    from danus.orchestration import cli

    wl, ledger, _, _ = _worker(tmp_path)
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(tmp_path))
    binding = capability.issue(wl.project_dir, wl.name, "researcher")
    source_id = GlobalMemory(wl.project_dir).append(
        "conclusion", claim="bounded fact", evidence="candidate", author="r")
    gm_before = {path.name: path.read_bytes() for path in GlobalMemory(wl.project_dir).dir.glob("*.jsonl")}
    entered, release = threading.Event(), threading.Event()

    class Verifier(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            entered.set()
            assert release.wait(10)
            payload = json.dumps({"verdict": "correct"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    http = HTTPServer(("127.0.0.1", 0), Verifier)
    monkeypatch.setenv("DANUS_VERIFY_URL", f"http://127.0.0.1:{http.server_port}/verify")
    monkeypatch.setattr(server, "_arxiv_search", lambda query, num_results=10: server._post_verify(server._verify_endpoint(), {"query": query}))
    ctx = multiprocessing.get_context("fork")
    parent, child = ctx.Pipe()
    mutate_now = ctx.Event()
    def submit():
        server._BOUND_WORKER = binding
        try:
            if tool == "search":
                result = server._bound_tool(server.search_arxiv_theorems)("bounded")
            else:
                result = server._bound_tool(server.fact_submit)(
                    statement="bounded fact", proof="bounded proof", source_id=source_id,
                    fact_type=tool, scope={"unit": "test"}, evidence_refs=["test"])
            child.send(("result", result))
        except (PermissionError, ValueError, OSError) as exc:
            child.send(("denied", str(exc)))
    def alter():
        assert mutate_now.wait(8)
        if change == "pause":
            cli.do_pause("control")
        elif change == "assignment":
            from danus.orchestration.cli import project_control_lock
            with project_control_lock(wl.project_dir):
                ledger.assign(worker="r", role="researcher", node_id="root", plan_version=1,
                              task="replacement", write_task=True)
        elif change == "epoch":
            capability.issue(wl.project_dir, "r", "researcher")

    gateway = ctx.Process(target=submit)
    mutation = ctx.Process(target=alter)
    gateway.start()
    mutation.start()
    threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        assert entered.wait(5), "verifier was never reached"
        mutate_now.set()
        mutation.join(2)
        assert not mutation.is_alive(), "external I/O blocked human/control action"
        assert mutation.exitcode == 0
        release.set()
        assert parent.poll(5)
        outcome, result = parent.recv()
        gateway.join(5)
        assert gateway.exitcode == 0
        if change == "none":
            assert outcome == "result"
            if tool != "search":
                assert result["fact_id"]
        else:
            assert outcome == "denied", result
            assert not list((wl.project_dir / "fact_graph/facts").glob("*.md"))
            assert {path.name: path.read_bytes() for path in GlobalMemory(wl.project_dir).dir.glob("*.jsonl")} == gm_before
    finally:
        release.set()
        mutate_now.set()
        for proc in (gateway, mutation):
            if proc.pid:
                proc.join(3)
                if proc.is_alive():
                    proc.kill()
                    proc.join()
        http.shutdown()
        http.server_close()


@pytest.mark.parametrize("field", ["version", "executable_sha256", "dependencies_sha256"])
def test_runtime_drift_refuses_experiment_and_verifier(tmp_path, monkeypatch, field):
    from danus.core import runtime
    from danus.execution.tests.test_s5_d3_frozen_bundle import _fixture
    from danus.execution.experiment import run_experiment
    from danus.verify.empirical import check_empirical_evidence
    root, _, store, pid = _fixture(tmp_path)
    expected = store.get_protocol(pid)["execution_runtime"]
    with monkeypatch.context() as changed:
        changed.setattr(runtime, "identity", lambda: {**expected, field: "changed"})
        with pytest.raises(ValueError, match="runtime changed"):
            run_experiment(root, pid)
        assert not (root / "experiment_runtime" / pid / "artifacts").exists()
    result = run_experiment(root, pid)
    monkeypatch.setattr(runtime, "identity", lambda: {**expected, field: "changed"})
    with pytest.raises(ValueError, match="runtime changed"):
        run_experiment(root, pid)
    report = check_empirical_evidence(root, scope={"unit": "u1"}, evidence_refs=[result["evidence_id"]])
    assert not report["passed"]
    assert any("runtime changed" in error for error in report["errors"])
    assert (root / "experiment_runtime" / pid / "artifacts/u1/raw/count.txt").read_text() == "1"


def test_historical_runtime_missing_is_readable_but_cannot_execute(tmp_path):
    from danus.core.evidence import protocol_identity, _content_id
    from danus.execution.tests.test_s5_d3_frozen_bundle import _fixture
    from danus.execution.experiment import run_experiment
    root, _, store, pid = _fixture(tmp_path)
    old = store.get_protocol(pid)
    old.pop("execution_runtime")
    old.pop("runtime_id")
    historical_id = _content_id(protocol_identity(old))
    old["protocol_id"] = historical_id
    store._write_once(store._protocol_path(historical_id), old)
    assert store.get_protocol(historical_id) == old
    with pytest.raises(ValueError, match="runtime identity"):
        run_experiment(root, historical_id)
    assert not (root / "experiment_runtime" / historical_id / "artifacts").exists()


def test_installed_dependency_bytes_must_match_their_lock(tmp_path, monkeypatch):
    import base64
    import hashlib
    from importlib import metadata
    from danus.core import runtime
    package = tmp_path / "example.py"
    package.write_text("VALUE = 1\n")
    info = tmp_path / "example-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: example\nVersion: 1.0\n")
    digest = base64.urlsafe_b64encode(hashlib.sha256(package.read_bytes()).digest()).rstrip(b"=").decode()
    (info / "RECORD").write_text(f"example.py,sha256={digest},{package.stat().st_size}\n")
    installed = list(metadata.distributions(path=[str(tmp_path)]))
    monkeypatch.setattr(runtime.metadata, "distributions", lambda **kwargs: installed)
    before = runtime.identity()
    assert runtime.identity() == before
    package.write_text("VALUE = 2\n")
    with pytest.raises(ValueError, match="dependency changed"):
        runtime.identity()


def test_unknown_worker_role_does_not_enter_legacy_path(tmp_path, monkeypatch):
    from danus.execution.tests.test_s5_d2_worker_isolation import _worker
    from danus.execution import loop
    wl, _, _, role = _worker(tmp_path)
    role["ROLE"] = "researchre"
    monkeypatch.setattr(loop.subprocess, "Popen", lambda *a, **k: pytest.fail("unknown role spawned"))
    assert loop.run_round(wl, role, "bounded", tmp_path / "refused.log", hard_timeout=1) == 126
