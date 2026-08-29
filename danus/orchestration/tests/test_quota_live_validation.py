"""A2-7o validation verdict tests. Offline; no Codex or model calls."""
from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "a2_7o_quota_validate.py"
_SPEC = importlib.util.spec_from_file_location("a2_7o_quota_validate", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
V = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(V)


def _snapshot(
    *,
    used: float = 20,
    reset: int = 200_000,
    cal_status: str = "waiting_for_weekly_delta",
    reason: str | None = None,
    tokens_per_pct: float | None = None,
    available: bool = True,
) -> dict:
    return {
        "available": available,
        "observed_at": "2026-08-29T00:00:00+00:00",
        "window_available": {"5h": False, "weekly": available},
        "windows": {
            "5h": None,
            "weekly": {
                "used_percent": used,
                "remaining_percent": 100 - used,
                "window_duration_mins": 10080,
                "resets_at": reset,
            }
            if available
            else None,
        },
        "five_hour_proxy": {
            "status": "ready" if cal_status == "ready" else "uncalibrated",
            "window_complete": True,
            "rounds": 2,
            "uncached_tokens": 1000,
            "weekly_equivalent_percent": 2.0 if cal_status == "ready" else None,
            "pace_pressure_percent": 67.2 if cal_status == "ready" else None,
            "calibration": {
                "status": cal_status,
                "recalibration_reason": reason,
                "tokens_per_weekly_percent": tokens_per_pct,
                "age_seconds": 0,
            },
        },
    }


def test_pass_requires_real_ready_calibration():
    previous = _snapshot(used=20)
    current = _snapshot(
        used=21,
        cal_status="ready",
        reason="online_update",
        tokens_per_pct=500,
    )

    got = V.classify(previous, current)

    assert got["verdict"] == "PASS"
    assert got["weekly_delta_percent"] == 1.0
    assert got["tokens_per_weekly_percent"] == 500.0


def test_wait_does_not_claim_success_before_official_delta():
    previous = _snapshot(used=20)
    current = _snapshot(used=20)

    got = V.classify(previous, current)

    assert got["verdict"] == "WAIT"
    assert got["weekly_delta_percent"] == 0.0


def test_weekly_drop_is_reported_as_reset_not_bad_calibration():
    previous = _snapshot(used=60)
    current = _snapshot(used=0, reason="weekly_decreased")

    got = V.classify(previous, current)

    assert got["verdict"] == "RESET"
    assert got["weekly_delta_percent"] == -60.0
    assert got["calibration_reason"] == "weekly_decreased"


def test_reset_timestamp_change_is_reported_as_reset():
    previous = _snapshot(used=60, reset=200_000)
    current = _snapshot(used=0, reset=300_000, reason="weekly_reset_changed")

    got = V.classify(previous, current)

    assert got["verdict"] == "RESET"
    assert got["weekly_delta_percent"] is None


def test_external_account_movement_is_not_misattributed_to_danus():
    previous = _snapshot(used=20)
    current = _snapshot(used=21, reason="external_usage")

    got = V.classify(previous, current)

    assert got["verdict"] == "EXTERNAL_USAGE"


def test_missing_official_weekly_sensor_fails_closed_for_validation():
    current = _snapshot(available=False)

    got = V.classify(None, current)

    assert got["verdict"] == "SENSOR_UNAVAILABLE"
    assert got["weekly_used_percent"] is None
