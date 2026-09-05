"""S5-B governance transaction recovery: durable side effects and bounded replay."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core._util import read_jsonl
from danus.core.handoff import HandoffRequests
from danus.execution import layout as L
from danus.gateway import server
from danus.orchestration import autonomy as A
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


def _finish(project: str, worker: str) -> None:
    ProgressLedger(L.project_dir(project)).update(
        worker=worker,
        state="DONE",
        actor="runtime",
        outcome="COMPLETE",
    )


def _apply(project: str):
    return A.apply_autonomy(
        project,
        [],
        rows=cli._project_rows(project),
        assign=cli.do_assign,
        start=lambda wl: "started",
        role_of=cli._read_worker_role,
    )


def _contract(ledger: ProgressLedger, *, goal: str = "change controller"):
    return ledger.submit_implementation_contract(
        proposer="low",
        node_id="P0",
        plan_version=1,
        goal=goal,
        must_preserve_invariants=["nominal objective"],
        forbidden_behaviors=["bypass review"],
        acceptance_conditions=["targeted test passes"],
    )


def _contract_review(handoffs: HandoffRequests, contract_id: str):
    return next(
        item
        for item in handoffs.all()
        if item["kind"] == "REVIEW"
        and item["subject_type"] == "IMPLEMENTATION_CONTRACT"
        and item["subject_id"] == contract_id
    )


def _write_proposal(path: Path, version: int, node_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
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
                    {"node_id": node_id, "role": "researcher", "task": f"research {node_id}"}
                ],
            }
        ),
        encoding="utf-8",
    )


def _start_plan_review(monkeypatch, project: str = "P"):
    monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
    init = planning.initialize_research(project, brief="Study one bounded question.")
    pdir = L.project_dir(project)
    _finish(project, "low")
    _write_proposal(Path(init["proposal_handoff"]), 1, "R1")
    actions = planning.planning_step(project)
    captured = next(item for item in actions if item["action"] == "PLAN_PROPOSAL")
    handoffs = HandoffRequests(ProgressLedger(pdir))
    review = next(
        item
        for item in handoffs.all()
        if item["kind"] == "REVIEW"
        and item["subject_type"] == "PLAN_CHANGE"
        and item["subject_id"] == captured["change_id"]
    )
    return pdir, captured, review


def _gateway_reviewer(monkeypatch, pdir: Path) -> None:
    monkeypatch.setenv("DANUS_PROJECT_DIR", str(pdir))
    monkeypatch.setenv("DANUS_AUTHOR", "low2")
    monkeypatch.setenv("DANUS_ROLE", "reviewer")


def test_exact_verdict_replay_is_idempotent_and_conflicts_fail_closed(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        planning.initialize_research("P", brief="Study one bounded question.")
        pdir = L.project_dir("P")
        ledger = ProgressLedger(pdir)
        _finish("P", "low")
        contract = _contract(ledger)
        _apply("P")
        _gateway_reviewer(monkeypatch, pdir)

        first = server.implementation_review_contract(
            contract["contract_id"], "APPROVE", "bounded and exact"
        )
        review_events_before = [
            event
            for event in read_jsonl(ledger.path)
            if event.get("event") == "IMPLEMENTATION_REVIEW"
            and event.get("contract_id") == contract["contract_id"]
        ]
        replay = server.implementation_review_contract(
            contract["contract_id"], "APPROVE", "bounded and exact"
        )
        review_events_after = [
            event
            for event in read_jsonl(ledger.path)
            if event.get("event") == "IMPLEMENTATION_REVIEW"
            and event.get("contract_id") == contract["contract_id"]
        ]

        assert first["status"] == "APPROVE"
        assert replay["status"] == "APPROVE"
        assert len(review_events_before) == len(review_events_after) == 1
        with pytest.raises(ValueError, match="conflicting replay"):
            server.implementation_review_contract(
                contract["contract_id"], "REJECT", "bounded and exact"
            )

        _finish("P", "low2")
        cli.do_supervise("P", apply=True)
        review = _contract_review(HandoffRequests(ledger), contract["contract_id"])
        assert review["status"] == "RESOLVED"

        pdir2, captured, _ = _start_plan_review(monkeypatch, project="Q")
        _gateway_reviewer(monkeypatch, pdir2)
        first_plan = server.plan_review_change(
            captured["change_id"], rationale="bounded", verdict="APPROVE"
        )
        reviewed_at = ResearchPlanGraph(pdir2).get_change(captured["change_id"])["reviewed_at"]
        replay_plan = server.plan_review_change(
            captured["change_id"], rationale="bounded", verdict="APPROVE"
        )
        assert first_plan["verdict"] == replay_plan["verdict"] == "APPROVE"
        assert ResearchPlanGraph(pdir2).get_change(captured["change_id"])["reviewed_at"] == reviewed_at
        with pytest.raises(ValueError, match="conflicting replay"):
            server.plan_review_change(
                captured["change_id"], rationale="different", verdict="APPROVE"
            )
        _finish("Q", "low2")
        planning.planning_step("Q")
        assert ResearchPlanGraph(pdir2).snapshot()["plan_version"] == 2


def test_missing_contract_review_side_effect_reopens_same_handoff_then_recovers(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        planning.initialize_research("P", brief="Study one bounded question.")
        pdir = L.project_dir("P")
        ledger = ProgressLedger(pdir)
        handoffs = HandoffRequests(ledger)
        _finish("P", "low")
        contract = _contract(ledger)
        _apply("P")
        review = _contract_review(handoffs, contract["contract_id"])
        first_assignment = review["assigned_assignment_id"]

        _finish("P", "low2")  # COMPLETE was persisted, required MCP verdict was not.
        cli.do_supervise("P", apply=True)
        retried = handoffs.get(review["handoff_id"])
        assert retried and retried["status"] == "ASSIGNED"
        assert retried["handoff_id"] == review["handoff_id"]
        assert retried["attempt"] == 2
        assert retried["assigned_assignment_id"] != first_assignment
        assert len([a for a in ledger.assignments() if a.get("handoff_id") == review["handoff_id"]]) == 2

        _gateway_reviewer(monkeypatch, pdir)
        server.implementation_review_contract(
            contract["contract_id"], "APPROVE", "recovered review"
        )
        _finish("P", "low2")
        cli.do_supervise("P", apply=True)
        assert handoffs.get(review["handoff_id"])["status"] == "RESOLVED"


def test_missing_side_effect_retry_is_bounded_at_two_attempts(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        planning.initialize_research("P", brief="Study one bounded question.")
        pdir = L.project_dir("P")
        ledger = ProgressLedger(pdir)
        handoffs = HandoffRequests(ledger)
        _finish("P", "low")
        contract = _contract(ledger)
        _apply("P")
        review = _contract_review(handoffs, contract["contract_id"])

        _finish("P", "low2")
        cli.do_supervise("P", apply=True)
        assert handoffs.get(review["handoff_id"])["attempt"] == 2

        _finish("P", "low2")
        report = cli.do_supervise("P", apply=True)
        stuck = handoffs.get(review["handoff_id"])
        assert stuck and stuck["status"] == "ASSIGNED" and stuck["attempt"] == 2
        assert len([a for a in ledger.assignments() if a.get("handoff_id") == review["handoff_id"]]) == 2
        assert any(
            action.get("kind") == "WAIT"
            and action.get("handoff_id") == review["handoff_id"]
            and action.get("reason") == "required_side_effect_missing_after_retry"
            for action in report["actions"]
        )


def test_missing_contract_replacement_retries_same_correction_handoff(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        planning.initialize_research("P", brief="Study one bounded question.")
        pdir = L.project_dir("P")
        ledger = ProgressLedger(pdir)
        handoffs = HandoffRequests(ledger)
        _finish("P", "low")
        original = _contract(ledger)
        _apply("P")
        review = _contract_review(handoffs, original["contract_id"])
        ledger.review_implementation_contract(
            original["contract_id"],
            reviewer="low2",
            verdict="REVISE",
            rationale="correct the implementation",
        )
        _finish("P", "low2")
        cli.do_supervise("P", apply=True)
        correction = next(
            item
            for item in handoffs.all()
            if item["kind"] == "REVISION"
            and item["subject_type"] == "IMPLEMENTATION_CONTRACT"
            and item["subject_id"] == original["contract_id"]
        )

        _finish("P", "low")  # COMPLETE without implementation_submit_contract.
        cli.do_supervise("P", apply=True)
        retried = handoffs.get(correction["handoff_id"])
        assert retried and retried["attempt"] == 2 and retried["status"] == "ASSIGNED"

        replacement = _contract(ledger, goal="corrected controller")
        assert replacement["replaces_contract_id"] == original["contract_id"]
        _finish("P", "low")
        cli.do_supervise("P", apply=True)
        assert handoffs.get(correction["handoff_id"])["status"] == "RESOLVED"


def test_missing_plan_replacement_retries_same_correction_handoff(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, captured, review = _start_plan_review(monkeypatch)
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
        planning.planning_step("P")
        correction = next(
            item
            for item in handoffs.all()
            if item["kind"] == "REVISION"
            and item["subject_type"] == "PLAN_CHANGE"
            and item["subject_id"] == captured["change_id"]
        )
        assert correction["parent_handoff_id"] == review["handoff_id"]

        _finish("P", "low")  # COMPLETE without writing plan_proposal.json.
        planning.planning_step("P")
        retried = handoffs.get(correction["handoff_id"])
        assert retried and retried["attempt"] == 2 and retried["status"] == "ASSIGNED"

        _write_proposal(planning._proposal_path(pdir), 1, "R2")
        _finish("P", "low")
        actions = planning.planning_step("P")
        assert handoffs.get(correction["handoff_id"])["status"] == "RESOLVED"
        assert any(item.get("action") == "PLAN_PROPOSAL" for item in actions)
