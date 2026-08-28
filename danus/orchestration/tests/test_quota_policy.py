"""A2-7k quota-aware Supervisor tests. Offline; no model/network calls."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from danus.execution import layout as L
from danus.orchestration import cli
from danus.orchestration.control import quota_policy


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _snapshot(*, now: float, weekly_remaining: float = 50,
              reset_in: float = 10 * 60 * 60, weekly_equivalent: float = 5,
              proxy_status: str = "ready") -> dict:
    return {
        "schema_version": 1,
        "available": True,
        "observed_at": _iso(now),
        "limit_id": "codex",
        "plan_type": "plus",
        "rate_limit_reached_type": None,
        "windows": {
            "5h": None,
            "weekly": {
                "used_percent": 100 - weekly_remaining,
                "remaining_percent": weekly_remaining,
                "window_duration_mins": 10080,
                "resets_at": int(now + reset_in),
            },
        },
        "five_hour_proxy": {
            "status": proxy_status,
            "weekly_equivalent_percent": weekly_equivalent,
        },
    }


def test_policy_is_fail_open_for_missing_or_stale_telemetry():
    now = 100_000.0
    assert quota_policy(None, now=now)["block_new_codex_rounds"] is False
    policy = quota_policy(_snapshot(now=now - 901), now=now)
    assert policy["state"] == "UNKNOWN"
    assert policy["reason"] == "telemetry_stale"
    assert policy["block_new_codex_rounds"] is False


def test_policy_blocks_only_when_official_limit_is_exhausted():
    now = 100_000.0
    policy = quota_policy(_snapshot(now=now, weekly_remaining=0), now=now)
    assert policy["state"] == "EXHAUSTED"
    assert policy["block_new_codex_rounds"] is True


def test_policy_uses_current_reset_and_recent_proxy_not_fixed_quota_size():
    now = 100_000.0
    pressure = quota_policy(
        _snapshot(now=now, weekly_remaining=10, reset_in=10 * 60 * 60, weekly_equivalent=6),
        now=now,
    )
    assert pressure["state"] == "PRESSURE"
    assert pressure["projected_weekly_burn_to_reset"] == 12.0
    assert pressure["block_new_codex_rounds"] is True

    normal = quota_policy(
        _snapshot(now=now, weekly_remaining=10, reset_in=5 * 60 * 60, weekly_equivalent=6),
        now=now,
    )
    assert normal["state"] == "NORMAL"
    assert normal["projected_weekly_burn_to_reset"] == 6.0
    assert normal["block_new_codex_rounds"] is False


def test_policy_does_not_hard_gate_uncalibrated_proxy():
    now = 100_000.0
    policy = quota_policy(_snapshot(now=now, proxy_status="uncalibrated"), now=now)
    assert policy["state"] == "UNKNOWN"
    assert policy["reason"] == "short_window_not_ready"
    assert policy["block_new_codex_rounds"] is False


def test_quota_gate_uses_existing_graceful_stop_boundary_and_skips_dsh(tmp_path: Path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    cli.do_new("P", roles="high:2")
    codex = L.WorkerLayout(root / "P" / "workers" / "high")
    dsh = L.WorkerLayout(root / "P" / "workers" / "high2")
    codex.role.write_text("ROLE=researcher\nBACKEND=chatgpt\n", encoding="utf-8")
    dsh.role.write_text("ROLE=reviewer\nBACKEND=dsh\n", encoding="utf-8")

    def forbidden(*_a, **_k):
        raise AssertionError("quota must not SIGTERM")

    monkeypatch.setattr(cli, "_stop_one", forbidden)
    rows = [
        {"worker": "high", "alive": True, "progress_state": "RUNNING"},
        {"worker": "high2", "alive": True, "progress_state": "RUNNING"},
    ]
    applied = cli._apply_quota_gate(
        "P", rows,
        {"state": "PRESSURE", "reason": "pace", "block_new_codex_rounds": True},
    )

    assert applied == [{"worker": "high", "result": "pause-at-round-boundary"}]
    assert codex.pause.read_text(encoding="utf-8").startswith("quota:pressure:")
    assert codex.stop.exists()
    assert not dsh.pause.exists()
    assert not dsh.stop.exists()


def test_quota_recovery_clears_only_quota_owned_pause(tmp_path: Path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    cli.do_new("P", roles="high:2")
    quota_worker = L.WorkerLayout(root / "P" / "workers" / "high")
    manual_worker = L.WorkerLayout(root / "P" / "workers" / "high2")
    quota_worker.pause.write_text("quota:pressure:pace\n", encoding="utf-8")
    manual_worker.pause.write_text("manual\n", encoding="utf-8")

    started = []
    monkeypatch.setattr(cli, "_start_one", lambda wl: started.append(wl.name) or "started")
    rows = [
        {"worker": "high", "alive": False, "progress_state": "RUNNING"},
        {"worker": "high2", "alive": False, "progress_state": "RUNNING"},
    ]
    applied = cli._apply_quota_gate(
        "P", rows,
        {"state": "NORMAL", "reason": "recovered", "block_new_codex_rounds": False},
    )

    assert applied == [{"worker": "high", "result": "started"}]
    assert started == ["high"]
    assert not quota_worker.pause.exists()
    assert manual_worker.pause.read_text(encoding="utf-8").strip() == "manual"
