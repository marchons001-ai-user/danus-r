"""A2-8c autonomous planning regressions. No LLM/network."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.execution import layout as L
from danus.orchestration import cli, planning


@contextmanager
def _root(tmp_path: Path):
    old = os.environ.get("DANUS_AGENTS_ROOT")
    os.environ["DANUS_AGENTS_ROOT"] = str(tmp_path / "projects")
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("DANUS_AGENTS_ROOT", None)
        else:
            os.environ["DANUS_AGENTS_ROOT"] = old


def _finish(project: str, worker: str):
    ProgressLedger(L.project_dir(project)).update(
        worker=worker, state="DONE", actor="runtime", outcome="COMPLETE"
    )


def _proposal(path: Path, version: int, suffix: str = "1"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "assignment_id": ProgressLedger(path.parents[1]).current("low")["assignment_id"],
        "base_plan_version": version,
        "reason": f"minimal route {suffix}",
        "post_hoc": False,
        "evidence_refs": [],
        "operations": [
            {"op": "add_node", "node_id": f"R{suffix}", "title": "Research", "summary": "", "parent": "P0", "depends_on": []},
            {"op": "add_node", "node_id": f"V{suffix}", "title": "Review", "summary": "", "parent": "P0", "depends_on": [f"R{suffix}"]},
        ],
        "tasks": [
            {"node_id": f"R{suffix}", "role": "researcher", "task": "research task"},
            {"node_id": f"V{suffix}", "role": "reviewer", "task": "review task"},
        ],
    }), encoding="utf-8")


def _invalid_proposal(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "assignment_id": ProgressLedger(path.parents[1]).current("low")["assignment_id"],
        "base_plan_version": 1,
        "reason": "minimal route",
        "post_hoc": False,
        "evidence_refs": [],
        "operations": [{
            "op": "add_node", "node_id": "R1", "title": "Research",
            "summary": "x" * 101, "parent": "P0", "depends_on": [],
        }],
        "tasks": [{"node_id": "R1", "role": "researcher", "task": "research task"}],
    }), encoding="utf-8")


def test_brief_proposal_review_commit_materializes_tasks(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        init = planning.initialize_research("P", brief="Study a bounded scientific question.")
        assert init["created"] is True
        pdir = L.project_dir("P")
        assert ResearchPlanGraph(pdir).snapshot()["plan_version"] == 1
        _finish("P", "low")
        _proposal(Path(init["proposal_handoff"]), 1)

        actions = planning.planning_step("P")
        captured = next(x for x in actions if x["action"] == "PLAN_PROPOSAL")
        change = ResearchPlanGraph(pdir).get_change(captured["change_id"])
        assert change["status"] == "PROPOSED"
        review_assignment = ProgressLedger(pdir).current("low2")
        assert review_assignment and review_assignment["role"] == "reviewer"

        ResearchPlanGraph(pdir).review_change(
            captured["change_id"], reviewer="low2", approve=True, rationale="bounded and minimal"
        )
        _finish("P", "low2")
        actions = planning.planning_step("P")
        committed = next(x for x in actions if x["action"] == "PLAN_COMMIT")
        assert committed["plan_version"] == 2
        assert (pdir / "tasks" / "v2" / "researcher" / "R1.md").is_file()
        assert (pdir / "tasks" / "v2" / "reviewer" / "V1.md").is_file()
        assert "Autonomous Plan Revision" in (pdir / "tasks" / "v2" / "researcher" / "R1.md").read_text()

        report = cli.do_supervise("P", apply=True)
        assert any(x.get("node_id") == "R1" and x.get("result") == "assigned" for x in report["autonomy_applied"])


def test_rejected_proposal_routes_revision_to_researcher(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        init = planning.initialize_research("P", brief="Study a bounded scientific question.")
        pdir = L.project_dir("P")
        _finish("P", "low")
        _proposal(Path(init["proposal_handoff"]), 1)
        actions = planning.planning_step("P")
        captured = next(x for x in actions if x["action"] == "PLAN_PROPOSAL")
        ResearchPlanGraph(pdir).review_change(
            captured["change_id"], reviewer="low2", approve=False, rationale="too broad"
        )
        _finish("P", "low2")
        actions = planning.planning_step("P")
        revision = next(x for x in actions if x.get("role") == "researcher")
        assert revision["result"] == "assigned"
        current = ProgressLedger(pdir).current("low")
        assert current and current["state"] == "ASSIGNED"
        assert "Revise Autonomous Research Plan" in L.WorkerLayout(L.worker_dir("P", "low")).task.read_text()


def test_invalid_proposal_routes_deterministic_repair_to_researcher(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        init = planning.initialize_research("P", brief="Study a bounded scientific question.")
        pdir = L.project_dir("P")
        _finish("P", "low")
        path = Path(init["proposal_handoff"])
        _invalid_proposal(path)

        actions = planning.planning_step("P")
        blocked = next(x for x in actions if x["action"] == "PLAN_PROPOSAL")
        repair = next(x for x in actions if x["action"] == "PLAN_REPAIR")
        assert blocked["result"] == "blocked"
        assert "summary exceeds 100 characters" in blocked["reason"]
        assert repair["result"] == "assigned"
        assert repair["attempt"] == 1
        current = ProgressLedger(pdir).current("low")
        assert current and current["state"] == "ASSIGNED"
        task = L.WorkerLayout(L.worker_dir("P", "low")).task.read_text()
        repair_error = json.loads(planning._repair_error_path(pdir).read_text(encoding="utf-8"))
        assert "summary exceeds 100 characters" in repair_error["reason"]
        assert "`PLAN_REPAIR_ERROR.json`" in task
        assert "summary` is at most 100 characters" in task
        assert ResearchPlanGraph(pdir).pending_changes() == []


def test_repair_retries_once_then_stops_spending(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        init = planning.initialize_research("P", brief="Study a bounded scientific question.")
        _finish("P", "low")
        path = Path(init["proposal_handoff"])
        _invalid_proposal(path)

        first = next(x for x in planning.planning_step("P") if x["action"] == "PLAN_REPAIR")
        assert first["result"] == "assigned" and first["attempt"] == 1
        _finish("P", "low")

        second = next(x for x in planning.planning_step("P") if x["action"] == "PLAN_REPAIR")
        assert second["result"] == "assigned" and second["attempt"] == 2
        assert second["assignment_id"] != first["assignment_id"]
        _finish("P", "low")

        third = next(x for x in planning.planning_step("P") if x["action"] == "PLAN_REPAIR")
        assert third["result"] == "repair_exhausted"
        assert third["assignment_id"] == second["assignment_id"]
        repairs = [
            a for a in ProgressLedger(L.project_dir("P")).assignments()
            if a.get("task_sha256") == ProgressLedger.task_sha256(planning._repair_task(L.project_dir("P")))
        ]
        assert len(repairs) == 2


def test_proposal_requires_exact_tasks_for_changed_leaves(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        init = planning.initialize_research("P", brief="Study a bounded scientific question.")
        pdir = L.project_dir("P")
        _finish("P", "low")
        path = Path(init["proposal_handoff"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "assignment_id": ProgressLedger(path.parents[1]).current("low")["assignment_id"],
            "base_plan_version": 1,
            "reason": "missing task",
            "operations": [{"op": "add_node", "node_id": "R1", "title": "Research", "summary": "", "parent": "P0", "depends_on": []}],
            "tasks": [],
        }), encoding="utf-8")
        actions = planning.planning_step("P")
        blocked = next(x for x in actions if x["action"] == "PLAN_PROPOSAL")
        assert blocked["result"] == "blocked"
        assert any(x["action"] == "PLAN_REPAIR" for x in actions)
        assert ResearchPlanGraph(pdir).pending_changes() == []

def test_plan_review_and_revision_tasks_reference_readonly_inputs(tmp_path: Path):
    with _root(tmp_path):
        pdir = L.project_dir("P")
        ResearchPlanGraph.create(pdir, node_id="P0", title="Root")
        change = {"change_id": "change-fixture", "base_plan_version": 1,
                  "review_rationale": "Clarify the exact task"}
        review = planning._review_task(pdir, change, "proposal-fixture")
        revision = planning._revision_task(pdir, change, "proposal-fixture")
        rethink = planning._rethink_task(pdir, change, "proposal-fixture")
        repair = planning._repair_task(pdir)

        for task in (review, revision, rethink):
            assert "PLAN_PROPOSAL.json" in task
            assert "RESEARCH_PLAN.json" in task
            assert str(pdir / "planning" / "proposals") not in task
            assert str(pdir / "plan" / "current.json") not in task
        assert "PLAN_REPAIR_ERROR.json" in repair
        assert str(planning._repair_error_path(pdir)) not in repair
