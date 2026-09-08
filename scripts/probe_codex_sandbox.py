#!/usr/bin/env python3
"""S5-D0 host probe for the official Linux Codex sandbox.

This does not import Danus or modify a Danus project. It creates a disposable
fake workspace/control tree under ~/.cache, runs ordinary Python through
``codex sandbox``, and checks the isolation properties Danus-R needs before any
production call site is migrated.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from pathlib import Path


PROFILE = "danus-probe"
MARKER = "DANUS_SANDBOX_PROBE="
EXPECTED_INNER_RESULTS = {
    "workspace_rw",
    "readonly_read",
    "readonly_write_denied",
    "plan_read_denied",
    "plan_write_denied",
    "plan_delete_denied",
    "progress_read_denied",
    "progress_write_denied",
    "progress_delete_denied",
    "fact_graph_read_denied",
    "fact_graph_write_denied",
    "fact_graph_delete_denied",
    "pause_read_denied",
    "pause_write_denied",
    "pause_delete_denied",
    "other_worker_read_denied",
    "other_worker_write_denied",
    "other_worker_delete_denied",
    "tmp_write_denied",
    "absolute_escape_denied",
    "dotdot_escape_denied",
    "existing_symlink_escape_denied",
    "new_symlink_escape_denied",
    "nested_process_inherits_denial",
    "parent_secret_absent",
    "network_loopback_denied",
}


def _toml_key(path: Path) -> str:
    return json.dumps(str(path))


def _write_profile(config_home: Path, codex_real: Path, readonly: Path) -> None:
    config_home.mkdir(parents=True, exist_ok=True)
    config = f'''default_permissions = "{PROFILE}"

[permissions.{PROFILE}]
description = "Danus-R S5-D0 acceptance probe"
extends = ":workspace"

[permissions.{PROFILE}.filesystem]
":root" = "deny"
":minimal" = "read"
":tmpdir" = "deny"
":slash_tmp" = "deny"
{_toml_key(codex_real)} = "read"
{_toml_key(readonly)} = "read"

[permissions.{PROFILE}.filesystem.":workspace_roots"]
"." = "write"

[permissions.{PROFILE}.network]
enabled = false
'''
    (config_home / "config.toml").write_text(config, encoding="utf-8")


def _inner_program(paths: dict[str, str], port: int) -> str:
    payload = json.dumps(paths)
    return f'''from __future__ import annotations
import errno
import json
import os
import socket
import subprocess
from pathlib import Path

P = {{k: Path(v) for k, v in json.loads({payload!r}).items()}}
results = {{}}
DENIAL_ERRNOS = {{errno.ENOENT, errno.EACCES, errno.EPERM, errno.EROFS}}
LINK_CREATION_DENIAL_ERRNOS = {{errno.EACCES, errno.EPERM, errno.EROFS}}
NETWORK_DENIAL_ERRNOS = {{
    errno.EACCES,
    errno.EPERM,
    errno.ECONNREFUSED,
    errno.ENETUNREACH,
    errno.EHOSTUNREACH,
}}

def record(name, passed, detail=""):
    results[name] = {{"pass": bool(passed), "detail": str(detail)}}

def expect_denied(name, fn):
    try:
        fn()
    except OSError as exc:
        allowed = exc.errno in DENIAL_ERRNOS
        record(name, allowed, f"{{type(exc).__name__}} errno={{exc.errno}}: {{exc}}")
    else:
        record(name, False, "operation unexpectedly succeeded")

try:
    P["workspace_file"].write_text("changed", encoding="utf-8")
    record("workspace_rw", P["workspace_file"].read_text(encoding="utf-8") == "changed")
except OSError as exc:
    record("workspace_rw", False, exc)

try:
    record("readonly_read", P["readonly_file"].read_text(encoding="utf-8") == "READONLY")
except OSError as exc:
    record("readonly_read", False, exc)
expect_denied("readonly_write_denied", lambda: P["readonly_file"].write_text("BAD", encoding="utf-8"))

for prefix, key in [
    ("plan", "plan"),
    ("progress", "progress"),
    ("fact_graph", "fact"),
    ("pause", "pause"),
    ("other_worker", "other_worker"),
]:
    expect_denied(prefix + "_read_denied", lambda key=key: P[key].read_text(encoding="utf-8"))
    expect_denied(prefix + "_write_denied", lambda key=key: P[key].write_text("FORGED", encoding="utf-8"))
    expect_denied(prefix + "_delete_denied", lambda key=key: P[key].unlink())

expect_denied("tmp_write_denied", lambda: Path("/tmp/danus-s5d0-probe").write_text("BAD", encoding="utf-8"))
expect_denied("absolute_escape_denied", lambda: Path(str(P["progress"])).read_text(encoding="utf-8"))
expect_denied("dotdot_escape_denied", lambda: P["dotdot_progress"].read_text(encoding="utf-8"))
expect_denied("existing_symlink_escape_denied", lambda: P["existing_link"].read_text(encoding="utf-8"))

try:
    P["late_link"].symlink_to(P["progress"])
except OSError as exc:
    allowed = exc.errno in LINK_CREATION_DENIAL_ERRNOS
    record(
        "new_symlink_escape_denied",
        allowed,
        f"link creation failed with {{type(exc).__name__}} errno={{exc.errno}}: {{exc}}",
    )
else:
    expect_denied("new_symlink_escape_denied", lambda: P["late_link"].read_text(encoding="utf-8"))

nested = subprocess.run(
    ["/bin/bash", "-lc", 'cat "$1" >/dev/null', "probe", str(P["progress"])],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
nested_ok = nested.returncode == 1 and str(P["progress"]) in nested.stderr
record(
    "nested_process_inherits_denial",
    nested_ok,
    f"returncode={{nested.returncode}} stderr={{nested.stderr.strip()}}",
)
record("parent_secret_absent", os.environ.get("DANUS_TEST_SECRET") is None, os.environ.get("DANUS_TEST_SECRET", ""))

try:
    sock = socket.create_connection(("127.0.0.1", {port}), timeout=1.0)
except OSError as exc:
    allowed = exc.errno in NETWORK_DENIAL_ERRNOS
    record(
        "network_loopback_denied",
        allowed,
        f"{{type(exc).__name__}} errno={{exc.errno}}: {{exc}}",
    )
else:
    sock.close()
    record("network_loopback_denied", False, "connected to host listener")

print({MARKER!r} + json.dumps(results, sort_keys=True))
'''


def _listener() -> tuple[socket.socket, int, threading.Event, threading.Thread]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(3.0)
    connected = threading.Event()

    def accept_once() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        connected.set()
        conn.close()

    thread = threading.Thread(target=accept_once, daemon=True)
    thread.start()
    return server, server.getsockname()[1], connected, thread


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", help="Codex launcher path; defaults to PATH lookup")
    args = parser.parse_args()

    launcher_text = args.codex or shutil.which("codex")
    if not launcher_text:
        print("FAIL: codex not found; install the official Linux Codex CLI first")
        return 2
    launcher = Path(launcher_text).expanduser().absolute()
    codex_real = launcher.resolve()
    if not codex_real.is_file():
        print(f"FAIL: resolved Codex executable is missing: {codex_real}")
        return 2

    cache = Path.home() / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="danus-s5d0-", dir=cache))
    server = None
    try:
        workspace = root / "workspace"
        readonly = root / "readonly"
        control = root / "control"
        config_home = root / "codex-home"
        for path in (
            workspace,
            readonly,
            control / "plan",
            control / "progress",
            control / "fact_graph" / "facts",
            control / "workers" / "other" / "local_memory",
        ):
            path.mkdir(parents=True, exist_ok=True)

        workspace_file = workspace / "file.txt"
        readonly_file = readonly / "input.txt"
        plan = control / "plan" / "current.json"
        progress = control / "progress" / "ledger.jsonl"
        fact = control / "fact_graph" / "facts" / "fact.txt"
        pause = control / "PAUSE"
        other_worker = control / "workers" / "other" / "local_memory" / "secret.txt"
        sentinels = {
            workspace_file: "WORKSPACE",
            readonly_file: "READONLY",
            plan: "PLAN",
            progress: "PROGRESS",
            fact: "FACT",
            pause: "PAUSED",
            other_worker: "PRIVATE",
        }
        for path, value in sentinels.items():
            path.write_text(value, encoding="utf-8")

        existing_link = workspace / "existing-link"
        existing_link.symlink_to(progress)
        paths = {
            "workspace_file": str(workspace_file),
            "readonly_file": str(readonly_file),
            "plan": str(plan),
            "progress": str(progress),
            "fact": str(fact),
            "pause": str(pause),
            "other_worker": str(other_worker),
            "existing_link": str(existing_link),
            "late_link": str(workspace / "late-link"),
            "dotdot_progress": str(workspace / ".." / "control" / "progress" / "ledger.jsonl"),
        }

        _write_profile(config_home, codex_real, readonly)
        server, port, connected, thread = _listener()
        inner_source = _inner_program(paths, port)
        compile(inner_source, "<danus-s5d0-inner>", "exec")
        (workspace / "probe_inner.py").write_text(inner_source, encoding="utf-8")

        child_env = {
            "HOME": str(Path.home()),
            "CODEX_HOME": str(config_home),
            "PATH": f"{launcher.parent}:/usr/local/bin:/usr/bin:/bin",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        os.environ["DANUS_TEST_SECRET"] = "must-not-cross-sandbox-boundary"
        cmd = [
            str(launcher),
            "sandbox",
            "-P", PROFILE,
            "-C", str(workspace),
            "/usr/bin/python3", "probe_inner.py",
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(workspace),
                env=child_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            print("FAIL: codex sandbox probe timed out")
            return 1

        thread.join(timeout=0.2)
        lines = [line for line in proc.stdout.splitlines() if line.startswith(MARKER)]
        if proc.returncode != 0 or not lines:
            print("FAIL: sandbox command did not return probe results")
            print(f"returncode={proc.returncode}")
            if proc.stdout:
                print("stdout:\n" + proc.stdout)
            if proc.stderr:
                print("stderr:\n" + proc.stderr)
            return 1

        results = json.loads(lines[-1][len(MARKER):])
        missing = sorted(EXPECTED_INNER_RESULTS - set(results))
        results["probe_assertions_complete"] = {
            "pass": not missing,
            "detail": "missing: " + ", ".join(missing) if missing else "",
        }
        if connected.is_set():
            results["network_loopback_denied"] = {
                "pass": False,
                "detail": "host listener accepted a sandbox connection",
            }

        protected = {
            "readonly": (readonly_file, "READONLY"),
            "plan": (plan, "PLAN"),
            "progress": (progress, "PROGRESS"),
            "fact_graph": (fact, "FACT"),
            "pause": (pause, "PAUSED"),
            "other_worker": (other_worker, "PRIVATE"),
        }
        changed = []
        for name, (path, expected) in protected.items():
            try:
                actual = path.read_text(encoding="utf-8")
            except OSError as exc:
                changed.append(f"{name}: unreadable on host after probe ({exc})")
                continue
            if actual != expected:
                changed.append(f"{name}: content changed")
        results["host_protected_state_unchanged"] = {
            "pass": not changed,
            "detail": "; ".join(changed),
        }

        failures = []
        for name in sorted(results):
            item = results[name]
            status = "PASS" if item["pass"] else "FAIL"
            detail = f" — {item['detail']}" if item.get("detail") else ""
            print(f"{status:4} {name}{detail}")
            if not item["pass"]:
                failures.append(name)

        if failures:
            print("\nS5-D0 RESULT: FAIL — " + ", ".join(failures))
            return 1
        print("\nS5-D0 RESULT: PASS")
        print(f"codex={codex_real}")
        return 0
    finally:
        if server is not None:
            server.close()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
