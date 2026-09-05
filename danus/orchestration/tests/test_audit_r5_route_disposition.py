from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.execution import layout as L
from danus.orchestration import cli as CLI
from danus.orchestration import supervisor_llm as S
from danus.orchestration.tests.test_s6_scientific_route_closure import _resolved_route


def _allow_control(monkeypatch) -> None:
    monkeypatch.setattr(
        CLI,
        "_do_supervise_unlocked",
        lambda project, **kwargs: {"project_state": "RUNNING", "budget_stop": None},
    )


def _commit_change(graph: ResearchPlanGraph, operations: list[dict]) -> int:
    version = graph.snapshot()["plan_version"]
    change_id = graph.propose_change(
        base_plan_version=version,
        proposer="planner",
        reason="audit R5 compatibility probe",
        operations=operations,
    )
    graph.review_change(
        change_id,
        reviewer="reviewer",
        verdict="APPROVE",
        rationale="bounded audit change",
    )
    return graph.commit_change(change_id, committer="supervisor")


def test_review_pause_is_cleared_before_experimenter_resume(tmp_path: Path, monkeypatch):
    ledger, source, _, _, _ = _resolved_route(tmp_path, monkeypatch)
    _allow_control(monkeypatch)
    wl = L.WorkerLayout(tmp_path / "workers" / "experimenter")
    wl.pause.write_text("review:assignment_needs_review\n", encoding="utf-8")
    starts = []

    def start(worker_layout):
        assert not worker_layout.pause.exists()
        starts.append(worker_layout.name)
        return "started"

    monkeypatch.setattr(CLI, "_start_one", start)
    result = S.apply_scientific_route_dispositions("p")

    current = ledger.current("experimenter", refresh_stale=False)
    assert result[0]["result"] == "resumed"
    assert result[0]["start_result"] == "started"
    assert current is not None
    assert current["assignment_id"] == source["assignment_id"]
    assert current["state"] == "RUNNING"
    assert not wl.pause.exists()
    assert starts == ["experimenter"]


@pytest.mark.parametrize(
    "pause_reason",
    ["manual", "quota:pressure:test", "budget:resource_token_budget"],
)
def test_foreign_worker_pause_is_never_cleared_or_resumed(
    tmp_path: Path, monkeypatch, pause_reason: str
):
    ledger, source, _, _, _ = _resolved_route(tmp_path, monkeypatch)
    _allow_control(monkeypatch)
    wl = L.WorkerLayout(tmp_path / "workers" / "experimenter")
    wl.pause.write_text(pause_reason + "\n", encoding="utf-8")
    monkeypatch.setattr(CLI, "_start_one", lambda wl: pytest.fail("paused worker must not start"))

    result = S.apply_scientific_route_dispositions("p")
    current = ledger.current("experimenter", refresh_stale=False)

    assert result[0]["result"] == "paused"
    assert result[0]["reason"] == pause_reason
    assert current is not None
    assert current["assignment_id"] == source["assignment_id"]
    assert current["state"] == "BLOCKED"
    assert current["outcome"] == "NEEDS_REVIEW"
    assert wl.pause.read_text(encoding="utf-8").strip() == pause_reason


def test_manual_researcher_pause_blocks_node_completion(tmp_path: Path, monkeypatch):
    ledger, _, _, _, _ = _resolved_route(
        tmp_path,
        monkeypatch,
        source_role="researcher",
        route="RETHINK",
    )
    _allow_control(monkeypatch)
    wl = L.WorkerLayout(tmp_path / "workers" / "researcher")
    wl.pause.write_text("manual\n", encoding="utf-8")

    result = S.apply_scientific_route_dispositions("p")

    assert result[0]["result"] == "paused"
    assert result[0]["reason"] == "manual"
    assert "root" not in ledger.completed_nodes()
    assert wl.pause.read_text(encoding="utf-8").strip() == "manual"


def test_project_pause_blocks_researcher_route_completion(tmp_path: Path, monkeypatch):
    ledger, _, _, handoff, _ = _resolved_route(
        tmp_path,
        monkeypatch,
        source_role="researcher",
        route="RETHINK",
    )
    (tmp_path / L.PAUSE_FILE).write_text("manual\n", encoding="utf-8")

    result = S.apply_scientific_route_dispositions("p")

    assert result == [{
        "handoff_id": handoff["handoff_id"],
        "source_assignment_id": handoff["subject_id"],
        "result": "blocked",
        "reason": "project_state:PAUSED",
    }]
    assert "root" not in ledger.completed_nodes()


def test_deadline_blocks_researcher_route_completion(tmp_path: Path, monkeypatch):
    ledger, _, _, _, _ = _resolved_route(
        tmp_path,
        monkeypatch,
        source_role="researcher",
        route="RETHINK",
    )
    (tmp_path / L.DEADLINE_FILE).write_text(str(time.time() - 1), encoding="utf-8")

    result = S.apply_scientific_route_dispositions("p")

    assert result[0]["result"] == "blocked"
    assert result[0]["reason"] == "resource_time_budget"
    assert "root" not in ledger.completed_nodes()


def test_token_budget_blocks_researcher_route_completion(tmp_path: Path, monkeypatch):
    ledger, _, _, _, _ = _resolved_route(
        tmp_path,
        monkeypatch,
        source_role="researcher",
        route="RETHINK",
    )
    status = tmp_path / "workers" / "researcher" / L.STATUS_FILE
    status.write_text(
        json.dumps({"state": "stopped", "round": 1, "context_tokens_total": 120}),
        encoding="utf-8",
    )

    result = S.apply_scientific_route_dispositions("p", token_budget=100)

    assert result[0]["result"] == "blocked"
    assert result[0]["reason"] == "resource_token_budget"
    assert "root" not in ledger.completed_nodes()


def test_unrelated_plan_revision_keeps_route_consumable(tmp_path: Path, monkeypatch):
    ledger, source, route_assignment, _, _ = _resolved_route(tmp_path, monkeypatch)
    _allow_control(monkeypatch)
    graph = ResearchPlanGraph(tmp_path)
    assert _commit_change(
        graph,
        [{
            "op": "add_node",
            "node_id": "other",
            "title": "Other",
            "summary": "unrelated branch",
            "parent": "root",
            "depends_on": [],
        }],
    ) == 2
    assert not ledger._is_plan_stale(source)
    assert not ledger._is_plan_stale(route_assignment)
    starts = []
    monkeypatch.setattr(CLI, "_start_one", lambda wl: starts.append(wl.name) or "started")

    result = S.apply_scientific_route_dispositions("p")
    current = ledger.current("experimenter", refresh_stale=False)

    assert result[0]["result"] == "resumed"
    assert current is not None
    assert current["assignment_id"] == source["assignment_id"]
    assert current["state"] == "RUNNING"
    assert starts == ["experimenter"]


def test_changed_source_node_is_explicitly_stale_not_resumed(tmp_path: Path, monkeypatch):
    ledger, source, _, _, _ = _resolved_route(tmp_path, monkeypatch)
    _allow_control(monkeypatch)
    graph = ResearchPlanGraph(tmp_path)
    assert _commit_change(
        graph,
        [{"op": "set_state", "node_id": "root", "state": "SUPERSEDED"}],
    ) == 2
    monkeypatch.setattr(CLI, "_start_one", lambda wl: pytest.fail("stale source must not start"))

    result = S.apply_scientific_route_dispositions("p")
    source_after = next(
        item for item in ledger.assignments() if item["assignment_id"] == source["assignment_id"]
    )

    assert result[0]["result"] == "stale"
    assert result[0]["reason"] == "source_plan_binding_stale"
    assert source_after["state"] == "STALE"
