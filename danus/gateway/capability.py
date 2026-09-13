"""One server-bound Gateway capability per worker round."""

from contextlib import contextmanager
from pathlib import Path
import re
import secrets

from danus.core import ProgressLedger
from danus.core.handoff import HandoffRequests

EPOCH_FILE = ".gateway-epoch"


def _current(root, worker, role, assignment_id=None, *, refresh_stale=True):
    ledger = ProgressLedger(root)
    current = ledger.current(worker, refresh_stale=refresh_stale)
    if (
        not current
        or current["state"] not in {"ASSIGNED", "RUNNING"}
        or current["role"].lower() != role
        or (assignment_id is not None and current["assignment_id"] != assignment_id)
        or ledger._is_plan_stale(current)
    ):
        raise PermissionError("worker capability requires its current active assignment")
    wdir = root / "workers" / worker
    if (root / ".pause").exists() or (wdir / ".pause").exists():
        raise PermissionError("worker capability is paused")
    if ProgressLedger.task_sha256((wdir / "TASK.md").read_text()) != current["task_sha256"]:
        raise PermissionError("worker capability TASK does not match its assignment")
    if current.get("handoff_id"):
        handoff = HandoffRequests(ledger).get(current["handoff_id"])
        if (not handoff or handoff["status"] != "ASSIGNED"
                or handoff.get("assigned_assignment_id") != current["assignment_id"]):
            raise PermissionError("worker capability handoff is no longer active")
    return current


def issue(root: Path, worker: str, role: str) -> dict:
    from danus.orchestration.cli import project_control_lock

    root = Path(root).resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", worker):
        raise ValueError("invalid capability worker")
    if role not in {"worker", "researcher", "reviewer", "experimenter"}:
        raise ValueError("invalid capability role")
    wdir = root / "workers" / worker
    if wdir.resolve() != wdir:
        raise ValueError("capability worker directory cannot be a symlink")
    with project_control_lock(root), ProgressLedger(root).assignment_lock():
        current = _current(root, worker, role)
        binding = {"project_dir": str(root), "worker": worker, "role": role,
                   "assignment_id": current["assignment_id"], "epoch": secrets.token_hex(32)}
        ProgressLedger._write_assignment_file(wdir / EPOCH_FILE, binding["epoch"])
        return binding


def validate(binding: dict, *, refresh_stale=True) -> None:
    """Authority checks hold the existing project/control and assignment locks.

    A read-only monitor may use refresh_stale=False without locks: it can stop a
    process, but grants no tool/write authority. It must not block its deadline.
    """
    try:
        if binding.get("revoked"):
            raise PermissionError("worker capability was revoked")
        root = Path(binding["project_dir"])
        epoch = root / "workers" / binding["worker"] / EPOCH_FILE
        if epoch.read_text() != binding["epoch"]:
            raise PermissionError("worker capability epoch is stale")
        _current(root, binding["worker"], binding["role"], binding["assignment_id"], refresh_stale=refresh_stale)
    except (KeyError, OSError, ValueError):
        binding["revoked"] = True
        raise


@contextmanager
def scope(binding: dict):
    from danus.orchestration.cli import project_control_lock

    root = Path(binding["project_dir"])
    with project_control_lock(root), ProgressLedger(root).assignment_lock():
        validate(binding)
        yield


def revoke(binding: dict) -> None:
    from danus.orchestration.cli import project_control_lock

    root = Path(binding["project_dir"])
    with project_control_lock(root), ProgressLedger(root).assignment_lock():
        path = root / "workers" / binding["worker"] / EPOCH_FILE
        try:
            if path.read_text() == binding["epoch"]:
                path.unlink()
        except FileNotFoundError:
            pass
    binding["revoked"] = True


def bind_stdio_peer() -> None:
    """Close the Gateway when the first actual FD sender dies or changes.

    SO_PASSCRED supplies a kernel PID, not a worker-provided identity. pidfd
    detects proxy death even when the agent harness still holds an FD copy.
    """
    import os
    import selectors
    import socket
    import struct
    import threading

    channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0, fileno=os.dup(0))
    incoming, to_server = os.pipe()
    from_server, outgoing = os.pipe()
    os.dup2(incoming, 0)
    os.dup2(outgoing, 1)
    os.close(incoming)
    os.close(outgoing)

    def relay():
        selector = selectors.DefaultSelector()
        selector.register(channel, selectors.EVENT_READ, "request")
        selector.register(from_server, selectors.EVENT_READ, "response")
        peer = None
        try:
            while True:
                for key, _ in selector.select():
                    if key.data == "dead":
                        os._exit(1)
                    if key.data == "response":
                        data = os.read(from_server, 65536)
                        if not data:
                            os._exit(1)
                        channel.sendall(data)
                        continue
                    data, ancillary, flags, _ = channel.recvmsg(65536, socket.CMSG_SPACE(12))
                    credentials = [value for level, kind, value in ancillary
                                   if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS]
                    if not data or flags & socket.MSG_CTRUNC or len(credentials) != 1:
                        os._exit(1)
                    pid, uid, _ = struct.unpack("3i", credentials[0])
                    if uid != os.getuid():
                        os._exit(1)
                    if peer is None:
                        peer = pid
                        selector.register(os.pidfd_open(pid), selectors.EVENT_READ, "dead")
                    if pid != peer:
                        os._exit(1)
                    while data:
                        data = data[os.write(to_server, data):]
        except (OSError, ValueError):
            os._exit(1)

    threading.Thread(target=relay, daemon=True).start()
