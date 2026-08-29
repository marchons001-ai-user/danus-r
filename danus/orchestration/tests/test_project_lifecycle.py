"""A2-8a durable project lifecycle regressions. No LLM or network."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.execution import layout as L
from danus.orchestration import cli
from danus.orchestration.control import reconcile


@contextmanager
def _agents_root(tmp_path: Path):
    old = os.environ.get("DANUS_AGENTS_ROOT")
    os.environ["DANUS_AGENTS_ROOT"] = str(tmp_path / "projects")
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("DANUS_AGENTS_ROOT", None)
        else:
            os.environ["DANUS_AGENTS_ROOT"] = old


def _project(tmp_path: Path):
    ctx = _agents_root(tmp_path)
    ctx.__enter__()
    cli.do_new("P", roles="low:2")
    pdir = L.project_dir("P")
    ResearchPlanGraph.create(pdir, node_id="root", title="Root")
    return ctx, pdir


def test_project_pause_is_durable_and_quiescent(tmp_path: Path):
    ctx, pdir = _project(tmp_path)
    try:
        result = cli.do_pause("P")
        assert {row["result"] for row in result} == {"paused"}
        assert (pdir / L.PAUSE_FILE).read_text(encoding="utf-8").strip() == "manual"

        # Project pause is separate from worker-specific pause state.
        rows = cli.do_status("P")
        assert rows and not any(row["paused"] for row in rows)
        first = reconcile(pdir, rows)
        assert first["project_state"] == "PAUSED"
        assert first["decisions"] == []

        # A fresh read represents a new Supervisor process: the project-root pause
        # marker survives without any in-memory lifecycle state.
        second = reconcile(pdir, cli.do_status("P"))
        assert second["project_state"] == "PAUSED"
        assert second["decisions"] == []
    finally:
        ctx.__exit__(None, None, None)


def test_worker_pause_does_not_pause_whole_project(tmp_path: Path):
    ctx, pdir = _project(tmp_path)
    try:
        cli.do_pause("P/low")
        assert L.WorkerLayout(L.worker_dir("P", "low")).pause.is_file()
        assert not (pdir / L.PAUSE_FILE).exists()
        report = reconcile(pdir, cli.do_status("P"))
        assert report["project_state"] == "RUNNING"
    finally:
        ctx.__exit__(None, None, None)


def test_project_resume_restores_running_without_erasing_worker_pause(tmp_path: Path, monkeypatch):
    ctx, pdir = _project(tmp_path)
    try:
        cli.do_pause("P")
        low = L.WorkerLayout(L.worker_dir("P", "low"))
        low.pause.write_text("manual\n", encoding="utf-8")

        monkeypatch.setattr(cli, "_start_one", lambda wl: "paused" if wl.pause.exists() else "started")
        result = {row["worker"]: row["result"] for row in cli.do_resume("P", stagger=0)}
        assert result == {"low": "paused", "low2": "started"}
        assert not (pdir / L.PAUSE_FILE).exists()
        assert low.pause.is_file()

        report = reconcile(pdir, cli.do_status("P"))
        assert report["project_state"] == "RUNNING"
    finally:
        ctx.__exit__(None, None, None)


def test_complete_has_priority_over_project_pause(tmp_path: Path):
    ctx, pdir = _project(tmp_path)
    try:
        cli.do_pause("P")
        ProgressLedger(pdir).set_node_state("root", state="DONE", actor="supervisor")
        report = reconcile(pdir, cli.do_status("P"))
        assert report["project_state"] == "COMPLETE"
        assert report["frontier"] == []
        assert report["decisions"] == []
    finally:
        ctx.__exit__(None, None, None)
