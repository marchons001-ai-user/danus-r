"""A2-7j Codex quota sensor tests. Offline; no OpenAI/model calls."""
from __future__ import annotations

import json
import os
from pathlib import Path

from danus.orchestration import quota as Q


def _window(used: int, minutes: int, reset: int) -> dict:
    return {
        "usedPercent": used,
        "windowDurationMins": minutes,
        "resetsAt": reset,
    }


def _structured_round(path: Path, *, inp: int, cached: int, output: int, mtime: float) -> None:
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


def _weekly(used: int, reset: int) -> dict:
    return Q.normalize_rate_limits({
        "rateLimits": {
            "limitId": "codex",
            "primary": _window(used, 10080, reset),
            "secondary": None,
            "planType": "plus",
        }
    })


def test_normalize_prefers_codex_bucket_and_matches_window_duration():
    result = {
        "rateLimits": {
            "limitId": "legacy",
            "primary": _window(99, 300, 1),
        },
        "rateLimitsByLimitId": {
            "other": {"limitId": "other", "primary": _window(88, 300, 2)},
            "codex": {
                "limitId": "codex",
                "planType": "plus",
                "primary": _window(25, 10080, 2000),
                "secondary": _window(40, 300, 1000),
            },
        },
        "rateLimitResetCredits": {"availableCount": 2},
    }

    got = Q.normalize_rate_limits(result)
    assert got["available"] is True
    assert got["source_bucket"] == "rateLimitsByLimitId.codex"
    assert got["limit_id"] == "codex"
    assert got["plan_type"] == "plus"
    assert got["window_available"] == {"5h": True, "weekly": True}
    assert got["reported_window_durations_mins"] == [10080, 300]
    assert got["reset_credits_available"] == 2
    assert got["windows"]["5h"] == {
        "used_percent": 40,
        "remaining_percent": 60,
        "window_duration_mins": 300,
        "resets_at": 1000,
    }
    assert got["windows"]["weekly"] == {
        "used_percent": 25,
        "remaining_percent": 75,
        "window_duration_mins": 10080,
        "resets_at": 2000,
    }


def test_real_plus_shape_can_report_weekly_without_five_hour_window():
    got = Q.normalize_rate_limits({
        "rateLimits": {
            "limitId": "codex",
            "primary": _window(0, 10080, 1788486960),
            "secondary": None,
            "planType": "plus",
        },
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "primary": _window(0, 10080, 1788486960),
                "secondary": None,
                "planType": "plus",
            }
        },
        "rateLimitResetCredits": {"availableCount": 1},
    })
    assert got["available"] is True
    assert got["window_available"] == {"5h": False, "weekly": True}
    assert got["reported_window_durations_mins"] == [10080]
    assert got["reset_credits_available"] == 1
    assert got["windows"]["5h"] is None
    assert got["windows"]["weekly"] == {
        "used_percent": 0,
        "remaining_percent": 100,
        "window_duration_mins": 10080,
        "resets_at": 1788486960,
    }


def test_legacy_bucket_is_supported_without_guessing_unknown_window():
    got = Q.normalize_rate_limits({
        "rateLimits": {
            "limitId": "codex",
            "primary": _window(12, 300, 1234),
            "secondary": _window(50, 1440, 5678),
        }
    })
    assert got["source_bucket"] == "rateLimits"
    assert got["window_available"] == {"5h": True, "weekly": False}
    assert got["reported_window_durations_mins"] == [300, 1440]
    assert got["windows"]["5h"]["remaining_percent"] == 88
    assert got["windows"]["weekly"] is None


def test_five_hour_proxy_first_observation_has_no_fake_percent(tmp_path: Path):
    now = 100_000.0
    project = tmp_path / "projects" / "P"
    proxy = Q._local_five_hour_proxy(project, _weekly(0, 200_000), None, now=now)

    assert proxy["status"] == "warming_up"
    assert proxy["window_complete"] is False
    assert proxy["coverage_seconds"] == 0
    assert proxy["uncached_tokens"] == 0
    assert proxy["rounds"] == 0
    assert proxy["weekly_equivalent_percent"] is None
    assert proxy["pace_pressure_percent"] is None
    assert proxy["calibration"]["status"] == "waiting_for_weekly_delta"
    assert proxy["calibration"]["anchor_weekly_used_percent"] == 0.0
    assert proxy["calibration"]["tokens_per_weekly_percent"] is None


def test_five_hour_proxy_backfills_existing_logs_but_stays_uncalibrated(tmp_path: Path):
    now = 100_000.0
    project = tmp_path / "projects" / "P"
    old = project / "workers" / "w" / "logs" / "round_1.log"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("legacy or DSH log\n", encoding="utf-8")
    os.utime(old, (now - 20_000, now - 20_000))
    _structured_round(
        project / "workers" / "w" / "logs" / "round_2.log",
        inp=1_000, cached=200, output=200, mtime=now - 60,
    )

    proxy = Q._local_five_hour_proxy(project, _weekly(0, 200_000), None, now=now)

    assert proxy["window_complete"] is True
    assert proxy["status"] == "uncalibrated"
    assert proxy["uncached_tokens"] == 1_000
    assert proxy["rounds"] == 1
    assert proxy["pace_pressure_percent"] is None


def test_five_hour_proxy_calibrates_from_real_weekly_delta(tmp_path: Path):
    now = 100_000.0
    project = tmp_path / "projects" / "P"
    old = project / "workers" / "w" / "logs" / "round_0.log"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("older runtime history\n", encoding="utf-8")
    os.utime(old, (now - 20_000, now - 20_000))

    first = Q._local_five_hour_proxy(project, _weekly(10, 200_000), None, now=now)
    assert first["status"] == "uncalibrated"

    _structured_round(
        project / "workers" / "w" / "logs" / "round_1.log",
        inp=1_000, cached=200, output=200, mtime=now + 100,
    )
    previous = {"five_hour_proxy": first}
    second = Q._local_five_hour_proxy(
        project, _weekly(12, 200_000), previous, now=now + 200,
    )

    assert second["status"] == "ready"
    assert second["uncached_tokens"] == 1_000
    assert second["calibration"]["status"] == "ready"
    assert second["calibration"]["tokens_per_weekly_percent"] == 500.0
    assert second["weekly_equivalent_percent"] == 2.0
    assert second["pace_pressure_percent"] == 67.2


def test_read_and_persist_official_app_server_shape(tmp_path: Path):
    fake = tmp_path / "fake-codex"
    fake.write_text(
        """#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    if not line.strip():
        continue
    message = json.loads(line)
    if message["method"] == "initialize":
        assert message["params"]["clientInfo"]["name"] == "danus-r"
        print(json.dumps({"id": 1, "result": {"userAgent": "fake"}}), flush=True)
    elif message["method"] == "initialized":
        pass
    elif message["method"] == "account/rateLimits/read":
        print(json.dumps({"id": 2, "result": {
          "rateLimitsByLimitId": {"codex": {
            "limitId": "codex", "planType": "plus",
            "primary": {"usedPercent": 35, "windowDurationMins": 300, "resetsAt": 111},
            "secondary": {"usedPercent": 70, "windowDurationMins": 10080, "resetsAt": 222}
          }},
          "rateLimitResetCredits": {"availableCount": 3}
        }}), flush=True)
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    project = tmp_path / "control" / "P"

    got = Q.refresh_quota(project, codex_bin=str(fake), timeout=2)
    assert got["available"] is True
    assert got["window_available"] == {"5h": True, "weekly": True}
    assert got["reset_credits_available"] == 3
    assert got["windows"]["5h"]["remaining_percent"] == 65
    assert got["windows"]["weekly"]["remaining_percent"] == 30
    assert got["five_hour_proxy"]["source"] == "danus_structured_round_logs"
    assert got["five_hour_proxy"]["approximate"] is True
    saved = Q.load_quota(project)
    assert saved == got
    assert Q.quota_path(project) == project / "supervisor" / "quota.json"


def test_sensor_failure_is_persisted_and_never_raises(tmp_path: Path):
    project = tmp_path / "P"
    got = Q.refresh_quota(project, codex_bin=str(tmp_path / "missing-codex"), timeout=1)
    assert got["available"] is False
    assert got["window_available"] == {"5h": False, "weekly": False}
    assert got["reported_window_durations_mins"] == []
    assert got["reset_credits_available"] is None
    assert got["windows"] == {"5h": None, "weekly": None}
    assert got["five_hour_proxy"]["calibration"]["status"] == "unavailable"
    assert Q.load_quota(project) == got


def test_sensor_resolves_openai_codex_even_when_workers_use_dsh(monkeypatch):
    monkeypatch.setenv("CODEX_BACKEND", "dsh")
    resolved = Path(Q._openai_codex_bin())
    assert resolved.name == "codex"
    assert resolved.name != "codex-dsh"
