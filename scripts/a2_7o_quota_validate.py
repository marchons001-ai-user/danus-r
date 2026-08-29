#!/usr/bin/env python3
"""A2-7o live quota-proxy validation helper.

This script never starts a model. It performs only the existing read-only Codex
rate-limit query, lets ``danus.orchestration.quota`` update its persisted proxy,
classifies the result, and appends a compact validation sample under the control
project's ``supervisor/`` directory.

Use a real Danus scientific worker separately to create the smallest possible
attributable Codex token pulse. After that pulse, ``--watch-seconds`` can poll the
quota endpoint without consuming model tokens until the official weekly meter
moves or the timeout expires.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from danus.core._util import append_jsonl
from danus.execution import layout as L
from danus.orchestration import quota as Q

_RESET_REASONS = {
    "weekly_decreased",
    "weekly_reset_changed",
    "plan_changed",
    "limit_changed",
}
_TERMINAL_VERDICTS = {"PASS", "RESET", "EXTERNAL_USAGE"}


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _weekly(snapshot: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return None
    windows = snapshot.get("windows")
    if not isinstance(windows, dict):
        return None
    weekly = windows.get("weekly")
    return weekly if isinstance(weekly, dict) else None


def classify(previous: Optional[Dict[str, Any]], current: Dict[str, Any]) -> Dict[str, Any]:
    """Classify one real quota observation without changing quota policy."""
    proxy = current.get("five_hour_proxy")
    if not isinstance(proxy, dict):
        proxy = {}
    calibration = proxy.get("calibration")
    if not isinstance(calibration, dict):
        calibration = {}

    current_weekly = _weekly(current)
    previous_weekly = _weekly(previous)
    current_used = _number(current_weekly.get("used_percent")) if current_weekly else None
    previous_used = _number(previous_weekly.get("used_percent")) if previous_weekly else None
    current_reset = current_weekly.get("resets_at") if current_weekly else None
    previous_reset = previous_weekly.get("resets_at") if previous_weekly else None

    delta = None
    if (
        current_used is not None
        and previous_used is not None
        and current_reset is not None
        and current_reset == previous_reset
    ):
        delta = current_used - previous_used

    reason = str(calibration.get("recalibration_reason") or "") or None
    tokens_per_pct = _number(calibration.get("tokens_per_weekly_percent"))
    calibration_status = str(calibration.get("status") or "unavailable")

    if not current.get("available") or current_weekly is None:
        verdict = "SENSOR_UNAVAILABLE"
    elif reason in _RESET_REASONS:
        verdict = "RESET"
    elif reason == "external_usage":
        verdict = "EXTERNAL_USAGE"
    elif calibration_status == "ready" and tokens_per_pct is not None and tokens_per_pct > 0:
        verdict = "PASS"
    else:
        verdict = "WAIT"

    return {
        "verdict": verdict,
        "observed_at": current.get("observed_at"),
        "official_available": bool(current.get("available")),
        "official_error": str(current.get("error") or "") or None,
        "official_5h_available": bool(current.get("window_available", {}).get("5h"))
        if isinstance(current.get("window_available"), dict)
        else False,
        "weekly_used_percent": current_used,
        "weekly_remaining_percent": _number(current_weekly.get("remaining_percent"))
        if current_weekly
        else None,
        "weekly_delta_percent": delta,
        "weekly_resets_at": current_reset,
        "proxy_status": proxy.get("status"),
        "window_complete": bool(proxy.get("window_complete")),
        "proxy_rounds": int(proxy.get("rounds") or 0),
        "proxy_uncached_tokens": int(proxy.get("uncached_tokens") or 0),
        "weekly_equivalent_percent": _number(proxy.get("weekly_equivalent_percent")),
        "pace_pressure_percent": _number(proxy.get("pace_pressure_percent")),
        "calibration_status": calibration_status,
        "calibration_reason": reason,
        "tokens_per_weekly_percent": tokens_per_pct,
        "calibration_age_seconds": calibration.get("age_seconds"),
    }


def validation_path(project_dir: Path) -> Path:
    return Path(project_dir) / "supervisor" / "quota_validation.jsonl"


def _emit(row: Dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))
        return
    parts = [
        row["verdict"],
        f"weekly_used={row['weekly_used_percent']}",
        f"delta={row['weekly_delta_percent']}",
        f"cal={row['calibration_status']}",
        f"reason={row['calibration_reason'] or '-'}",
        f"rounds={row['proxy_rounds']}",
        f"uncached_tokens={row['proxy_uncached_tokens']}",
        f"tokens_per_weekly_pct={row['tokens_per_weekly_percent']}",
        f"proxy={row['proxy_status']}",
    ]
    if row.get("official_error"):
        parts.append(f"error={row['official_error']}")
    print(" ".join(parts))


def _sample(project_dir: Path, project: str, *, refresh: bool) -> Dict[str, Any]:
    previous = Q.load_quota(project_dir)
    current = Q.refresh_quota(project_dir) if refresh else previous
    if current is None:
        current = {
            "available": False,
            "observed_at": None,
            "window_available": {"5h": False, "weekly": False},
            "windows": {"5h": None, "weekly": None},
        }
    row = classify(previous, current)
    row["project"] = project
    append_jsonl(validation_path(project_dir), row)
    return row


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A2-7o live quota calibration validator")
    parser.add_argument("project", help="Danus control project name")
    parser.add_argument(
        "--watch-seconds",
        type=float,
        default=0.0,
        help="poll read-only quota telemetry for up to this many seconds; no model calls",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=10.0,
        help="poll interval while watching (default: 10 seconds)",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="inspect the last persisted quota snapshot without contacting Codex app-server",
    )
    parser.add_argument("--json", action="store_true", help="emit one JSON object per sample")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.watch_seconds < 0:
        raise SystemExit("--watch-seconds must be >= 0")
    if args.poll_seconds <= 0:
        raise SystemExit("--poll-seconds must be > 0")
    if args.no_refresh and args.watch_seconds > 0:
        raise SystemExit("--no-refresh cannot be combined with --watch-seconds")

    project_dir = L.project_dir(args.project)
    if not project_dir.is_dir():
        raise SystemExit(f"no such project: {args.project}")

    deadline = time.monotonic() + args.watch_seconds
    row: Dict[str, Any]
    while True:
        row = _sample(project_dir, args.project, refresh=not args.no_refresh)
        _emit(row, as_json=args.json)
        if row["verdict"] in _TERMINAL_VERDICTS or args.watch_seconds <= 0:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(args.poll_seconds, remaining))

    if row["verdict"] == "PASS":
        return 0
    if row["verdict"] == "RESET":
        return 3
    if row["verdict"] == "EXTERNAL_USAGE":
        return 4
    if row["verdict"] == "SENSOR_UNAVAILABLE":
        return 5
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
