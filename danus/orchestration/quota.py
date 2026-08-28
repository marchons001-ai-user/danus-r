"""Read-only Codex quota telemetry via the official app-server protocol.

This module observes quota only. It never changes worker state, scheduling, model
selection, or experiment scope. Snapshots are persisted under the Danus control
project so a restarted Supervisor can inspect the last observation without making
another network request.
"""
from __future__ import annotations

import json
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from danus.core._util import utc_now
from danus.execution.scaffold import atomic_write

_FIVE_HOURS_MINS = 300
_WEEKLY_MINS = 7 * 24 * 60


def _openai_codex_bin() -> str:
    """Resolve the OpenAI Codex CLI, independent of a worker's DSH backend."""
    wrapper = Path(__file__).resolve().parents[2] / "bin" / "codex"
    if wrapper.is_file():
        return str(wrapper)
    return shutil.which("codex") or "codex"


def _window(snapshot: Dict[str, Any], duration_mins: int) -> Optional[Dict[str, Any]]:
    for key in ("primary", "secondary"):
        raw = snapshot.get(key)
        if not isinstance(raw, dict) or raw.get("windowDurationMins") != duration_mins:
            continue
        used = raw.get("usedPercent")
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            return None
        used_value = max(0.0, min(100.0, float(used)))
        if used_value.is_integer():
            used_value = int(used_value)
        reset = raw.get("resetsAt")
        return {
            "used_percent": used_value,
            "remaining_percent": 100 - used_value,
            "window_duration_mins": duration_mins,
            "resets_at": int(reset) if isinstance(reset, (int, float)) and not isinstance(reset, bool) else None,
        }
    return None


def normalize_rate_limits(result: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize only the Codex 5h/weekly windows; do not guess bucket semantics."""
    bucket = None
    source_bucket = None
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict) and isinstance(by_id.get("codex"), dict):
        bucket = by_id["codex"]
        source_bucket = "rateLimitsByLimitId.codex"
    elif isinstance(result.get("rateLimits"), dict):
        bucket = result["rateLimits"]
        source_bucket = "rateLimits"
    if bucket is None:
        raise ValueError("Codex rate-limit response has no codex bucket")

    return {
        "schema_version": 1,
        "source": "codex_app_server",
        "source_bucket": source_bucket,
        "observed_at": utc_now(),
        "available": True,
        "limit_id": bucket.get("limitId"),
        "plan_type": bucket.get("planType"),
        "rate_limit_reached_type": bucket.get("rateLimitReachedType"),
        "windows": {
            "5h": _window(bucket, _FIVE_HOURS_MINS),
            "weekly": _window(bucket, _WEEKLY_MINS),
        },
    }


def _failure(message: str) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "source": "codex_app_server",
        "observed_at": utc_now(),
        "available": False,
        "error": message,
        "windows": {"5h": None, "weekly": None},
    }


def _send(proc: subprocess.Popen, message: Dict[str, Any]) -> None:
    if proc.stdin is None:
        raise OSError("Codex app-server stdin unavailable")
    proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def _wait_response(lines: "queue.Queue[Optional[str]]", request_id: int, deadline: float) -> Optional[Dict[str, Any]]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError from exc
        if line is None:
            return None
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and message.get("id") == request_id:
            return message


def read_codex_quota(*, codex_bin: Optional[str] = None, timeout: float = 15.0) -> Dict[str, Any]:
    """Perform one read-only ``account/rateLimits/read`` app-server exchange."""
    binary = codex_bin or _openai_codex_bin()
    timeout = max(0.1, float(timeout))
    try:
        proc = subprocess.Popen(
            [binary, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        return _failure(f"Codex app-server unavailable: {exc.__class__.__name__}")

    lines: "queue.Queue[Optional[str]]" = queue.Queue()

    def _read_stdout() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=_read_stdout, daemon=True).start()
    deadline = time.monotonic() + timeout
    try:
        _send(proc, {
            "method": "initialize",
            "id": 1,
            "params": {"clientInfo": {"name": "danus-r", "version": "0.1.0"}},
        })
        init = _wait_response(lines, 1, deadline)
        if init is None:
            return _failure(f"Codex app-server initialize response missing (rc={proc.poll()})")
        if isinstance(init.get("error"), dict):
            message = str(init["error"].get("message") or "initialize failed")
            return _failure(f"Codex app-server initialize failed: {message}")

        _send(proc, {"method": "initialized"})
        _send(proc, {"method": "account/rateLimits/read", "id": 2})
        response = _wait_response(lines, 2, deadline)
        if response is None:
            return _failure(f"Codex quota response missing (app-server rc={proc.poll()})")
        if isinstance(response.get("error"), dict):
            return _failure(str(response["error"].get("message") or "Codex quota read failed"))
        result = response.get("result")
        if not isinstance(result, dict):
            return _failure("Codex quota response has no result object")
        try:
            return normalize_rate_limits(result)
        except ValueError as exc:
            return _failure(str(exc))
    except TimeoutError:
        return _failure("Codex quota read timed out")
    except (BrokenPipeError, OSError) as exc:
        return _failure(f"Codex app-server I/O failed: {exc.__class__.__name__}")
    finally:
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()


def quota_path(project_dir: Path) -> Path:
    return Path(project_dir) / "supervisor" / "quota.json"


def refresh_quota(
    project_dir: Path, *, codex_bin: Optional[str] = None, timeout: float = 15.0
) -> Dict[str, Any]:
    """Read once and atomically persist the observation, including failures."""
    snapshot = read_codex_quota(codex_bin=codex_bin, timeout=timeout)
    atomic_write(
        quota_path(project_dir),
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return snapshot


def load_quota(project_dir: Path) -> Optional[Dict[str, Any]]:
    """Read the last persisted snapshot without contacting Codex."""
    path = quota_path(project_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) and payload.get("schema_version") == 1 else None
