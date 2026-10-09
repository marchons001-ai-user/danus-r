"""Regression for removing speculative quota stops without changing manual controls."""
from __future__ import annotations

from pathlib import Path

from danus.execution import layout as L
from danus.orchestration import cli, quota as Q


def _project(tmp_path: Path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    cli.do_new("P", roles="high:3")
    return root


def test_historical_quota_pauses_are_cleared_without_touching_manual_pause(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    legacy = L.WorkerLayout(root / "P" / "workers" / "high")
    manual = L.WorkerLayout(root / "P" / "workers" / "high2")
    budget = L.WorkerLayout(root / "P" / "workers" / "high3")
    legacy.pause.write_text("quota:pressure:recent_pace_exhausts_before_weekly_reset\n")
    legacy.stop.touch()
    manual.pause.write_text("manual\n")
    budget.pause.write_text("budget:resource_token_budget\n")
    budget.stop.touch()
    monkeypatch.setattr(cli, "_worker_alive", lambda _wl: False)

    applied = cli._clear_legacy_quota_pauses("P")
    assert applied == [{"worker": "high", "result": "legacy-quota-pause-cleared"}]
    assert not legacy.pause.exists()
    assert not legacy.stop.exists()
    assert manual.pause.read_text() == "manual\n"
    assert budget.pause.read_text() == "budget:resource_token_budget\n"
    assert budget.stop.exists()
    assert cli._clear_legacy_quota_pauses("P") == []


def test_quota_migration_keeps_live_worker_stop_file(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    legacy = L.WorkerLayout(root / "P" / "workers" / "high")
    legacy.pause.write_text("quota:pressure:prior\n")
    legacy.stop.touch()
    monkeypatch.setattr(cli, "_worker_alive", lambda wl: wl.name == "high")
    assert cli._clear_legacy_quota_pauses("P")[0]["worker"] == "high"
    assert not legacy.pause.exists()
    assert legacy.stop.exists()


def test_observational_quota_has_no_local_five_hour_prediction(tmp_path):
    project = tmp_path / "project"
    assert Q.load_quota(project) is None
    data = Q._failure("simulated offline")
    assert data["available"] is False
    assert "five_hour_proxy" not in data
    assert "block_new_codex_rounds" not in data
    assert not hasattr(cli, "_apply_quota_gate")
