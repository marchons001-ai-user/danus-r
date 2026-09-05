from __future__ import annotations

from pathlib import Path

from danus.core._util import read_jsonl
from danus.core.handoff import HandoffRequests
from danus.execution import context as C
from danus.execution import loop as LOOP
from danus.orchestration import autonomy as AUTO
from danus.orchestration import cli as CLI
from danus.orchestration import supervisor_llm as S
from danus.orchestration.control import build_supervisor_snapshot, reduce_supervisor_snapshot
from danus.orchestration.tests.test_s6_scientific_route_closure import (
    _ambiguity,
    _bind_layout,
    _luna,
)


def test_s6_final_acceptance_smoke(tmp_path: Path, monkeypatch):
    """Exercise one complete reviewed-ambiguity route through final root closure."""
    _bind_layout(monkeypatch, tmp_path)
    ledger, source, action = _ambiguity(tmp_path)

    task = "investigate the bounded scientific question\n"
    task_spec = tmp_path / "tasks" / "v1" / "experimenter" / "root.md"
    task_spec.parent.mkdir(parents=True)
    task_spec.write_text(task, encoding="utf-8")

    calls = []
    monkeypatch.setattr(
        S,
        "_run_luna",
        lambda prompt: calls.append(prompt) or _luna("INTERPRET"),
    )

    first = S.resolve_scientific_routes(tmp_path, [action])[0]
    second = S.resolve_scientific_routes(tmp_path, [action])[0]
    assert first["status"] == "valid"
    assert first["route"] == "INTERPRET"
    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["decision_key"] == first["decision_key"]
    assert second["handoff_id"] == first["handoff_id"]
    assert len(calls) == 1

    handoffs = HandoffRequests(ledger)
    route = handoffs.get(first["handoff_id"])
    assert route is not None
    assert route["status"] == "OPEN"
    assert "SOURCE: metrics conflict" in route["request"]
    assert "REVIEW: evidence valid, conclusion ambiguous" in route["request"]

    researcher_dir = tmp_path / "workers" / "researcher"
    researcher_dir.mkdir(parents=True, exist_ok=True)
    (researcher_dir / "TASK.md").write_text(route["request"], encoding="utf-8")
    route_assignment = ledger.assign(
        worker="researcher",
        role="researcher",
        node_id="root",
        plan_version=1,
        task=route["request"],
        handoff_id=route["handoff_id"],
    )
    ledger.update(worker="researcher", state="DONE", actor="runtime", outcome="COMPLETE")
    C.write_checkpoint(
        researcher_dir,
        role="researcher",
        round_no=1,
        telemetry={
            "last_agent_message": "INTERPRET RESULT: evidence supports only the bounded claim\nDANUS_OUTCOME: COMPLETE"
        },
    )
    handoffs.resolve(
        route["handoff_id"],
        assignment_id=route_assignment["assignment_id"],
        actor="supervisor",
        summary="scientific route completed",
    )

    monkeypatch.setattr(CLI, "_start_one", lambda wl: "started")
    disposition = S.apply_scientific_route_dispositions("p")
    assert disposition == [
        {
            "handoff_id": route["handoff_id"],
            "source_assignment_id": source["assignment_id"],
            "result": "resumed",
            "start_result": "started",
        }
    ]
    current = ledger.current("experimenter", refresh_stale=False)
    assert current is not None
    assert current["assignment_id"] == source["assignment_id"]
    assert current["state"] == "RUNNING"

    routed_context = LOOP._resolved_scientific_route_context("p", source["assignment_id"])
    assert route["handoff_id"] in routed_context
    assert "evidence supports only the bounded claim" in routed_context

    ledger.update(worker="experimenter", state="DONE", actor="runtime", outcome="COMPLETE")
    report = reduce_supervisor_snapshot(build_supervisor_snapshot(tmp_path, []))
    assert any(
        item.get("action") == "REVIEW" and item.get("reason") == "completion_not_recorded"
        for item in report["decisions"]
    )

    def unexpected_assign(*args, **kwargs):
        raise AssertionError("final acceptance must not need a new assignment")

    applied = AUTO.apply_autonomy(
        "p",
        report["decisions"],
        rows=[],
        assign=unexpected_assign,
        start=lambda wl: "started",
        role_of=lambda wl: "experimenter",
    )
    assert any(
        item.get("action") == "AUTO_COMPLETE"
        and item.get("node_id") == "root"
        and item.get("result") == "completed"
        for item in applied
    )

    final = reduce_supervisor_snapshot(build_supervisor_snapshot(tmp_path, []))
    assert final["project_state"] == "COMPLETE"
    assert "root" in ledger.completed_nodes()
    assert not [
        item
        for item in handoffs.all()
        if item.get("status") in {"OPEN", "ASSIGNED"}
    ]
    assert len(
        [
            event
            for event in read_jsonl(ledger.path)
            if event.get("event") == "SUPERVISOR_ROUTE_DECIDED"
        ]
    ) == 1
