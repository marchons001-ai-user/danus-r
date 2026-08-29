"""A2-7o regressions from live Plus quota observations."""
from __future__ import annotations

import json
import os
from pathlib import Path

from danus.orchestration import quota as Q


def _weekly(used: int, reset: int) -> dict:
    return Q.normalize_rate_limits({
        "rateLimits": {
            "limitId": "codex",
            "primary": {
                "usedPercent": used,
                "windowDurationMins": 10080,
                "resetsAt": reset,
            },
            "secondary": None,
            "planType": "plus",
        }
    })


def _round(path: Path, *, mtime: float, uncached: int = 1000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "type": "turn.completed",
        "usage": {
            "input_tokens": uncached,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
        },
    }) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_zero_anchor_tolerates_sliding_reset_timestamp(tmp_path: Path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    now = 100_000.0

    first = Q._local_five_hour_proxy(_weekly(0, 200_000), None, now=now)
    second = Q._local_five_hour_proxy(
        _weekly(0, 200_300), {"five_hour_proxy": first}, now=now + 300,
    )

    assert second["calibration"]["status"] == "waiting_for_weekly_delta"
    assert second["calibration"]["recalibration_reason"] is None
    assert second["calibration"]["anchor_weekly_used_percent"] == 0.0
    assert second["calibration"]["anchor_observed_at"] == first["calibration"]["anchor_observed_at"]


def test_first_positive_delta_calibrates_even_if_reset_timestamp_becomes_fixed(tmp_path: Path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    now = 100_000.0
    project = root / "P"

    first = Q._local_five_hour_proxy(_weekly(0, 200_000), None, now=now)
    _round(project / "workers" / "w" / "logs" / "round_1.log", mtime=now + 100)
    second = Q._local_five_hour_proxy(
        _weekly(1, 200_300), {"five_hour_proxy": first}, now=now + 200,
    )

    assert second["calibration"]["status"] == "ready"
    assert second["calibration"]["recalibration_reason"] == "online_update"
    assert second["calibration"]["tokens_per_weekly_percent"] == 1000.0
    assert second["calibration"]["anchor_weekly_used_percent"] == 1.0


def test_positive_anchor_still_invalidates_on_reset_timestamp_change(tmp_path: Path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    now = 100_000.0
    previous = {
        "five_hour_proxy": {
            "observed_since": Q._iso(now - 20_000),
            "calibration": {
                "status": "ready",
                "weekly_reset_at": 200_000,
                "plan_type": "plus",
                "limit_id": "codex",
                "anchor_observed_at": Q._iso(now - 100),
                "anchor_weekly_used_percent": 10.0,
                "calibrated_at": Q._iso(now - 100),
                "tokens_per_weekly_percent": 500.0,
            },
        },
    }

    current = Q._local_five_hour_proxy(_weekly(10, 200_300), previous, now=now)

    assert current["calibration"]["status"] == "waiting_for_weekly_delta"
    assert current["calibration"]["recalibration_reason"] == "weekly_reset_changed"
    assert current["calibration"]["tokens_per_weekly_percent"] is None
