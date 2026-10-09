"""A2-7j Codex quota sensor tests. Offline; no OpenAI/model calls."""
from __future__ import annotations

import json
from pathlib import Path

from danus.orchestration import quota as Q


def _window(used: int, minutes: int, reset: int) -> dict:
    return {
        "usedPercent": used,
        "windowDurationMins": minutes,
        "resetsAt": reset,
    }


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


def test_read_and_persist_official_app_server_shape(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(tmp_path / "projects"))
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
    assert "five_hour_proxy" not in got
    saved = Q.load_quota(project)
    assert saved == got
    assert Q.quota_path(project) == project / "supervisor" / "quota.json"


def test_sensor_failure_is_persisted_and_never_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(tmp_path / "projects"))
    project = tmp_path / "P"
    got = Q.refresh_quota(project, codex_bin=str(tmp_path / "missing-codex"), timeout=1)
    assert got["available"] is False
    assert got["window_available"] == {"5h": False, "weekly": False}
    assert got["reported_window_durations_mins"] == []
    assert got["reset_credits_available"] is None
    assert got["windows"] == {"5h": None, "weekly": None}
    assert "five_hour_proxy" not in got
    assert Q.load_quota(project) == got


def test_sensor_resolves_openai_codex_even_when_workers_use_dsh(monkeypatch):
    monkeypatch.setenv("CODEX_BACKEND", "dsh")
    resolved = Path(Q._openai_codex_bin())
    assert resolved.name == "codex"
    assert resolved.name != "codex-dsh"
