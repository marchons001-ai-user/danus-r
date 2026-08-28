"""Read-only Codex quota telemetry via the official app-server protocol.

This module observes quota only. It never changes worker state, scheduling, model
selection, or experiment scope. Snapshots are persisted under the Danus control
project so a restarted Supervisor can inspect the last observation without making
another network request.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
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


def read_codex_quota(*, codex_bin: Optional[str] = None, timeout: float = 15.0) -> Dict[str, Any]:
    """Perform one read-only ``account/rateLimits/read`` app-server exchange."""
    binary = codex_bin or _openai_codex_bin()
    messages = [
        {
            "method": "initialize",
            "id": 1,
            "params": {"clientInfo": {"name": "danus-r", "version": "0.1.0"}},
        },
        {"method": "initialized"},
        {"method": "account/rateLimits/read", "id": 2},
    ]
    wire = "".join(json.dumps(message, separators=(",", ":")) + "\n" for message in messages)
    try:
        proc = subprocess.run(
            [binary, "app-server", "--listen", "stdio://"],
            input=wire,
            text=True,
            capture_output=True,
            timeout=max(0.1, float(timeout)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _failure("Codex quota read timed out")
    except OSError as exc:
        return _failure(f"Codex app-server unavailable: {exc.__class__.__name__}")

    response = None
    init_error = None
    for line in proc.stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("id") == 1 and isinstance(message.get("error"), dict):
            init_error = str(message["error"].get("message") or "initialize failed")
        if message.get("id") == 2:
            response = message
            break

    if response is None:
        if init_error:
            return _failure(f"Codex app-server initialize failed: {init_error}")
        return _failure(f"Codex quota response missing (app-server rc={proc.returncode})")
    if isinstance(response.get("error"), dict):
        return _failure(str(response["error"].get("message") or "Codex quota read failed"))
    result = response.get("result")
    if not isinstance(result, dict):
        return _failure("Codex quota response has no result object")
    try:
        return normalize_rate_limits(result)
    except ValueError as exc:
        return _failure(str(exc))


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
