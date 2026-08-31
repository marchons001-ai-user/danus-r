"""Offline A2-9a TUI entry, overview, and experiment-panel tests."""

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
from danus.core.evidence import EvidenceStore
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


def _experiment(project, *, with_status=True, completed=2, total=4):
    units = [{"id": f"case-{index}"} for index in range(1, total + 1)]
    protocol_id = EvidenceStore(project).freeze_protocol(
        question="Robustness sweep",
        hypothesis="H",
        matrix={"units": units},
        primary_metrics=["score"],
        decision_rule="compare score",
        author="experimenter",
    )
    runtime = project / "experiment_runtime" / protocol_id
    runtime.mkdir(parents=True)
    checkpoint = {
        "schema_version": 1,
        "protocol_id": protocol_id,
        "completed": {f"case-{index}": [] for index in range(1, completed + 1)},
        "failures": [],
        "evidence_id": None,
    }
    (runtime / "checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    if with_status:
        status = {
            "protocol_id": protocol_id,
            "state": "running",
            "completed_units": completed + 1,
            "total_units": total,
            "current_unit": f"case-{completed + 1}",
            "attempt": 2,
            "unit_started_at": "2026-08-31T12:00:00Z",
            "updated_at": "2026-08-31T12:00:01Z",
        }
        (runtime / "status.json").write_text(json.dumps(status), encoding="utf-8")
    return protocol_id


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
    protocol_id = _experiment(project)
    ledger_before = ledger.path.read_bytes()
    change_path = project / "plan" / "changes" / f"{change_id}.json"
    change_before = change_path.read_bytes()
    runtime = project / "experiment_runtime" / protocol_id
    checkpoint_before = (runtime / "checkpoint.json").read_bytes()
    status_before = (runtime / "status.json").read_bytes()

    snapshot = load_project_snapshot("P")

    assert snapshot["project_state"] == "RUNNING"
    assert snapshot["quota"]["state"] == "UNKNOWN"
    assert ledger.path.read_bytes() == ledger_before
    assert change_path.read_bytes() == change_before
    assert (runtime / "checkpoint.json").read_bytes() == checkpoint_before
    assert (runtime / "status.json").read_bytes() == status_before


def test_textual_overview_runs_headless(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    _worker(project)
    _experiment(project)

    async def check() -> None:
        app = DanusTUI("P")
        async with app.run_test(size=(120, 36)):
            assert app.project_name == "P"
            assert app.snapshot["project_state"] == "RUNNING"
            assert app.query_one("#summary", Static)
            assert app.query_one("#quota", Static)
            tree = app.query_one("#plan", Tree)
            workers = app.query_one("#workers", DataTable)
            experiments = app.query_one("#experiments", DataTable)
            assert "P0" in str(tree.root.label)
            assert workers.row_count == 1
            assert experiments.row_count == 1

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
            "age_s": None,
        }
    ]


def test_snapshot_reports_current_round_age_only_while_running(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    _worker(project)
    started = time.time() - 12
    status_path = project / "workers" / "r1" / ".status.json"
    status_path.write_text(
        json.dumps({"state": "running", "round": 3, "round_started_at": started}),
        encoding="utf-8",
    )
    monkeypatch.setattr("danus.tui.state.C._worker_alive", lambda wl: True)

    row = load_project_snapshot("P")["workers"][0]

    assert row["runtime"] == "working"
    assert 11.0 <= row["age_s"] <= 13.0


def test_snapshot_uses_checkpoint_safe_point_for_live_experiment(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    protocol_id = _experiment(project, completed=2, total=4)

    row = load_project_snapshot("P")["experiments"][0]

    assert row == {
        "protocol_id": protocol_id,
        "label": "Robustness sweep",
        "state": "running",
        "completed_units": 2,
        "total_units": 4,
        "percent": 50.0,
        "current_unit": "case-3",
        "attempt": 2,
        "evidence_id": None,
    }


def test_snapshot_supports_legacy_checkpoint_without_status(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    protocol_id = _experiment(project, with_status=False, completed=1, total=2)

    row = load_project_snapshot("P")["experiments"][0]

    assert row["protocol_id"] == protocol_id
    assert row["state"] == "checkpoint"
    assert row["completed_units"] == 1
    assert row["total_units"] == 2
    assert row["percent"] == 50.0
    assert row["current_unit"] is None


def test_orchestration_dispatches_tui(monkeypatch):
    calls = []
    monkeypatch.setattr(tui, "main", lambda argv: calls.append(list(argv)) or 0)
    monkeypatch.setattr(sys, "argv", ["danus.orchestration", "tui", "P"])

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("danus.orchestration", run_name="__main__")

    assert exc.value.code == 0
    assert calls == [["P"]]
