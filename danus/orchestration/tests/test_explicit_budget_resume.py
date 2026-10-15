"""Explicit budget recovery does not silently bypass supervisor-owned pauses."""
from __future__ import annotations

import json

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.orchestration import cli, steering
from danus.execution import layout as L


def _project(tmp_path, monkeypatch, *, pause="budget:resource_token_budget"):
    root = tmp_path / "projects"
    pdir = root / "P"
    worker = pdir / "workers" / "low"
    worker.mkdir(parents=True)
    (worker / ".role").write_text("ROLE=researcher\n", encoding="utf-8")
    (worker / ".pause").write_text(pause + "\n", encoding="utf-8")
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    ResearchPlanGraph.create(pdir, node_id="P0", title="Budget pilot")
    ledger = ProgressLedger(pdir)
    record = ledger.assign(
        worker="low", role="researcher", node_id="P0",
        plan_version=1, task="Interpret the evidence",
    )
    calls = []
    monkeypatch.setattr(steering, "do_status", lambda target: [{
        "worker": "low", "pause_reason": pause,
        "progress_state": "RUNNING", "paused": True,
    }])
    monkeypatch.setattr(cli, "_project_rows", lambda project: [
        {"worker": "low", "usage_complete": True,
         "usage_tokens_total": 472375},
        {"worker": "low2", "usage_complete": False,
         "usage_tokens_total": None},
        {"worker": "low3", "usage_complete": True,
         "usage_tokens_total": 521979},
    ])
    def legacy(target, stagger=0):
        calls.append(target)
        return [{"worker": "low", "result": "started"}]
    monkeypatch.setattr(steering, "_legacy_resume", legacy)
    return pdir, record, calls


def test_budget_pause_stays_protected_without_explicit_ack(tmp_path, monkeypatch):
    pdir, assignment, calls = _project(tmp_path, monkeypatch)
    assert steering.do_resume("P/low") == [{"worker": "low", "result": "system-paused"}]
    assert steering.do_resume("P/low", budget_limit=2_000_000) == [
        {"worker": "low", "result": "budget-usage-incomplete"}
    ]
    assert (pdir / "workers/low/.pause").exists()
    assert not calls
    assert not (pdir / "operator_budget_resumes.jsonl").exists()


def test_operator_may_resume_only_exact_active_worker_under_new_budget(tmp_path, monkeypatch):
    pdir, assignment, calls = _project(tmp_path, monkeypatch)
    result = steering.do_resume(
        "P/low", budget_limit=2_000_000, accept_incomplete_usage=True,
    )
    assert result == [{"worker": "low", "result": "started"}]
    assert calls == ["P/low"]
    audit = [json.loads(line) for line in
             (pdir / "operator_budget_resumes.jsonl").read_text().splitlines()]
    assert len(audit) == 1
    assert audit[0]["assignment_id"] == assignment["assignment_id"]
    assert audit[0]["new_cumulative_budget"] == 2_000_000
    assert audit[0]["known_usage_lower_bound"] == 994354
    assert audit[0]["unknown_workers"] == ["low2"]
    assert audit[0]["accepted_incomplete_usage"] is True
    assert ProgressLedger(pdir).current("low", refresh_stale=False)["assignment_id"] == assignment["assignment_id"]


def test_real_lower_bound_exceedance_must_stay_paused(tmp_path, monkeypatch):
    pdir, assignment, calls = _project(tmp_path, monkeypatch)
    assert steering.do_resume(
        "P/low", budget_limit=900000, accept_incomplete_usage=True,
    ) == [{"worker": "low", "result": "budget-exceeded"}]
    assert not calls
    assert not (pdir / "operator_budget_resumes.jsonl").exists()


def test_review_pause_cannot_be_overridden_as_budget(tmp_path, monkeypatch):
    pdir, assignment, calls = _project(
        tmp_path, monkeypatch, pause="review:assignment_blocked",
    )
    assert steering.do_resume(
        "P/low", budget_limit=2_000_000, accept_incomplete_usage=True,
    ) == [{"worker": "low", "result": "system-paused"}]
    assert not calls


def test_bad_budget_args_rejected_without_side_effects(tmp_path, monkeypatch):
    pdir, assignment, calls = _project(tmp_path, monkeypatch)
    for kwargs in (
        {"budget_limit": 0},
        {"budget_limit": -1},
        {"budget_limit": 2_000_000, "target": "P"},
        {"accept_incomplete_usage": True},
    ):
        target = kwargs.pop("target", "P/low")
        with pytest.raises(ValueError):
            steering.do_resume(target, **kwargs)
    assert not calls
    assert not (pdir / "operator_budget_resumes.jsonl").exists()
