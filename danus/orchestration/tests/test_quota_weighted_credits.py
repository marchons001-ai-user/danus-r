"""A2-7o model-weighted Codex credit proxy tests. Offline only."""
from __future__ import annotations

import json
import os
from pathlib import Path

from danus.orchestration import quota as Q
from danus.orchestration.control import quota_policy


def _window(used: int, minutes: int, reset: int) -> dict:
    return {"usedPercent": used, "windowDurationMins": minutes, "resetsAt": reset}


def _weekly(used: int, reset: int) -> dict:
    return Q.normalize_rate_limits({
        "rateLimits": {
            "limitId": "codex",
            "primary": _window(used, 10080, reset),
            "secondary": None,
            "planType": "plus",
        }
    })


def _role(worker: Path, model: str, mtime: float) -> None:
    worker.mkdir(parents=True, exist_ok=True)
    path = worker / ".role"
    path.write_text(f"MODEL={model}\nROLE=researcher\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _round(path: Path, *, inp: int, cached: int, output: int, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "type": "turn.completed",
        "usage": {
            "input_tokens": inp,
            "cached_input_tokens": cached,
            "cache_write_input_tokens": 0,
            "output_tokens": output,
            "reasoning_output_tokens": 0,
        },
    }) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_credit_rate_card_distinguishes_sol_terra_luna():
    telemetry = {
        "input_tokens": 1_000_000,
        "cached_input_tokens": 0,
        "output_tokens": 0,
    }
    assert Q._weighted_credits(telemetry, "gpt-5.6-sol") == 100.0
    assert Q._weighted_credits(telemetry, "gpt-5.6-terra") == 50.0
    assert Q._weighted_credits(telemetry, "gpt-5.6-luna") == 5.0


def test_plus_prior_is_estimated_and_never_hard_gates(tmp_path: Path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    now = 100_000.0
    worker = root / "P" / "workers" / "w"
    _role(worker, "gpt-5.6-terra", now - 30_000)
    old = worker / "logs" / "round_0.log"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("coverage only\n", encoding="utf-8")
    os.utime(old, (now - 20_000, now - 20_000))
    _round(worker / "logs" / "round_1.log", inp=1_000_000, cached=0, output=0, mtime=now - 60)

    official = _weekly(0, 200_000)
    official["observed_at"] = Q._iso(now)
    proxy = Q._local_five_hour_proxy(official, None, now=now)
    official["five_hour_proxy"] = proxy

    assert proxy["status"] == "estimated"
    assert proxy["weighted_credits"] == 50.0
    assert proxy["credit_rounds"] == 1
    assert proxy["estimated_five_hour_used_percent"] == 20.8
    assert proxy["estimate_source"] == "community_plus_prior"
    assert proxy["estimate_confidence"] == "low"
    assert proxy["calibration"]["status"] == "waiting_for_weekly_delta"

    policy = quota_policy(official, now=now)
    assert policy["state"] == "UNKNOWN"
    assert policy["reason"] == "short_window_not_ready"
    assert policy["block_new_codex_rounds"] is False


def test_empirical_credit_calibration_uses_last_three_median(tmp_path: Path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    now = 100_000.0
    worker = root / "P" / "workers" / "w"
    _role(worker, "gpt-5.6-terra", now - 30_000)
    old = worker / "logs" / "round_0.log"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("coverage only\n", encoding="utf-8")
    os.utime(old, (now - 20_000, now - 20_000))

    first = Q._local_five_hour_proxy(_weekly(10, 200_000), None, now=now)

    _round(worker / "logs" / "round_1.log", inp=1_000_000, cached=0, output=0, mtime=now + 100)
    second = Q._local_five_hour_proxy(
        _weekly(12, 200_000), {"five_hour_proxy": first}, now=now + 200
    )
    assert second["calibration"]["basis"] == "weighted_credits"
    assert second["calibration"]["credit_samples"] == [25.0]
    assert second["calibration"]["credits_per_weekly_percent"] == 25.0

    _round(worker / "logs" / "round_2.log", inp=1_200_000, cached=0, output=0, mtime=now + 300)
    third = Q._local_five_hour_proxy(
        _weekly(13, 200_000), {"five_hour_proxy": second}, now=now + 400
    )
    assert third["calibration"]["credit_samples"] == [25.0, 60.0]
    assert third["calibration"]["credits_per_weekly_percent"] == 42.5

    _round(worker / "logs" / "round_3.log", inp=300_000, cached=0, output=0, mtime=now + 500)
    fourth = Q._local_five_hour_proxy(
        _weekly(14, 200_000), {"five_hour_proxy": third}, now=now + 600
    )
    assert fourth["calibration"]["credit_samples"] == [25.0, 60.0, 15.0]
    assert fourth["calibration"]["credits_per_weekly_percent"] == 25.0
    assert fourth["status"] == "ready"
