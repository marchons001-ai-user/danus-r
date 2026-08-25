from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.execution import layout as L
from danus.orchestration import cli


@contextmanager
def _env(tmp_path: Path):
    old = os.environ.get("DANUS_AGENTS_ROOT")
    os.environ["DANUS_AGENTS_ROOT"] = str(tmp_path / "agents")
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("DANUS_AGENTS_ROOT", None)
        else:
            os.environ["DANUS_AGENTS_ROOT"] = old


def _add_node(plan: ResearchPlanGraph, node_id: str) -> None:
    version = plan.snapshot()["plan_version"]
    cid = plan.propose_change(
        base_plan_version=version,
        proposer="researcher",
        reason="add planned direction",
        operations=[
            {"op": "add_node", "node_id": node_id, "title": "Planned direction", "summary": "", "parent": "root", "depends_on": []}
        ],
    )
    plan.review_change(cid, reviewer="reviewer", approve=True, rationale="needed")
    plan.commit_change(cid, committer="supervisor")


def test_planned_assign_requires_node_and_records_binding(tmp_path):
    with _env(tmp_path):
        cli.do_new("P", roles="high:1")
        pdir = L.project_dir("P")
        plan = ResearchPlanGraph.create(pdir, node_id="root", title="Root")
        _add_node(plan, "A")
        wl = L.WorkerLayout(L.worker_dir("P", "high"))
        wl.role.write_text("ROLE=researcher\n", encoding="utf-8")

        with pytest.raises(SystemExit, match="requires --node"):
            cli.do_assign("P/high", "analyze")

        result = cli.do_assign("P/high", "analyze", node_id="A")
        current = ProgressLedger(pdir).current("high")
        assert result["assignment_id"] == current["assignment_id"]
        assert result["plan_version"] == plan.snapshot()["plan_version"]
        assert current["role"] == "researcher"
        assert current["node_id"] == "A"
        assert current["task_sha256"] == ProgressLedger.task_sha256(wl.task.read_text(encoding="utf-8"))

        status = cli.worker_status(wl)
        assert status["assignment_id"] == current["assignment_id"]
        assert status["plan_node_id"] == "A"
        assert status["progress_state"] == "ASSIGNED"


def test_invalid_binding_restores_previous_task(tmp_path):
    with _env(tmp_path):
        cli.do_new("P", roles="high:1")
        pdir = L.project_dir("P")
        ResearchPlanGraph.create(pdir, node_id="root", title="Root")
        wl = L.WorkerLayout(L.worker_dir("P", "high"))
        wl.task.write_text("keep me\n", encoding="utf-8")

        with pytest.raises(SystemExit, match="unknown plan node"):
            cli.do_assign("P/high", "replacement", node_id="missing")
        assert wl.task.read_text(encoding="utf-8") == "keep me\n"
        assert not ProgressLedger(pdir).path.exists()


def test_legacy_project_keeps_unbound_assign_behavior(tmp_path):
    with _env(tmp_path):
        cli.do_new("P", roles="high:1")
        result = cli.do_assign("P/high", "legacy task")
        assert "assignment_id" not in result
        assert L.WorkerLayout(L.worker_dir("P", "high")).task.read_text(encoding="utf-8") == "legacy task\n"
        with pytest.raises(SystemExit, match="no Research Plan"):
            cli.do_assign("P/high", "x", node_id="A")


def test_assign_parser_accepts_plan_node():
    args = cli.build_parser().parse_args(["assign", "P/high", "--task", "x", "--node", "A"])
    assert args.node == "A"
