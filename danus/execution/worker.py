"""Native Codex tool isolation and an external, per-round Gateway."""

from contextlib import contextmanager
import json
import os
import selectors
import struct
import threading
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile

from danus import codex, sandbox
from danus.gateway import capability
from . import layout as L
from . import context as C


def _overlaps(path: Path, other: Path) -> bool:
    return path == other or path in other.parents or other in path.parents


def _outside_control(path: Path, control: Path) -> Path:
    path = sandbox._policy_path(path)
    if _overlaps(path, control):
        raise ValueError("worker input/runtime must be outside the control project")
    return path


def _relay_gateway_socket(listener: socket.socket, gateway_fd: socket.socket,
                          stop: threading.Event, path: Path) -> None:
    """One-shot host-side bridge for a fixed, non-model-controlled MCP proxy.

    The private socket is outside the scientific sandbox; restricted Codex
    forbids socket connect and sendto, so an in-sandbox proxy cannot use it.
    The host proxy only forwards MCP bytes to the role-bound Gateway.
    Closing either endpoint terminates the round channel.
    """
    peer = None
    try:
        listener.settimeout(0.25)
        while not stop.is_set():
            try:
                peer, _ = listener.accept()
                break
            except socket.timeout:
                continue
        if peer is None:
            return
        _, uid, _ = struct.unpack("3i", peer.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if uid != os.getuid():
            return
        path.unlink(missing_ok=True)  # one connection only; no reconnect
        with selectors.DefaultSelector() as selector:
            selector.register(peer, selectors.EVENT_READ, gateway_fd)
            selector.register(gateway_fd, selectors.EVENT_READ, peer)
            while not stop.is_set():
                for key, _ in selector.select(timeout=0.25):
                    data = key.fileobj.recv(65536)
                    if not data:
                        return
                    key.data.sendall(data)
    except OSError:
        pass  # Gateway or MCP peer disconnected; fail closed below
    finally:
        if peer is not None:
            peer.close()
            try:
                gateway_fd.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass



def _inject_plan_read_inputs(view: Path, wl: L.WorkerLayout, role_name: str) -> None:
    """Mirror exact, validated, assignment-scoped planning inputs into read-only view.

    The authoritative control directory itself stays hidden from Codex tools.
    """
    if role_name not in L.SCIENTIFIC_ROLES:
        return
    from danus.core import ProgressLedger, ResearchPlanGraph
    from danus.core.handoff import HandoffRequests
    from danus.orchestration import planning

    control = wl.project_dir.resolve()
    graph = ResearchPlanGraph(control)
    if graph.path.is_symlink():
        raise ValueError("research plan current file must not be a symlink")
    if not graph.path.is_file():
        return

    snapshot = graph.snapshot()
    (view / "RESEARCH_PLAN.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    ledger = ProgressLedger(control)
    assignment = ledger.current(wl.name, refresh_stale=False)
    if not assignment or str(assignment.get("role") or "").lower() != role_name:
        return
    if int(assignment.get("plan_version") or 0) != snapshot["plan_version"]:
        return

    task = wl.task.read_text(encoding="utf-8")
    if role_name == "researcher" and task.startswith("# Repair Autonomous Plan Proposal\n"):
        path = planning._repair_error_path(control)
        if path.is_symlink():
            raise ValueError("planning repair error must not be a symlink")
        if not path.is_file():
            raise ValueError("planning repair error is missing")
        report = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("planning repair error must be an object")
        (view / "PLAN_REPAIR_ERROR.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    handoff_id = str(assignment.get("handoff_id") or "")
    if not handoff_id:
        return
    handoff = HandoffRequests(ledger).get(handoff_id)
    if (
        not handoff
        or handoff.get("status") != "ASSIGNED"
        or handoff.get("assigned_assignment_id") != assignment.get("assignment_id")
        or handoff.get("subject_type") != "PLAN_CHANGE"
        or handoff.get("kind") not in {"REVIEW", "REVISION", "RETHINK"}
    ):
        return

    change_id = str(handoff.get("subject_id") or "")
    change = graph.get_change(change_id)
    if int(change.get("base_plan_version") or 0) != snapshot["plan_version"]:
        raise ValueError("assigned Plan Change is bound to a different Plan version")
    proposal_id = planning._proposal_id_from_change(change)
    if not proposal_id:
        raise ValueError("assigned Plan Change lacks immutable proposal identity")
    record_path = planning._proposal_record_path(control, proposal_id)
    if record_path.is_symlink():
        raise ValueError("immutable Plan proposal must not be a symlink")
    record = planning._load_proposal_record(control, proposal_id)
    proposal = record.get("proposal") or {}
    if (
        record.get("change_id") != change_id
        or record.get("proposer") != change.get("proposer")
        or proposal.get("base_plan_version") != snapshot["plan_version"]
        or proposal.get("operations") != change.get("operations")
        or bool(proposal.get("post_hoc")) != bool(change.get("post_hoc"))
        or list(proposal.get("evidence_refs") or []) != list(change.get("evidence_refs") or [])
    ):
        raise ValueError("assigned Plan Change and immutable proposal differ")
    (view / "PLAN_PROPOSAL.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


@contextmanager
def worker_session(wl: L.WorkerLayout, role: dict):
    """The agent harness is trusted; tools and its MCP proxy share one deny policy."""
    if codex.backend() == "dsh":
        raise RuntimeError("DSH worker tools have not integrated the official sandbox capability transport")
    engine = sandbox._resolved_codex()
    role_name = str(role.get("ROLE") or "worker").lower()
    binding = capability.issue(wl.project_dir, wl.name, role_name)
    base = Path.home() / ".cache" / "danus-worker"
    base.mkdir(parents=True, exist_ok=True)
    server_end = client_end = gateway = None
    try:
        with tempfile.TemporaryDirectory(prefix="round-", dir=base) as directory:
            root = Path(directory)
            view = root / "view"
            view.mkdir()
            home = root / "codex-home"
            control = wl.project_dir.resolve()
            workspace = L.scientific_workspace(control)
            if workspace == control:
                workspace = wl.dir / "workspace"
                workspace.mkdir(exist_ok=True)
                workspace = sandbox._policy_path(workspace)
            else:
                workspace = _outside_control(workspace, control)
            if not workspace.is_dir():
                raise ValueError("worker workspace is missing")
            notes = wl.local_memory / "notes"
            notes.mkdir(parents=True, exist_ok=True)
            notes = sandbox._policy_path(notes)
            inputs = [_outside_control(path, control) for path in L.readonly_inputs(control)]
            agent_bin = codex.resolve_bin()
            original_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
            private = [root, original_home.absolute(), original_home.resolve()]
            if any(_overlaps(path, protected) for path in [workspace, *inputs] for protected in private):
                raise ValueError("scientific paths overlap trusted worker state or credentials")
            trusted = [engine, Path(agent_bin).absolute(), Path(agent_bin).resolve(),
                       Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
                       Path(__file__).resolve().parents[1]]
            if any(_overlaps(path, protected) for path in [workspace, notes] for protected in trusted):
                raise ValueError("scientific writes overlap trusted runtime or launcher")
            (view / "workspace").symlink_to(workspace, target_is_directory=True)
            memory = view / "local_memory"
            memory.mkdir()
            (memory / "notes").symlink_to(notes, target_is_directory=True)
            migration = memory / "migration"
            migration.mkdir()
            C.migration_path(view).symlink_to(migration / "result.json")
            with capability.scope(binding):
                (view / "TASK.md").write_text(wl.task.read_text())
                (view / "AGENTS.md").write_text(L.harness_contract(role_name).read_text())
                # The autonomous bootstrap needs the research brief, but the
                # project control root must remain invisible to the sandbox.
                # Mirror only this public research input into the read-only
                # worker view; never grant access to its source directory.
                brief = control / "RESEARCH_BRIEF.md"
                if brief.is_symlink():
                    raise ValueError("research brief must not be a symlink")
                if brief.is_file():
                    (view / "RESEARCH_BRIEF.md").write_bytes(brief.read_bytes())
                _inject_plan_read_inputs(view, wl, role_name)
                checkpoint = C.checkpoint_for_prompt(wl.dir, role_name)
                if checkpoint:
                    (memory / C.CHECKPOINT_NAME).write_text(json.dumps(checkpoint))
            skills = L.harness_skills_dir(role_name)
            if skills is not None:
                shutil.copytree(skills, view / ".agents" / "skills")
            proxy = view / "gateway_proxy.py"
            shutil.copyfile(Path(__file__).resolve().parents[1] / "gateway" / "proxy.py", proxy)
            readable = [view, _outside_control(proxy, control),
                        _outside_control(Path(sys.prefix), control),
                        _outside_control(Path(sys.base_prefix), control)]
            readable += inputs
            sandbox._write_profile(
                home, codex_bin=engine, readable_paths=readable,
                writable_paths=[workspace, notes, migration], network=False,
                profile="danus-worker", workspace_access="read",
            )
            server_end, client_end = socket.socketpair()
            server_end.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            fd = client_end.fileno()
            # Codex 0.160 closes extra FDs when spawning MCP children.
            # Its restricted sandbox also denies connect()/sendto(), so only
            # the fixed transport-only MCP proxy executes outside the sandbox.
            # The private socket cannot be accessed by model-side subprocesses;
            # actual model tools and experiment code stay in Codex's sandbox.
            # Keep the original direct FD for D2/D3 sandbox attack probes.
            socket_path = root / "gateway.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(socket_path))
                socket_path.chmod(0o600)
                listener.listen(1)
            except BaseException:
                listener.close()
                raise
            bridge_stop = threading.Event()
            bridge_thread = threading.Thread(
                target=_relay_gateway_socket,
                args=(listener, client_end, bridge_stop, socket_path), daemon=True)
            bridge_thread.start()
            # Preserve the direct-FD proxy invocation exposed to existing D2/D3
            # isolation probes. Only Codex's MCP child uses the host-only bridge.
            proxy_cmd = [sys.executable, "-I", str(proxy), str(fd)]
            mcp_cmd = [sys.executable, "-I", str(proxy), "--socket", str(socket_path)]
            config = home / "config.toml"
            with config.open("a") as stream:
                stream.write('\n[shell_environment_policy]\ninherit = "none"\n')
                stream.write('set = { PATH = "/usr/local/bin:/usr/bin:/bin", LANG = "C.UTF-8" }\n')
                stream.write('\n[mcp_servers.danus]\n')
                stream.write('command = ' + json.dumps(mcp_cmd[0]) + '\n')
                stream.write('args = ' + json.dumps(mcp_cmd[1:]) + '\n')
                stream.write('tool_timeout_sec = 3600\nrequired = true\ndefault_tools_approval_mode = "approve"\n')
                stream.write('\n[mcp_servers.danus.env]\nCODEX_HOME = ' + json.dumps(str(home)) + '\n')
            auth = original_home / "auth.json"
            if auth.is_file():
                shutil.copyfile(auth, home / "auth.json")
                (home / "auth.json").chmod(0o600)
            env = codex.subprocess_env(agent_bin)
            env["CODEX_HOME"] = str(home)
            gateway_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
                           "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                           "DANUS_SANDBOX_CODEX_BIN": str(engine)}
            for key in ("DANUS_VERIFY_URL", "DANUS_VERIFY_TIMEOUT"):
                if key in os.environ:
                    gateway_env[key] = os.environ[key]
            parent_pid = os.getpid()
            gateway = subprocess.Popen(
                [sys.executable, "-m", "danus.gateway", "--worker-binding", json.dumps(binding)],
                stdin=server_end, stdout=server_end, stderr=subprocess.DEVNULL,
                env=gateway_env, cwd=str(root), start_new_session=True,
                preexec_fn=lambda: sandbox._arm_parent_death(parent_pid),
            )
            server_end.close()
            server_end = None
            try:
                yield {"cwd": view, "env": env, "fd": fd, "gateway": gateway,
                       "binding": binding, "codex_bin": agent_bin, "engine": engine,
                       "proxy_command": proxy_cmd, "home": home,
                       "gateway_socket": socket_path}
            finally:
                bridge_stop.set()
                listener.close()
                bridge_thread.join(timeout=1)
    finally:
        if client_end is not None:
            try:
                client_end.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client_end.close()
        if server_end is not None:
            server_end.close()
        if gateway is not None:
            sandbox._terminate_process_tree(gateway)
        capability.revoke(binding)


def check_session(session: dict) -> None:
    if session["gateway"].poll() is not None:
        raise PermissionError("external Gateway disconnected")
    capability.validate(session["binding"], refresh_stale=False)


def command(session: dict, role: dict, prompt: str, *, json_output=False) -> list[str]:
    tail = ["-C", str(session["cwd"]), "--skip-git-repo-check", "--ignore-rules", "--ephemeral"]
    if json_output:
        tail.append("--json")
    prompt += "\n\nUse workspace/ for scientific files and local_memory/notes/ for your notes. Runtime checkpoint files are read-only."
    return codex.exec_cmd(session["codex_bin"], role["MODEL"], role["REASONING_EFFORT"],
                          *tail, prompt, permission_profile="danus-worker")
