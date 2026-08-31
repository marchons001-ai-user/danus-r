"""Offline A2-9a TUI entry and read-only overview tests."""

from __future__ import annotations

import asyncio
import json
import runpy
import sys
import time

import pytest
from textual.widgets import DataTable, Static, Tree

from danus import tui
from danus.core import ProgressLedger, ResearchPlanGraph
from danus.tui.app import DanusTUI
from danus.tui.state import load_project_snapshot


def _project(tmp_path, monkeypatch, name="P"):
    root = tmp_path / "projects"
    project = root / name
    (project / "workers").mkdir(parents=True)
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    ResearchPlanGraph.create(project, node_id="P0", title="Research Project")
    return project


def _worker(project, name="r1"):
    worker = project / "workers" / name
    worker.mkdir()
    (worker / ".role").write_text("ROLE=researcher\n", encoding="utf-8")
    (worker / ".status.json").write_text(
        json.dumps({"state": "stopped", "round": 2, "updated_at": time.time()}),
        encoding="utf-8",
    )
    ProgressLedger(project).assign(
        worker=name,
        role="researcher",
        node_id="P0",
        plan_version=1,
        task="inspect the research problem\n",
    )


def test_tui_main_opens_existing_project(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(DanusTUI, "run", lambda self: seen.append(self.project_name))

    assert tui.main(["P"]) == 0
    assert seen == ["P"]


def test_tui_main_rejects_unknown_project(tmp_path, monkeypatch):
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        tui.main(["missing"])
    assert exc.value.code == 2


def test_project_snapshot_is_read_only(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    ledger = ProgressLedger(project)
    ledger.set_node_state("P0", state="DONE", actor="supervisor")
    assert load_project_snapshot("P")["project_state"] == "COMPLETE"

    graph = ResearchPlanGraph(project)
    change_id = graph.propose_change(
        base_plan_version=1,
        proposer="researcher",
        reason="extend the route",
        operations=[{
            "op": "add_node",
            "node_id": "R1",
            "title": "Follow-up",
            "summary": "",
            "parent": "P0",
            "depends_on": [],
        }],
    )
    ledger_before = ledger.path.read_bytes()
    change_path = project / "plan" / "changes" / f"{change_id}.json"
    change_before = change_path.read_bytes()

    snapshot = load_project_snapshot("P")

    assert snapshot["project_state"] == "RUNNING"
    assert snapshot["quota"]["state"] == "UNKNOWN"
    assert ledger.path.read_bytes() == ledger_before
    assert change_path.read_bytes() == change_before


def test_textual_overview_runs_headless(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    _worker(project)

    async def check() -> None:
        app = DanusTUI("P")
        async with app.run_test(size=(100, 30)):
            assert app.project_name == "P"
            assert app.snapshot["project_state"] == "RUNNING"
            assert app.query_one("#summary", Static)
            assert app.query_one("#quota", Static)
            tree = app.query_one("#plan", Tree)
            table = app.query_one("#workers", DataTable)
            assert "P0" in str(tree.root.label)
            assert table.row_count == 1

    asyncio.run(check())


def test_snapshot_reports_worker_assignment(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    _worker(project)

    snapshot = load_project_snapshot("P")
    assert snapshot["plan_version"] == 1
    assert snapshot["nodes"][0]["state"] == "OPEN"
    assert snapshot["workers"] == [
        {
            "worker": "r1",
            "role": "researcher",
            "runtime": "stopped",
            "node_id": "P0",
            "progress_state": "ASSIGNED",
            "round": 2,
            "age_s": snapshot["workers"][0]["age_s"],
        }
    ]
    assert snapshot["workers"][0]["age_s"] is not None


def test_orchestration_dispatches_tui(monkeypatch):
    calls = []
    monkeypatch.setattr(tui, "main", lambda argv: calls.append(list(argv)) or 0)
    monkeypatch.setattr(sys, "argv", ["danus.orchestration", "tui", "P"])

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("danus.orchestration", run_name="__main__")

    assert exc.value.code == 0
    assert calls == [["P"]]
