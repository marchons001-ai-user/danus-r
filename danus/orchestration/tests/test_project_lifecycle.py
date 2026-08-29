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

        rows = cli.do_status("P")
        assert rows and all(row["paused"] for row in rows)
        first = reconcile(pdir, rows)
        assert first["project_state"] == "PAUSED"
        assert first["decisions"] == []

        # A fresh read represents a new Supervisor process: no in-memory lifecycle
        # state is needed; the existing per-worker pause markers are the durable fact.
        second = reconcile(pdir, cli.do_status("P"))
        assert second["project_state"] == "PAUSED"
        assert second["decisions"] == []
    finally:
        ctx.__exit__(None, None, None)


def test_project_resume_restores_running_without_new_state_store(tmp_path: Path, monkeypatch):
    ctx, pdir = _project(tmp_path)
    try:
        cli.do_pause("P")
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        result = cli.do_resume("P", stagger=0)
        assert {row["result"] for row in result} == {"started"}

        rows = cli.do_status("P")
        assert rows and not any(row["paused"] for row in rows)
        report = reconcile(pdir, rows)
        assert report["project_state"] == "RUNNING"
    finally:
        ctx.__exit__(None, None, None)


def test_complete_has_priority_over_pause_markers(tmp_path: Path):
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
