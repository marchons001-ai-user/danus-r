from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
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


def _finish(ledger: ProgressLedger, worker: str) -> None:
    ledger.update(
        worker=worker,
        state="DONE",
        actor="runtime",
        outcome="COMPLETE",
    )


def test_supervise_returns_post_apply_fixed_point_after_leaf_completion(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        cli.do_new("P", roles="low:1")
        pdir = L.project_dir("P")
        wl = L.WorkerLayout(L.worker_dir("P", "low"))
        wl.role.write_text("ROLE=researcher\n", encoding="utf-8")
        ResearchPlanGraph.create(pdir, node_id="root", title="Root")
        task = "finish bounded leaf\n"
        spec = pdir / "tasks" / "v1" / "researcher" / "root.md"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(task, encoding="utf-8")
        cli.do_assign("P/low", task, node_id="root")
        ledger = ProgressLedger(pdir)
        _finish(ledger, "low")

        report = cli.do_supervise("P", apply=True)

        assert report["project_state"] == "COMPLETE"
        assert report["completed_nodes"] == ["root"]
        assert report["decisions"] == []
        assert report["convergence"]["stable"] is True
        assert report["convergence"]["passes"] < report["convergence"]["max_passes"]
        assert any(item.get("action") == "AUTO_COMPLETE" for item in report["autonomy_applied"])


def test_generic_review_converges_without_duplicate_handoff_or_assignment(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        planning.initialize_research("P", brief="Study one bounded question.")
        pdir = L.project_dir("P")
        ledger = ProgressLedger(pdir)
        _finish(ledger, "low")
        source = cli.do_assign("P/low3", "inspect ambiguous result", node_id="P0")
        ledger.update(
            worker="low3",
            state="BLOCKED",
            actor="runtime",
            note="independent scientific judgment required",
            outcome="NEEDS_REVIEW",
        )

        first = cli.do_supervise("P", apply=True)
        handoffs = HandoffRequests(ProgressLedger(pdir))
        reviews = [
            item
            for item in handoffs.all()
            if item.get("kind") == "REVIEW"
            and item.get("subject_type") == "ASSIGNMENT"
            and item.get("subject_id") == source["assignment_id"]
        ]
        assert len(reviews) == 1
        review = reviews[0]
        assert review["status"] == "ASSIGNED"
        bound = [
            item
            for item in ProgressLedger(pdir).assignments()
            if item.get("handoff_id") == review["handoff_id"]
        ]
        assert len(bound) == 1
        assert first["convergence"]["stable"] is True

        second = cli.do_supervise("P", apply=True)
        assert len([
            item
            for item in HandoffRequests(ProgressLedger(pdir)).all()
            if item.get("kind") == "REVIEW"
            and item.get("subject_type") == "ASSIGNMENT"
            and item.get("subject_id") == source["assignment_id"]
        ]) == 1
        assert len([
            item
            for item in ProgressLedger(pdir).assignments()
            if item.get("handoff_id") == review["handoff_id"]
        ]) == 1
        assert second["convergence"]["stable"] is True


def test_approved_contract_review_resolves_and_assigns_experimenter_in_one_call(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        planning.initialize_research("P", brief="Study one bounded question.")
        pdir = L.project_dir("P")
        ledger = ProgressLedger(pdir)
        _finish(ledger, "low")
        contract = ledger.submit_implementation_contract(
            proposer="low",
            node_id="P0",
            plan_version=1,
            goal="change controller",
            must_preserve_invariants=["nominal objective"],
            forbidden_behaviors=["bypass review"],
            acceptance_conditions=["targeted test passes"],
        )

        cli.do_supervise("P", apply=True)
        handoffs = HandoffRequests(ProgressLedger(pdir))
        review = next(
            item
            for item in handoffs.all()
            if item.get("kind") == "REVIEW"
            and item.get("subject_type") == "IMPLEMENTATION_CONTRACT"
            and item.get("subject_id") == contract["contract_id"]
        )
        assert review["status"] == "ASSIGNED"
        ledger.review_implementation_contract(
            contract["contract_id"],
            reviewer="low2",
            verdict="APPROVE",
            rationale="bounded and exact",
        )
        _finish(ledger, "low2")

        report = cli.do_supervise("P", apply=True)

        assert HandoffRequests(ProgressLedger(pdir)).get(review["handoff_id"])["status"] == "RESOLVED"
        experiment = ProgressLedger(pdir).current("low3")
        assert experiment is not None
        assert experiment.get("implementation_contract_id") == contract["contract_id"]
        assert report["convergence"]["stable"] is True
        assert len([
            item
            for item in ProgressLedger(pdir).assignments()
            if item.get("implementation_contract_id") == contract["contract_id"]
        ]) == 1


def test_supervise_stops_at_fixed_pass_limit_if_state_never_stabilizes(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        cli.do_new("P", roles="low:1")
        pdir = L.project_dir("P")
        ResearchPlanGraph.create(pdir, node_id="root", title="Root")
        monkeypatch.setattr(cli, "_apply_supervisor_decisions", lambda project, decisions: [])
        monkeypatch.setattr(cli.A, "apply_autonomy", lambda *args, **kwargs: [])
        counter = iter(range(100))
        monkeypatch.setattr(cli, "_reduction_key", lambda report: str(next(counter)))

        report = cli.do_supervise("P", apply=True)

        assert report["convergence"] == {
            "passes": cli._SUPERVISOR_MAX_PASSES,
            "stable": False,
            "max_passes": cli._SUPERVISOR_MAX_PASSES,
            "reason": "pass_limit",
        }
