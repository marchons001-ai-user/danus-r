from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.execution import layout as L
from danus.orchestration import cli, planning
from danus.orchestration.control import reconcile


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


def _finish(project: str, worker: str) -> None:
    ProgressLedger(L.project_dir(project)).update(
        worker=worker,
        state="DONE",
        actor="runtime",
        outcome="COMPLETE",
    )


def _write_proposal(path: Path, version: int, node_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "assignment_id": ProgressLedger(path.parents[1]).current("low")["assignment_id"],
                "base_plan_version": version,
                "reason": f"bounded route {node_id}",
                "post_hoc": False,
                "evidence_refs": [],
                "operations": [
                    {
                        "op": "add_node",
                        "node_id": node_id,
                        "title": "Research",
                        "summary": "",
                        "parent": "P0",
                        "depends_on": [],
                    }
                ],
                "tasks": [
                    {
                        "node_id": node_id,
                        "role": "researcher",
                        "task": f"research {node_id}",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _start_plan_review(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
    init = planning.initialize_research("P", brief="Study one bounded question.")
    pdir = L.project_dir("P")
    _finish("P", "low")
    _write_proposal(Path(init["proposal_handoff"]), 1, "R1")
    actions = planning.planning_step("P")
    captured = next(item for item in actions if item["action"] == "PLAN_PROPOSAL")
    ledger = ProgressLedger(pdir)
    handoffs = HandoffRequests(ledger)
    review = next(
        item
        for item in handoffs.all()
        if item["kind"] == "REVIEW" and item["subject_id"] == captured["change_id"]
    )
    assignment = ledger.current("low2")
    assert assignment and assignment["handoff_id"] == review["handoff_id"]
    return pdir, captured, review


def test_plan_review_handoff_approves_and_commits_without_duplicate_assignment(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, captured, review = _start_plan_review(tmp_path, monkeypatch)
        ledger = ProgressLedger(pdir)
        handoffs = HandoffRequests(ledger)
        first_assignment = review["assigned_assignment_id"]

        planning.planning_step("P")
        repeated = handoffs.get(review["handoff_id"])
        assert repeated and repeated["assigned_assignment_id"] == first_assignment
        assert len([a for a in ledger.assignments() if a.get("handoff_id") == review["handoff_id"]]) == 1

        ResearchPlanGraph(pdir).review_change(
            captured["change_id"],
            reviewer="low2",
            verdict="APPROVE",
            rationale="bounded",
        )
        _finish("P", "low2")
        actions = planning.planning_step("P")

        assert any(
            item["action"] == "PLAN_COMMIT" and item.get("plan_version") == 2
            for item in actions
        )
        assert handoffs.get(review["handoff_id"])["status"] == "RESOLVED"


def test_plan_revise_creates_child_revision_and_replacement_review(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, captured, review = _start_plan_review(tmp_path, monkeypatch)
        graph = ResearchPlanGraph(pdir)
        ledger = ProgressLedger(pdir)
        handoffs = HandoffRequests(ledger)

        graph.review_change(
            captured["change_id"],
            reviewer="low2",
            verdict="REVISE",
            rationale="narrow the route",
        )
        _finish("P", "low2")
        actions = planning.planning_step("P")
        revision = next(
            item
            for item in handoffs.all()
            if item["kind"] == "REVISION" and item["subject_id"] == captured["change_id"]
        )

        assert handoffs.get(review["handoff_id"])["status"] == "RESOLVED"
        assert revision["parent_handoff_id"] == review["handoff_id"]
        assert revision["status"] == "ASSIGNED"
        assert any(item.get("handoff_kind") == "REVISION" for item in actions)
        current = ledger.current("low")
        assert current and current["handoff_id"] == revision["handoff_id"]

        _finish("P", "low")
        _write_proposal(planning._proposal_path(pdir), 1, "R2")
        actions = planning.planning_step("P")
        assert handoffs.get(revision["handoff_id"])["status"] == "RESOLVED"
        replacement = next(item for item in actions if item["action"] == "PLAN_PROPOSAL")
        next_review = next(
            item
            for item in handoffs.all()
            if item["kind"] == "REVIEW" and item["subject_id"] == replacement["change_id"]
        )
        assert next_review["handoff_id"] != review["handoff_id"]
        assert next_review["status"] == "ASSIGNED"


def test_plan_reject_creates_child_rethink(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, captured, review = _start_plan_review(tmp_path, monkeypatch)
        graph = ResearchPlanGraph(pdir)
        handoffs = HandoffRequests(ProgressLedger(pdir))

        graph.review_change(
            captured["change_id"],
            reviewer="low2",
            verdict="REJECT",
            rationale="route is scientifically wrong",
        )
        _finish("P", "low2")
        actions = planning.planning_step("P")
        rethink = next(
            item
            for item in handoffs.all()
            if item["kind"] == "RETHINK" and item["subject_id"] == captured["change_id"]
        )

        assert handoffs.get(review["handoff_id"])["status"] == "RESOLVED"
        assert rethink["parent_handoff_id"] == review["handoff_id"]
        assert rethink["status"] == "ASSIGNED"
        assert any(item.get("handoff_kind") == "RETHINK" for item in actions)


def test_invalid_revision_proposal_stales_handoff_before_legacy_repair(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, captured, _ = _start_plan_review(tmp_path, monkeypatch)
        graph = ResearchPlanGraph(pdir)
        ledger = ProgressLedger(pdir)
        handoffs = HandoffRequests(ledger)

        graph.review_change(
            captured["change_id"],
            reviewer="low2",
            verdict="REVISE",
            rationale="repair locally",
        )
        _finish("P", "low2")
        planning.planning_step("P")
        revision = next(item for item in handoffs.all() if item["kind"] == "REVISION")
        _finish("P", "low")

        path = planning._proposal_path(pdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "assignment_id": ProgressLedger(path.parents[1]).current("low")["assignment_id"],
                    "base_plan_version": 1,
                    "reason": "invalid replacement",
                    "operations": [
                        {
                            "op": "add_node",
                            "node_id": "R2",
                            "title": "Research",
                            "summary": "x" * 101,
                            "parent": "P0",
                            "depends_on": [],
                        }
                    ],
                    "tasks": [{"node_id": "R2", "role": "researcher", "task": "research"}],
                }
            ),
            encoding="utf-8",
        )
        actions = planning.planning_step("P")

        assert handoffs.get(revision["handoff_id"])["status"] == "STALE"
        assert handoffs.get(revision["handoff_id"])["status"] != "RESOLVED"
        assert any(item["action"] == "PLAN_REPAIR" for item in actions)


def test_pending_handoff_prevents_false_project_complete(tmp_path: Path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="P0", title="Root")
    ledger = ProgressLedger(tmp_path)
    ledger.set_node_state("P0", state="DONE", actor="supervisor")
    handoffs = HandoffRequests(ledger)
    request = handoffs.open(
        kind="REVIEW",
        plan_version=plan.snapshot()["plan_version"],
        node_id="P0",
        subject_type="PLAN_NODE",
        subject_id="P0",
        requested_by="researcher",
        source_role="researcher",
        request="Review root closure.",
        reason="closure review",
    )

    blocked = reconcile(tmp_path, [])
    assert blocked["project_state"] == "RUNNING"
    assert blocked["pending_handoffs"] == [
        {
            "handoff_id": request["handoff_id"],
            "kind": "REVIEW",
            "status": "OPEN",
            "node_id": "P0",
            "target_role": "reviewer",
        }
    ]

    handoffs.cancel(request["handoff_id"], actor="main", reason="test complete")
    complete = reconcile(tmp_path, [])
    assert complete["project_state"] == "COMPLETE"
    assert complete["pending_handoffs"] == []
