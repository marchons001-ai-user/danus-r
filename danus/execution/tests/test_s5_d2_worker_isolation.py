"""Real official-sandbox workers and external Gateway, without model API calls."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from danus import sandbox
from danus.core import GlobalMemory, ProgressLedger, ResearchPlanGraph
from danus.execution import layout as L
from danus.execution import worker as W
from danus.gateway import capability


def _worker(tmp_path, role="researcher"):
    root = tmp_path / "control"
    wl = L.WorkerLayout(root / "workers" / "r")
    wl.dir.mkdir(parents=True)
    ResearchPlanGraph.create(root, node_id="root", title="Root")
    ledger = ProgressLedger(root)
    assignment = ledger.assign(worker="r", role=role, node_id="root", plan_version=1,
                               task="Bound task\n", write_task=True)
    return wl, ledger, assignment, {"ROLE": role, "MODEL": "test", "REASONING_EFFORT": "low"}


def _client_source(session):
    proxy = session["proxy_command"][-2]
    return f'''import errno, json, os, subprocess, sys
from pathlib import Path

proxy = subprocess.Popen([sys.executable, "-I", {proxy!r}, sys.argv[1]],
                         pass_fds=(int(sys.argv[1]),), stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True)

def rpc(method, params, request_id=1):
    proxy.stdin.write(json.dumps({{"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}}) + "\\n")
    proxy.stdin.flush()
    while True:
        line = proxy.stdout.readline()
        if not line:
            raise RuntimeError("gateway disconnected")
        message = json.loads(line)
        if message.get("id") == request_id:
            return message

rpc("initialize", {{"protocolVersion": "2024-11-05", "capabilities": {{}}, "clientInfo": {{"name": "attack", "version": "1"}}}})
proxy.stdin.write('{{"jsonrpc":"2.0","method":"notifications/initialized"}}\\n')
proxy.stdin.flush()

def call(name, arguments):
    return rpc("tools/call", {{"name": name, "arguments": arguments}}, 2)

def failed(message):
    return bool(message.get("error") or message.get("result", {{}}).get("isError"))
'''


@contextmanager
def _attack(session, body):
    path = session["cwd"] / "attack.py"
    path.write_text(_client_source(session) + "\n" + body)
    cmd = [str(session["engine"]), "sandbox", "-P", "danus-worker", "-C", str(session["cwd"]),
           sys.executable, "-I", str(path), str(session["fd"])]
    proc = subprocess.Popen(cmd, env=sandbox._sandbox_env(session["home"], session["engine"], {}),
                            pass_fds=(session["fd"],), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            sandbox._terminate_process_tree(proc)


@pytest.mark.parametrize("role", ["researcher", "reviewer", "experimenter"])
def test_real_worker_denies_files_and_forged_authority(tmp_path, role):
    wl, _, _, config = _worker(tmp_path, role)
    protected = [wl.project_dir / name for name in (
        "plan/current.json", "progress/ledger.jsonl", "fact_graph/facts/secret.json",
        "PAUSE", "workers/other/local_memory/secret.txt",
    )]
    for path in protected:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("HOST_SECRET")
    before = {path: path.read_bytes() for path in protected}
    with W.worker_session(wl, config) as session:
        body = f'''
denied = 0
for name in {[str(p) for p in protected]!r}:
    for action in (lambda p: p.read_bytes(), lambda p: p.write_text("FORGED"), lambda p: p.unlink()):
        try:
            action(Path(name))
        except OSError as exc:
            assert exc.errno in (errno.ENOENT, errno.EACCES, errno.EPERM, errno.EROFS)
            denied += 1
assert denied == 15, denied
os.environ["DANUS_ROLE"] = "main"
os.environ["DANUS_ASSIGNMENT_ID"] = "forged"
assert not failed(call("gm_add", {{"kind": "plan", "claim": "authorized finding"}}))
assert failed(call("fact_revoke", {{"fact_id": "forged", "reason": "attack"}}))
assert failed(call("gm_add", {{"kind": "plan", "claim": "cross project", "project": "other"}}))
assert failed(call("gm_add", {{"kind": "plan", "claim": "forged identity", "role": "main", "assignment_id": "forged"}}))
print("ATTACKS_DENIED", flush=True)
'''
        with _attack(session, body) as proc:
            out, err = proc.communicate(timeout=20)
            assert proc.returncode == 0, (out, err)
            assert "ATTACKS_DENIED" in out
    assert all(path.read_bytes() == value for path, value in before.items())
    entries = GlobalMemory(wl.project_dir).read("plan")
    assert len(entries) == 1 and entries[0]["author"] == "r"


@pytest.mark.parametrize("change", ["assignment", "epoch", "pause"])
def test_live_capability_cannot_revive_after_change(tmp_path, change):
    wl, ledger, _, config = _worker(tmp_path)
    with W.worker_session(wl, config) as session:
        body = '''
assert not failed(call("gm_add", {"kind":"plan", "claim":"before"}))
print("READY", flush=True)
input()
assert failed(call("gm_add", {"kind":"plan", "claim":"stale"}))
print("REVOKED", flush=True)
input()
assert failed(call("gm_add", {"kind":"plan", "claim":"revived"}))
'''
        with _attack(session, body) as proc:
            assert proc.stdout.readline().strip() == "READY"
            if change == "assignment":
                ledger.assign(worker="r", role="researcher", node_id="root", plan_version=1,
                              task="Replacement task\n", write_task=True)
            elif change == "epoch":
                capability.issue(wl.project_dir, "r", "researcher")
            else:
                wl.pause.write_text("manual\n")
            proc.stdin.write("continue\n")
            proc.stdin.flush()
            assert proc.stdout.readline().strip() == "REVOKED"
            wl.pause.unlink(missing_ok=True)
            out, err = proc.communicate("continue\n", timeout=20)
            assert proc.returncode == 0, (out, err)
    assert len(GlobalMemory(wl.project_dir).read("plan")) == 1


def test_gateway_crash_and_new_round_do_not_restore_old_channel(tmp_path):
    wl, _, _, config = _worker(tmp_path)
    with W.worker_session(wl, config) as session:
        body = '''
assert not failed(call("gm_add", {"kind":"plan", "claim":"before crash"}))
print("READY", flush=True)
input()
try:
    call("gm_add", {"kind":"plan", "claim":"after crash"})
except (OSError, RuntimeError):
    print("DISCONNECTED", flush=True)
else:
    raise AssertionError("dead Gateway accepted a call")
'''
        with _attack(session, body) as proc:
            assert proc.stdout.readline().strip() == "READY"
            session["gateway"].kill()
            session["gateway"].wait(timeout=5)
            with pytest.raises(PermissionError, match="disconnected"):
                W.check_session(session)
            out, err = proc.communicate("continue\n", timeout=10)
            assert proc.returncode == 0 and "DISCONNECTED" in out, (out, err)
    with W.worker_session(wl, config) as restarted:
        assert restarted["binding"]["epoch"] != session["binding"]["epoch"]
        with _attack(restarted, 'assert not failed(call("gm_add", {"kind":"plan", "claim":"new round"}))') as proc:
            out, err = proc.communicate(timeout=20)
            assert proc.returncode == 0, (out, err)
    assert len(GlobalMemory(wl.project_dir).read("plan")) == 2


@pytest.mark.parametrize("action", ["proxy.stdin.close(); assert proxy.wait(timeout=5) == 1", "proxy.kill(); assert proxy.wait(timeout=5) < 0"])
def test_proxy_disconnect_closes_external_gateway(tmp_path, action):
    wl, _, _, config = _worker(tmp_path)
    with W.worker_session(wl, config) as session:
        with _attack(session, action) as proc:
            out, err = proc.communicate(timeout=15)
            assert proc.returncode == 0, (out, err)
        session["gateway"].wait(timeout=5)
        with pytest.raises(PermissionError, match="disconnected"):
            W.check_session(session)


def test_dsh_cannot_fall_back_to_unisolated_worker(tmp_path, monkeypatch):
    wl, _, _, config = _worker(tmp_path)
    monkeypatch.setenv("CODEX_BACKEND", "dsh")
    with pytest.raises(RuntimeError, match="DSH"):
        with W.worker_session(wl, config):
            pytest.fail("unisolated DSH worker was launched")


def test_codex_mcp_proxy_uses_one_shot_socket_without_inherited_gateway_fd(tmp_path):
    """Codex 0.160 may close the extra FD while spawning MCP subprocesses."""
    wl, _, _, role = _worker(tmp_path)
    with W.worker_session(wl, role) as session:
        assert session["gateway_socket"].is_socket()
        script = session["cwd"] / "socket_bridge_test.py"
        script.write_text(
            "import json, subprocess, sys\n"
            f"p=subprocess.Popen([{sys.executable!r}, '-I', "
            f"{str(session['cwd'] / 'gateway_proxy.py')!r}, '--socket', "
            f"{str(session['gateway_socket'])!r}], "
            "stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, close_fds=True)\n"
            "def rpc(method, params, ident):\n"
            "    p.stdin.write(json.dumps({'jsonrpc':'2.0','id':ident,"
            "'method':method,'params':params})+'\\n')\n"
            "    p.stdin.flush()\n"
            "    while True:\n"
            "        line=p.stdout.readline()\n"
            "        assert line, 'MCP proxy closed before its response'\n"
            "        message=json.loads(line)\n"
            "        if message.get('id') == ident:\n"
            "            return message\n"
            "r=rpc('initialize', {'protocolVersion':'2024-11-05',"
            "'capabilities':{},'clientInfo':{'name':'socket-smoke','version':'1'}}, 1)\n"
            "assert 'result' in r, r\n"
            "p.stdin.write(json.dumps({'jsonrpc':'2.0',"
            "'method':'notifications/initialized'})+'\\n')\n"
            "p.stdin.flush()\n"
            "r=rpc('tools/call',{'name':'gm_search',"
            "'arguments':{'query':'__socket_bridge_smoke_no_match__'}},2)\n"
            "assert 'result' in r and not r['result'].get('isError'), r\n"
            "r=rpc('tools/call',{'name':'fact_revoke',"
            "'arguments':{'fact_id':'forged','reason':'attack'}},3)\n"
            "assert 'error' in r or r.get('result',{}).get('isError'), r\n"
            "print('SOCKET_BRIDGE_PASS',flush=True)\n"
            "p.stdin.close()\n"
            "p.wait(timeout=5)\n"
        )
        cmd = [
            str(session["engine"]), "sandbox", "-P", "danus-worker",
            "-C", str(session["cwd"]), sys.executable, "-I", str(script),
        ]
        result = subprocess.run(
            cmd, cwd=session["cwd"],
            env=sandbox._sandbox_env(session["home"], session["engine"], {}),
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=30,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert "SOCKET_BRIDGE_PASS" in result.stdout
    assert GlobalMemory(wl.project_dir).read("plan") == []


@pytest.mark.parametrize("lifetime", ["complete", "crash", "migration"])
def test_native_codex_worker_uses_policy_and_external_gateway(tmp_path, monkeypatch, lifetime):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import shlex
    import threading
    from danus.execution import loop

    wl, _, _, config = _worker(tmp_path)
    protected = wl.project_dir / "progress" / "ledger.jsonl"
    before = protected.read_bytes()
    requests = []
    errors = []
    outputs = []
    from danus.execution import context as C
    migration_payload = {key: [] for key in C._MIGRATION_FIELDS}
    migration_payload.update(current_objective="bounded objective", next_action="resume checkpoint")
    marker = str(tmp_path / "detached-child")
    alive = wl.dir / "workspace" / "child-started"

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            tools = {t.get("name"): t for t in payload.get("tools", [])}
            try:
                if len(requests) == 1:
                    name = next(n for n in ("exec_command", "shell_command", "shell") if n in tools)
                    program = (
                        "import errno,os,socket,json; from pathlib import Path; "
                        f"p=Path({str(protected)!r}); "
                        "assert os.environ.get('DANUS_TEST_SECRET') is None; "
                        "\ntry: p.read_bytes()\nexcept OSError as e: assert e.errno in (1,2,13)\nelse: raise AssertionError('read escaped')"
                        "\ntry: p.write_text('FORGED')\nexcept OSError as e: assert e.errno in (1,2,13,30)\nelse: raise AssertionError('write escaped')"
                        f"\ntry: socket.create_connection(('127.0.0.1', {server.server_port}), timeout=1)\nexcept OSError as e: assert e.errno in (1,13)\nelse: raise AssertionError('network escaped')"
                        "\nprint('D2_NATIVE_DENIAL_OK')"
                    )
                    if lifetime == "migration":
                        program += ("\nassert Path('local_memory/agent_checkpoint.json').is_file()\n" +
                                    f"Path('local_memory/context_migration.json').write_text(json.dumps({migration_payload!r}))")
                    if lifetime == "crash":
                        program += ("\nimport subprocess,sys,time\n" +
                                    f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', {marker!r}], start_new_session=True)\n" +
                                    "Path('workspace/child-started').touch()\ntime.sleep(60)")
                    command = "/usr/bin/python3 -c " + shlex.quote(program)
                    args = ({"cmd": command, "yield_time_ms": 1000, "max_output_tokens": 1000} if name == "exec_command"
                            else {"command": command} if name == "shell_command"
                            else {"command": ["/bin/sh", "-c", command]})
                    item = {"id": "fc1", "type": "function_call", "call_id": "c1", "name": name, "arguments": json.dumps(args)}
                elif len(requests) == 2 and lifetime != "migration":
                    outputs.extend(item for item in payload.get("input", []) if item.get("type") == "function_call_output")
                    assert any(t.get("name") == "gm_add" for t in tools["mcp__danus"]["tools"])
                    name = "gm_add"
                    item = {"id": "fc2", "type": "function_call", "call_id": "c2", "name": name, "namespace": "mcp__danus",
                            "arguments": json.dumps({"kind": "plan", "claim": "native gateway works"})}
                else:
                    outputs.extend(item for item in payload.get("input", []) if item.get("type") == "function_call_output")
                    item = {"id": "msg1", "type": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": "DANUS_OUTCOME: COMPLETE", "annotations": []}]}
                response = {"id": "resp" + str(len(requests)), "status": "completed", "output": [item],
                            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
                events = [
                    {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": response},
                ]
                body = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                errors.append((repr(exc), list(tools)))
                self.send_error(500)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_session = W.worker_session
    monkeypatch.setenv("DANUS_CODEX_BIN", str(sandbox._resolved_codex()))
    monkeypatch.setenv("CODEX_BACKEND", "api")
    monkeypatch.setenv("DANUS_TEST_SECRET", "host-only")
    empty_home = tmp_path / "empty-codex-home"
    empty_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(empty_home))

    @contextmanager
    def local_model_session(*args, **kwargs):
        with real_session(*args, **kwargs) as session:
            path = session["home"] / "config.toml"
            path.write_text('model_provider = "local_test"\n' + path.read_text() +
                            '\n[model_providers.local_test]\nname = "local test"\nwire_api = "responses"\nrequires_openai_auth = false\n' +
                            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n')
            session["env"] = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CODEX_HOME": str(session["home"]), "LANG": "C.UTF-8"}
            yield session

    monkeypatch.setattr(W, "worker_session", local_model_session)
    log = wl.dir / "native.log"
    try:
        if lifetime == "migration":
            wl.logs.mkdir()
            prior = wl.logs / "round_1.log"
            prior.write_text("prior bounded execution")
            C.write_checkpoint(wl.dir, role="researcher", round_no=1, telemetry={},
                               assignment=ProgressLedger(wl.project_dir).current(wl.name))
            result = loop._run_context_migration(wl, config, "context budget", prior, hard_timeout=30)
            assert result == migration_payload, ((wl.logs / "migration_1.log").read_text(), errors)
            assert "D2_NATIVE_DENIAL_OK" in json.dumps(outputs)
            assert protected.read_bytes() == before
            return
        if lifetime == "crash":
            import signal
            import time

            def children():
                found = []
                for path in Path("/proc").glob("[0-9]*/cmdline"):
                    try:
                        args = path.read_bytes().split(b"\0")
                        if marker.encode() in args and args[0] == b"/usr/bin/python3":
                            found.append(int(path.parent.name))
                    except OSError:
                        pass
                return found

            import inspect
            import textwrap
            child_source = (
                "from contextlib import contextmanager\nfrom pathlib import Path\n"
                "from types import SimpleNamespace\n"
                "from danus.execution import worker as W, layout as L, loop\n"
                f"server=SimpleNamespace(server_port={server.server_port})\n"
                f"tmp_path=Path({str(tmp_path)!r})\nreal_session=W.worker_session\n"
                + textwrap.dedent(inspect.getsource(local_model_session)) +
                "\nW.worker_session=local_model_session\n"
                f"wl=L.WorkerLayout(Path({str(wl.dir)!r}))\n"
                f"raise SystemExit(loop.run_round(wl, {config!r}, 'Execute the bounded test.', Path({str(log)!r}), hard_timeout=30))\n"
            )
            controller = subprocess.Popen([sys.executable, "-c", child_source])
            try:
                deadline = time.monotonic() + 10
                while not alive.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert alive.exists(), log.read_text()
                assert children(), "detached child was never running"
                controller.kill()
                controller.wait(timeout=5)
                controller = None
                deadline = time.monotonic() + 5
                while children() and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert not children(), "detached tool survived worker SIGKILL"
            finally:
                if controller is not None:
                    controller.kill()
                    controller.wait(timeout=5)
                for pid in children():
                    os.kill(pid, signal.SIGKILL)
            return
        rc = loop.run_round(wl, config, "Execute the bounded test.", log, hard_timeout=30, context_budget=True)
        assert rc == 0, (log.read_text(), errors)
        assert not errors
        assert "D2_NATIVE_DENIAL_OK" in json.dumps(outputs), (outputs, log.read_text())
        assert protected.read_bytes() == before
        entries = GlobalMemory(wl.project_dir).read("plan")
        assert len(entries) == 1 and entries[0]["author"] == "r"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_worker_view_allows_science_notes_but_not_checkpoint_or_home(tmp_path):
    wl, _, _, config = _worker(tmp_path)
    wl.local_memory.mkdir(exist_ok=True)
    checkpoint = wl.local_memory / "runtime_checkpoint.json"
    checkpoint.write_text("HOST_CHECKPOINT")
    with W.worker_session(wl, config) as session:
        secret = session["home"] / "auth.json"
        secret.write_text("HOST_AUTH")
        body = f'''
Path("workspace/result.txt").write_text("science")
Path("local_memory/notes/result.txt").write_text("note")
for name in {[str(checkpoint), str(secret)]!r}:
    for action in (lambda p: p.read_bytes(), lambda p: p.write_text("FORGED")):
        try:
            action(Path(name))
        except OSError:
            pass
        else:
            raise AssertionError(name)
'''
        with _attack(session, body) as proc:
            out, err = proc.communicate(timeout=15)
            assert proc.returncode == 0, (out, err)
        assert (wl.dir / "workspace" / "result.txt").read_text() == "science"
        assert (wl.local_memory / "notes" / "result.txt").read_text() == "note"
        assert checkpoint.read_text() == "HOST_CHECKPOINT"


@pytest.mark.parametrize("source", ["private_workspace", "private_input", "credential_input", "launcher_workspace"])
def test_worker_rejects_scientific_paths_that_expose_its_trusted_host(tmp_path, monkeypatch, source):
    wl, _, _, role = _worker(tmp_path)
    import tempfile
    with tempfile.TemporaryDirectory(prefix="danus-boundary-", dir=Path.home() / ".cache") as home_text:
        home = Path(home_text)
        credentials = home / ".codex"
        credentials.mkdir()
        (credentials / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "TEST_ONLY_SECRET"}))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CODEX_HOME", str(credentials))
        metadata = {}
        target = credentials / "auth.json"
        if source == "private_workspace":
            metadata["workspace"] = str(home)
        elif source in {"private_input", "credential_input"}:
            metadata["readonly_inputs"] = [str(home if source == "private_input" else credentials)]
        else:
            launcher = tmp_path / "launcher"
            launcher.mkdir()
            target = launcher / "codex"
            target.write_text("TRUSTED_LAUNCHER")
            monkeypatch.setenv("DANUS_CODEX_BIN", str(target))
            metadata["workspace"] = str(launcher)
        (wl.project_dir / L.PROJECT_META_FILE).write_text(json.dumps(metadata))
        try:
            with W.worker_session(wl, role) as session:
                body = f"Path({str(target)!r}).write_text('FORGED')" if source == "launcher_workspace" else f"assert json.loads(Path({str(target)!r}).read_text())['OPENAI_API_KEY'] == 'TEST_ONLY_SECRET'"
                with _attack(session, body) as proc:
                    out, err = proc.communicate(timeout=15)
                    assert proc.returncode != 0, "sandbox exposed credentials or allowed trusted launcher replacement"
        except ValueError as exc:
            assert "trusted" in str(exc)
            return
        pytest.fail(f"unsafe scientific policy was accepted: {out!r}, {err!r}")


def test_worker_timeout_terminates_agent_while_control_lock_is_busy(tmp_path, monkeypatch):
    import time
    from danus.orchestration.cli import project_control_lock
    wl, _, _, role = _worker(tmp_path)
    pidfile = tmp_path / "agent.pid"
    fake = tmp_path / "fake-agent"
    fake.write_text("#!/usr/bin/env python3\nimport os,time\n" +
                    f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
    fake.chmod(0o755)
    monkeypatch.setenv("DANUS_CODEX_BIN", str(fake))
    code = ("from pathlib import Path; from danus.execution import loop, layout as L; " +
            f"raise SystemExit(loop.run_round(L.WorkerLayout(Path({str(wl.dir)!r})), {role!r}, 'bounded', Path({str(wl.dir / 'timeout.log')!r}), hard_timeout=1))")
    controller = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not pidfile.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pidfile.is_file()
        pid = int(pidfile.read_text())
        with project_control_lock(wl.project_dir):
            time.sleep(1.6)
            stat = Path(f"/proc/{pid}/stat")
            assert not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] == "Z", "control-lock wait bypassed the round timeout"
        assert controller.wait(timeout=5) == 124
    finally:
        sandbox._terminate_process_tree(controller)
