"""Small wrapper around the official Linux Codex sandbox."""

from __future__ import annotations

import ctypes
from contextlib import nullcontext
import time
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_OUTPUT_LIMIT_RC = 125

_ENV_ALLOWLIST = frozenset({"HOME", "LANG", "LC_ALL", "TZ"})


class SandboxInterrupted(RuntimeError):
    """The host monitor did not establish a completed sandbox command."""


def _toml_key(path: Path) -> str:
    return json.dumps(str(path))


def _policy_path(raw: Path) -> Path:
    path = Path(raw)
    current = path
    while True:
        if current.is_symlink():
            raise ValueError(f"sandbox policy path cannot contain a symlink: {path}")
        parent = current.parent
        if parent == current:
            break
        current = parent
    return path.resolve()


def _resolved_codex() -> Path:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("official Codex Linux sandbox is unavailable")
    raw = os.environ.get("DANUS_SANDBOX_CODEX_BIN") or shutil.which("codex")
    if not raw:
        raise FileNotFoundError("official Codex sandbox executable not found")
    candidate = Path(raw).expanduser()
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise FileNotFoundError(f"Codex executable not found: {candidate}")
    return candidate


def _write_profile(
    config_home: Path,
    *,
    codex_bin: Path,
    readable_paths: Sequence[Path],
    writable_paths: Sequence[Path],
    network: bool,
    profile: str = "danus-reducer",
    workspace_access: str = "write",
) -> None:
    if network:
        raise ValueError("sandbox network access must remain disabled")
    filesystem = [
        '":root" = "deny"',
        '":minimal" = "read"',
        '":tmpdir" = "deny"',
        '":slash_tmp" = "deny"',
        f"{_toml_key(codex_bin)} = \"read\"",
    ]
    seen = {codex_bin}
    for path in readable_paths:
        if path in seen:
            continue
        filesystem.append(f"{_toml_key(path)} = \"read\"")
        seen.add(path)
    for path in writable_paths:
        if path in seen:
            continue
        filesystem.append(f"{_toml_key(path)} = \"write\"")
        seen.add(path)
    config = "\n".join(
        [
            f'default_permissions = "{profile}"',
            "",
            f"[permissions.{profile}]",
            'description = "Danus-R isolated execution"',
            'extends = ":workspace"',
            "",
            f"[permissions.{profile}.filesystem]",
            *filesystem,
            "",
            f'[permissions.{profile}.filesystem.":workspace_roots"]',
            f'"." = "{workspace_access}"',
            "",
            f"[permissions.{profile}.network]",
            "enabled = false",
            "",
        ]
    )
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.toml").write_text(config, encoding="utf-8")


def _sandbox_env(
    config_home: Path,
    codex_bin: Path,
    env: Mapping[str, str] | None,
) -> dict[str, str]:
    source = env or {}
    clean = {key: str(value) for key, value in source.items() if key in _ENV_ALLOWLIST}
    clean.setdefault("HOME", str(Path.home()))
    clean.setdefault("LANG", "C.UTF-8")
    clean["CODEX_HOME"] = str(config_home)
    clean["PATH"] = os.pathsep.join(
        [str(codex_bin.parent), "/usr/local/bin", "/usr/bin", "/bin"]
    )
    return clean


def _sandbox_home_root() -> Path:
    root = Path.home() / ".cache" / "danus-sandbox"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _send_tree_signal(proc: subprocess.Popen[str], sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
        return
    except (AttributeError, OSError):
        pass
    try:
        if sig == signal.SIGTERM:
            proc.terminate()
        else:
            proc.kill()
    except (AttributeError, OSError):
        pass


def _terminate_process_tree(proc: subprocess.Popen[str], grace: float = 0.5) -> None:
    _send_tree_signal(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        _send_tree_signal(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def run_sandboxed(
    command: Sequence[str],
    *,
    cwd: Path,
    readable_paths: Sequence[Path] = (),
    writable_paths: Sequence[Path] = (),
    network: bool = False,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    workspace_access: str = "write",
    pass_fds: Sequence[int] = (),
    authorize=None,
) -> subprocess.CompletedProcess[str]:
    """Run one command through the official Codex Linux sandbox.

    The caller supplies the only paths that may be visible. Missing Codex,
    unsupported hosts, launch failures, and timeouts are errors; this function
    never falls back to an ordinary subprocess.
    """
    if not command or not all(isinstance(arg, str) and arg for arg in command):
        raise ValueError("sandbox command must be a non-empty string sequence")
    root = _policy_path(Path(cwd))
    if not root.is_dir():
        raise ValueError(f"sandbox cwd is not a directory: {root}")
    if timeout is not None and timeout <= 0:
        raise ValueError("sandbox timeout must be positive")

    codex_bin = _resolved_codex()
    readable = tuple(_policy_path(Path(path)) for path in readable_paths)
    writable = tuple(_policy_path(Path(path)) for path in writable_paths)
    if workspace_access not in {"read", "write"}:
        raise ValueError("invalid sandbox workspace access")
    if workspace_access == "write" and root not in writable:
        writable = (root, *writable)
    if any(not path.exists() for path in (*readable, *writable)):
        raise ValueError("sandbox policy contains a missing path")

    config_home = Path(
        tempfile.mkdtemp(prefix="codex-home-", dir=str(_sandbox_home_root()))
    )
    completion = None
    try:
        completion = tempfile.TemporaryFile()
        _write_profile(
            config_home,
            codex_bin=codex_bin,
            readable_paths=readable,
            writable_paths=writable,
            network=network,
            workspace_access=workspace_access,
        )
        cmd = [
            str(codex_bin),
            "sandbox",
            "-P",
            "danus-reducer",
            "-C",
            str(root),
            *command,
        ]
        guard = authorize or nullcontext
        owner_fd = os.pidfd_open(os.getpid())
        try:
            with guard():
                proc = subprocess.Popen(
                    [sys.executable, "-I", str(Path(__file__).resolve()), str(owner_fd),
                     str(completion.fileno()), ",".join(map(str, pass_fds)), *cmd],
                    cwd=str(root),
                    env=_sandbox_env(config_home, codex_bin, env),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                    pass_fds=(owner_fd, completion.fileno(), *pass_fds),
                )
        finally:
            os.close(owner_fd)
        started = time.monotonic()
        try:
            while True:
                interval = 0.5
                if timeout is not None:
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(cmd, timeout)
                    interval = min(interval, remaining)
                try:
                    stdout, stderr = proc.communicate(timeout=interval)
                    if proc.returncode != 0:
                        _send_tree_signal(proc, signal.SIGKILL)
                    with guard():
                        break
                except subprocess.TimeoutExpired:
                    if proc.poll() is not None:
                        _send_tree_signal(proc, signal.SIGKILL)
                    with guard():
                        pass
                    if timeout is not None and time.monotonic() - started >= timeout:
                        raise
            if proc.returncode == _OUTPUT_LIMIT_RC:
                raise SandboxInterrupted("sandbox output limit exceeded")
            if proc.returncode != 0:
                raise SandboxInterrupted(f"sandbox monitor interrupted with rc={proc.returncode}")
            completion.seek(0)
            try:
                returncode = int(completion.read(32))
            except ValueError as exc:
                _send_tree_signal(proc, signal.SIGKILL)
                raise SandboxInterrupted("sandbox monitor exited without a command result") from exc
            if returncode < 0:
                _send_tree_signal(proc, signal.SIGKILL)
                raise SandboxInterrupted(f"sandbox engine interrupted with rc={returncode}")
            if returncode != 0:
                _send_tree_signal(proc, signal.SIGKILL)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_tree(proc)
            try:
                proc.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                _send_tree_signal(proc, signal.SIGKILL)
                proc.kill()
                proc.wait()
            raise TimeoutError(f"sandbox command timed out after {exc.timeout}s") from exc
        except BaseException:
            _terminate_process_tree(proc)
            raise
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    finally:
        if completion is not None:
            completion.close()
        shutil.rmtree(config_home, ignore_errors=True)


_LIBC = ctypes.CDLL(None, use_errno=True) if sys.platform.startswith("linux") else None


def _arm_parent_death(expected_parent_pid: int) -> None:
    """Linux: a scientific model/migration process must not outlive its worker."""
    if _LIBC is None:
        return
    if _LIBC.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        os._exit(126)
    if os.getppid() != expected_parent_pid:
        os.kill(os.getpid(), signal.SIGKILL)


def _cleanup_monitor_home() -> None:
    home = Path(os.environ.get("CODEX_HOME", ""))
    root = _sandbox_home_root().resolve()
    if (home.name.startswith("codex-home-") and not home.is_symlink()
            and home.parent.resolve() == root):
        shutil.rmtree(home, ignore_errors=True)


def _watch_command() -> int:
    """Own the sandbox process group before launching the official engine.

    Codex can fork launch helpers; PR_SET_PDEATHSIG on its first PID alone does
    not cover them. The inherited pidfd identifies the actual calling process.
    """
    import select

    owner = int(sys.argv[1])
    completion = int(sys.argv[2])
    inherited = tuple(int(fd) for fd in sys.argv[3].split(",") if fd)
    if select.select([owner], [], [], 0)[0]:
        _cleanup_monitor_home()
        return 126
    # Only the trusted monitor can attest completion; neither private fd enters
    # the sandbox command.
    import selectors

    child = subprocess.Popen(sys.argv[4:], pass_fds=inherited,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child_fd = None
    total = 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(owner, selectors.EVENT_READ, "owner")
            for stream, destination in ((child.stdout, 1), (child.stderr, 2)):
                selector.register(stream, selectors.EVENT_READ, destination)
            try:
                child_fd = os.pidfd_open(child.pid)
            except ProcessLookupError:
                pass
            else:
                selector.register(child_fd, selectors.EVENT_READ, "child")
            while len(selector.get_map()) > 1:
                for key, _ in selector.select():
                    if key.data == "owner":
                        _cleanup_monitor_home()
                        os.killpg(os.getpgrp(), signal.SIGKILL)
                    if key.data == "child":
                        selector.unregister(child_fd)
                        continue
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > _MAX_OUTPUT_BYTES:
                        child.kill()
                        child.wait()
                        return _OUTPUT_LIMIT_RC
                    # The parent drains both pipes; only bounded bytes reach it.
                    while chunk:
                        chunk = chunk[os.write(key.data, chunk):]
        returncode = child.wait()
    except BrokenPipeError:
        # The caller can die while the monitor is blocked forwarding output.
        try:
            _cleanup_monitor_home()
        finally:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        raise
    finally:
        if child_fd is not None:
            os.close(child_fd)
        child.stdout.close()
        child.stderr.close()
    os.write(completion, str(returncode).encode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_watch_command())
