"""Read-only Codex quota telemetry via the official app-server protocol.

This module observes quota only. It never changes worker state, scheduling, model
selection, or experiment scope. Snapshots are persisted under the Danus control
project so a restarted Supervisor can inspect the last observation without making
another network request.

When Codex does not expose its historical 5-hour bucket, the persisted snapshot
also carries a clearly-labelled local proxy. The proxy reuses A2-7g structured
round telemetry and measures Danus Codex activity over a rolling five-hour window.
It is never presented as an OpenAI quota percentage.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from danus.core._util import utc_now
from danus.execution import context as C
from danus.execution import layout as L
from danus.execution.scaffold import atomic_write

_FIVE_HOURS_MINS = 300
_FIVE_HOURS_SECONDS = _FIVE_HOURS_MINS * 60
_WEEKLY_MINS = 7 * 24 * 60
_CALIBRATION_MAX_AGE_SECONDS = 6 * 60 * 60


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


def _reported_durations(snapshot: Dict[str, Any]) -> list[int]:
    durations: list[int] = []
    for key in ("primary", "secondary"):
        raw = snapshot.get(key)
        if not isinstance(raw, dict):
            continue
        value = raw.get("windowDurationMins")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0 and value not in durations:
            durations.append(value)
    return durations


def normalize_rate_limits(result: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize Codex quota without inventing windows the backend did not report."""
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

    five_hour = _window(bucket, _FIVE_HOURS_MINS)
    weekly = _window(bucket, _WEEKLY_MINS)
    reset_credits = result.get("rateLimitResetCredits")
    available_count = reset_credits.get("availableCount") if isinstance(reset_credits, dict) else None
    if isinstance(available_count, bool) or not isinstance(available_count, int):
        available_count = None

    return {
        "schema_version": 1,
        "source": "codex_app_server",
        "source_bucket": source_bucket,
        "observed_at": utc_now(),
        "available": True,
        "limit_id": bucket.get("limitId"),
        "plan_type": bucket.get("planType"),
        "rate_limit_reached_type": bucket.get("rateLimitReachedType"),
        "window_available": {
            "5h": five_hour is not None,
            "weekly": weekly is not None,
        },
        "reported_window_durations_mins": _reported_durations(bucket),
        "reset_credits_available": available_count,
        "windows": {
            "5h": five_hour,
            "weekly": weekly,
        },
    }


def _failure(message: str) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "source": "codex_app_server",
        "observed_at": utc_now(),
        "available": False,
        "error": message,
        "window_available": {"5h": False, "weekly": False},
        "reported_window_durations_mins": [],
        "reset_credits_available": None,
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


def _epoch(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _round_logs() -> list[Path]:
    """All worker round logs under the existing Danus project root."""
    return list(L.agents_root().glob("*/workers/*/logs/round_*.log"))


def _local_five_hour_proxy(
    official: Dict[str, Any],
    previous: Optional[Dict[str, Any]],
    *,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Rolling 5h Danus activity with short-lived online weekly calibration.

    Calibration uses only the latest positive official-weekly delta interval and is
    replaced on every new measurable delta. It expires after six hours and is
    invalidated by weekly reset/decrease or account limit-regime changes. This is a
    local pressure proxy, never an OpenAI 5h remaining-quota estimate.
    """
    now = float(time.time() if now is None else now)
    cutoff = now - _FIVE_HOURS_SECONDS
    paths = _round_logs()

    prior_proxy = previous.get("five_hour_proxy") if isinstance(previous, dict) else None
    if not isinstance(prior_proxy, dict):
        prior_proxy = {}
    prior_cal = prior_proxy.get("calibration")
    if not isinstance(prior_cal, dict):
        prior_cal = {}

    prior_since = _epoch(prior_proxy.get("observed_since"))
    mtimes: list[float] = []
    for path in paths:
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            pass
    oldest = min(mtimes, default=now)
    observed_since = min(x for x in (now, oldest, prior_since) if x is not None)
    coverage = max(0, min(_FIVE_HOURS_SECONDS, int(now - observed_since)))
    window_complete = coverage >= _FIVE_HOURS_SECONDS

    weekly = official.get("windows", {}).get("weekly") if isinstance(official.get("windows"), dict) else None
    if not isinstance(weekly, dict):
        weekly = None
    weekly_used = weekly.get("used_percent") if weekly else None
    weekly_reset = weekly.get("resets_at") if weekly else None
    if isinstance(weekly_used, bool) or not isinstance(weekly_used, (int, float)):
        weekly_used = None
    if isinstance(weekly_reset, bool) or not isinstance(weekly_reset, int):
        weekly_reset = None

    official_ok = weekly_used is not None and weekly_reset is not None
    plan_type = official.get("plan_type")
    limit_id = official.get("limit_id")
    prior_reset = prior_cal.get("weekly_reset_at")
    prior_plan = prior_cal.get("plan_type")
    prior_limit = prior_cal.get("limit_id")
    same_regime = bool(
        official_ok
        and prior_reset == weekly_reset
        and prior_plan == plan_type
        and prior_limit == limit_id
    )

    recalibration_reason = None
    if official_ok and prior_cal:
        if prior_reset != weekly_reset:
            recalibration_reason = "weekly_reset_changed"
        elif prior_plan != plan_type:
            recalibration_reason = "plan_changed"
        elif prior_limit != limit_id:
            recalibration_reason = "limit_changed"

    tokens_per_pct = prior_cal.get("tokens_per_weekly_percent") if same_regime else None
    calibrated_at = _epoch(prior_cal.get("calibrated_at")) if same_regime else None
    if isinstance(tokens_per_pct, bool) or not isinstance(tokens_per_pct, (int, float)) or tokens_per_pct <= 0:
        tokens_per_pct = None
    if tokens_per_pct is not None and (
        calibrated_at is None or now - calibrated_at > _CALIBRATION_MAX_AGE_SECONDS
    ):
        tokens_per_pct = None
        calibrated_at = None
        recalibration_reason = "calibration_expired"

    anchor_at = _epoch(prior_cal.get("anchor_observed_at")) if same_regime else None
    anchor_used = prior_cal.get("anchor_weekly_used_percent") if same_regime else None
    if isinstance(anchor_used, bool) or not isinstance(anchor_used, (int, float)):
        anchor_used = None
    if anchor_at is not None and now - anchor_at > _CALIBRATION_MAX_AGE_SECONDS:
        anchor_at = None
        anchor_used = None
        tokens_per_pct = None
        calibrated_at = None
        recalibration_reason = "calibration_expired"

    if official_ok and anchor_at is None:
        anchor_at = now
        anchor_used = float(weekly_used)

    scan_start = cutoff
    if anchor_at is not None:
        scan_start = min(scan_start, anchor_at)

    five_hour_tokens = 0
    five_hour_rounds = 0
    calibration_tokens = 0
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < scan_start:
            continue
        telemetry = C.inspect_log(path)
        if not telemetry.get("structured"):
            continue
        tokens = max(0, int(telemetry.get("uncached_tokens") or 0))
        if mtime >= cutoff:
            five_hour_tokens += tokens
            five_hour_rounds += 1
        if anchor_at is not None and mtime >= anchor_at:
            calibration_tokens += tokens

    calibration_status = "unavailable"
    if not official_ok:
        if prior_cal:
            calibration_status = "waiting_for_official"
            # Preserve a still-fresh calibration across transient sensor failure,
            # but never use it for a ready/hard-control proxy while official data
            # is unavailable.
            prior_value = prior_cal.get("tokens_per_weekly_percent")
            prior_time = _epoch(prior_cal.get("calibrated_at"))
            if (
                isinstance(prior_value, (int, float))
                and not isinstance(prior_value, bool)
                and prior_value > 0
                and prior_time is not None
                and now - prior_time <= _CALIBRATION_MAX_AGE_SECONDS
            ):
                tokens_per_pct = float(prior_value)
                calibrated_at = prior_time
            anchor_at = _epoch(prior_cal.get("anchor_observed_at"))
            anchor_used = prior_cal.get("anchor_weekly_used_percent")
            weekly_reset = prior_cal.get("weekly_reset_at")
            plan_type = prior_cal.get("plan_type")
            limit_id = prior_cal.get("limit_id")
    else:
        if not same_regime and prior_cal:
            tokens_per_pct = None
            calibrated_at = None
            anchor_at = now
            anchor_used = float(weekly_used)
            calibration_tokens = 0

        calibration_status = "ready" if tokens_per_pct is not None else "waiting_for_weekly_delta"
        if anchor_at is not None and anchor_used is not None:
            delta_pct = float(weekly_used) - float(anchor_used)
            if delta_pct < 0:
                tokens_per_pct = None
                calibrated_at = None
                anchor_at = now
                anchor_used = float(weekly_used)
                calibration_tokens = 0
                calibration_status = "waiting_for_weekly_delta"
                recalibration_reason = "weekly_decreased"
            elif delta_pct > 0:
                if calibration_tokens > 0:
                    # Latest measurable interval wins: fast adaptation to account
                    # quota/denominator changes, with no long-memory averaging.
                    tokens_per_pct = calibration_tokens / delta_pct
                    calibrated_at = now
                    calibration_status = "ready"
                    recalibration_reason = "online_update"
                else:
                    # The account meter moved without attributable Danus Codex
                    # activity. Treat this as external usage/regime uncertainty.
                    tokens_per_pct = None
                    calibrated_at = None
                    calibration_status = "waiting_for_weekly_delta"
                    recalibration_reason = "external_usage"
                anchor_at = now
                anchor_used = float(weekly_used)
                calibration_tokens = 0

    weekly_equivalent = None
    pace_pressure = None
    if window_complete and calibration_status == "ready" and tokens_per_pct is not None:
        weekly_equivalent = five_hour_tokens / tokens_per_pct
        sustainable_five_hour_pct = 100.0 * _FIVE_HOURS_MINS / _WEEKLY_MINS
        pace_pressure = 100.0 * weekly_equivalent / sustainable_five_hour_pct

    if not window_complete:
        status = "warming_up"
    elif calibration_status == "ready" and pace_pressure is not None:
        status = "ready"
    elif calibration_status == "waiting_for_official":
        status = "stale"
    else:
        status = "uncalibrated"

    calibration_age = None
    if calibrated_at is not None:
        calibration_age = max(0, int(now - calibrated_at))

    return {
        "source": "danus_structured_round_logs",
        "scope": "danus_codex_only",
        "approximate": True,
        "window_duration_mins": _FIVE_HOURS_MINS,
        "status": status,
        "observed_since": _iso(observed_since),
        "coverage_seconds": coverage,
        "window_complete": window_complete,
        "uncached_tokens": five_hour_tokens,
        "rounds": five_hour_rounds,
        "weekly_equivalent_percent": round(weekly_equivalent, 3) if weekly_equivalent is not None else None,
        "pace_pressure_percent": round(pace_pressure, 1) if pace_pressure is not None else None,
        "calibration": {
            "status": calibration_status,
            "method": "latest_positive_weekly_delta",
            "max_age_seconds": _CALIBRATION_MAX_AGE_SECONDS,
            "age_seconds": calibration_age,
            "recalibration_reason": recalibration_reason,
            "weekly_reset_at": weekly_reset,
            "plan_type": plan_type,
            "limit_id": limit_id,
            "anchor_observed_at": _iso(anchor_at) if anchor_at is not None else None,
            "anchor_weekly_used_percent": anchor_used,
            "calibrated_at": _iso(calibrated_at) if calibrated_at is not None else None,
            "tokens_per_weekly_percent": round(tokens_per_pct, 3) if tokens_per_pct is not None else None,
        },
    }


def quota_path(project_dir: Path) -> Path:
    return Path(project_dir) / "supervisor" / "quota.json"


def refresh_quota(
    project_dir: Path, *, codex_bin: Optional[str] = None, timeout: float = 15.0
) -> Dict[str, Any]:
    """Read once, attach the local five-hour proxy, and atomically persist."""
    previous = load_quota(project_dir)
    snapshot = read_codex_quota(codex_bin=codex_bin, timeout=timeout)
    snapshot["five_hour_proxy"] = _local_five_hour_proxy(snapshot, previous)
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
