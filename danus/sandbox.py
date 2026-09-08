"""Small wrapper around the official Linux Codex sandbox."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

_ENV_ALLOWLIST = frozenset({"HOME", "LANG", "LC_ALL", "TZ"})


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
    profile = "danus-reducer"
    config = "\n".join(
        [
            f'default_permissions = "{profile}"',
            "",
            f"[permissions.{profile}]",
            'description = "Danus-R reducer isolation"',
            'extends = ":workspace"',
            "",
            f"[permissions.{profile}.filesystem]",
            *filesystem,
            "",
            f'[permissions.{profile}.filesystem.":workspace_roots"]',
            '"." = "write"',
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
        os.killpg(os.getpgid(proc.pid), sig)
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
    if root not in writable:
        writable = (root, *writable)
    if any(not path.exists() for path in (*readable, *writable)):
        raise ValueError("sandbox policy contains a missing path")

    config_home = Path(
        tempfile.mkdtemp(prefix="codex-home-", dir=str(_sandbox_home_root()))
    )
    try:
        _write_profile(
            config_home,
            codex_bin=codex_bin,
            readable_paths=readable,
            writable_paths=writable,
            network=network,
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
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(root),
                env=_sandbox_env(config_home, codex_bin, env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError:
            raise
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_tree(proc)
            try:
                proc.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                _send_tree_signal(proc, signal.SIGKILL)
                proc.kill()
                proc.wait()
            raise TimeoutError(f"sandbox command timed out after {exc.timeout}s") from exc
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    finally:
        shutil.rmtree(config_home, ignore_errors=True)
