"""Offline A2-9a-4 Live Activity tests."""
from __future__ import annotations

import asyncio
import json

from textual.widgets import DataTable

from danus.core import ResearchPlanGraph
from danus.core.evidence import EvidenceStore
from danus.tui.app import DanusTUI
from danus.tui.state import load_project_snapshot


def _project(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    project = root / "P"
    (project / "workers").mkdir(parents=True)
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    ResearchPlanGraph.create(project, node_id="P0", title="Research Project")
    return project


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def test_snapshot_aggregates_public_activity_sources_without_mutation(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)

    worker = project / "workers" / "r1"
    (worker / "logs").mkdir(parents=True)
    (worker / ".role").write_text("ROLE=researcher\n", encoding="utf-8")
    round_log = worker / "logs" / "round_1.log"
    _write_jsonl(
        round_log,
        [
            {
                "type": "item.started",
                "timestamp": "2026-08-31T12:00:00Z",
                "item": {
                    "id": "tool-1",
                    "type": "mcp_tool_call",
                    "server": "danus",
                    "tool": "fact_search",
                },
            },
            {
                "type": "item.completed",
                "timestamp": "2026-08-31T12:00:01Z",
                "item": {
                    "id": "msg-1",
                    "type": "agent_message",
                    "text": "Checking the frozen evidence before revising the hypothesis.",
                },
            },
        ],
    )

    supervisor_path = project / "supervisor" / "events.jsonl"
    _write_jsonl(
        supervisor_path,
        [{
            "event": "RECONCILE",
            "at": "2026-08-31T12:00:02Z",
            "applied": [],
            "autonomy_applied": [{
                "action": "AUTO_DISPATCH",
                "node_id": "R1",
                "worker": "r1",
                "result": "assigned",
            }],
        }],
    )

    graph = ResearchPlanGraph(project)
    change_id = graph.propose_change(
        base_plan_version=1,
        proposer="researcher",
        reason="add evidence analysis",
        operations=[{
            "op": "add_node",
            "node_id": "R1",
            "title": "Analyze evidence",
            "summary": "",
            "parent": "P0",
            "depends_on": [],
        }],
    )
    change_path = project / "plan" / "changes" / f"{change_id}.json"

    store = EvidenceStore(project)
    protocol_id = store.freeze_protocol(
        question="Does the intervention improve the metric?",
        hypothesis="It improves the metric.",
        matrix={"units": []},
        primary_metrics=["score"],
        decision_rule="compare score",
        author="experimenter",
    )
    evidence_id = store.add_record(protocol_id=protocol_id, author="experimenter")
    protocol_path = store.protocols_dir / f"{protocol_id}.json"
    evidence_path = store.records_dir / f"{evidence_id}.json"

    experiment_path = project / "experiment_runtime" / protocol_id / "events.jsonl"
    _write_jsonl(
        experiment_path,
        [{
            "event": "UNIT_STARTED",
            "protocol_id": protocol_id,
            "completed_units": 0,
            "total_units": 2,
            "unit_id": "case-1",
            "attempt": 1,
            "at": "2026-08-31T12:00:03Z",
        }],
    )

    before = {
        path: path.read_bytes()
        for path in (
            round_log,
            supervisor_path,
            change_path,
            protocol_path,
            evidence_path,
            experiment_path,
        )
    }

    activity = load_project_snapshot("P")["activity"]

    sources = {row["source"] for row in activity}
    assert {"MODEL", "TOOL", "SUPERVISOR", "PLAN", "EVIDENCE", "EXPERIMENT"} <= sources
    assert any(row["role"] == "researcher" and row["source"] == "MODEL" for row in activity)
    assert any("fact_search" in row["message"] for row in activity if row["source"] == "TOOL")
    assert all(path.read_bytes() == content for path, content in before.items())


def test_refresh_reloads_activity_without_running_control_plane(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    supervisor_path = project / "supervisor" / "events.jsonl"

    async def check() -> None:
        app = DanusTUI("P")
        async with app.run_test(size=(120, 40)):
            table = app.query_one("#activity", DataTable)
            assert app.snapshot["activity"] == []
            assert table.row_count == 1

            _write_jsonl(
                supervisor_path,
                [{
                    "event": "RECONCILE",
                    "at": "2026-08-31T12:00:04Z",
                    "applied": [{
                        "action": "CONTINUE",
                        "node_id": "P0",
                        "worker": "r1",
                        "result": "continued",
                    }],
                    "autonomy_applied": [],
                }],
            )
            source_before = supervisor_path.read_bytes()

            app._refresh_snapshot()

            assert len(app.snapshot["activity"]) == 1
            assert table.row_count == 1
            assert supervisor_path.read_bytes() == source_before

    asyncio.run(check())
