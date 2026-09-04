from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from danus.core import ProgressLedger
from danus.core.handoff import HandoffRequests
from danus.execution import layout as L
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


def _review_for(handoffs: HandoffRequests, subject_id: str):
    return next(
        item
        for item in handoffs.all()
        if item["kind"] == "REVIEW"
        and item["subject_type"] == "IMPLEMENTATION_CONTRACT"
        and item["subject_id"] == subject_id
    )


def test_contract_review_resolves_before_exact_experimenter_assignment(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        planning.initialize_research("P", brief="Study one bounded question.")
        pdir = L.project_dir("P")
        ledger = ProgressLedger(pdir)
        handoffs = HandoffRequests(ledger)
        _finish("P", "low")

        contract = _contract(ledger)
        duplicate = _contract(ledger)
        assert duplicate["contract_id"] == contract["contract_id"]

        _apply("P")
        review = _review_for(handoffs, contract["contract_id"])
        reviewer_assignment = ledger.current("low2")
        assert review["status"] == "ASSIGNED"
        assert reviewer_assignment and reviewer_assignment["handoff_id"] == review["handoff_id"]

        ledger.review_implementation_contract(
            contract["contract_id"],
            reviewer="low2",
            verdict="APPROVE",
            rationale="bounded and exact",
        )
        with pytest.raises(ValueError, match="review handoff requires RESOLVED"):
            ledger.assign(
                worker="low3",
                role="experimenter",
                node_id="P0",
                plan_version=1,
                task=contract["task"],
            )

        _finish("P", "low2")
        _apply("P")
        assert handoffs.get(review["handoff_id"])["status"] == "RESOLVED"
        experiment = ledger.current("low3")
        assert experiment and experiment["implementation_contract_id"] == contract["contract_id"]
        assert experiment["task_sha256"] == contract["task_sha256"]

        _apply("P")
        assert len(
            [
                item
                for item in ledger.assignments()
                if item.get("implementation_contract_id") == contract["contract_id"]
            ]
        ) == 1


@pytest.mark.parametrize(
    ("verdict", "kind"),
    [("REVISE", "REVISION"), ("REJECT", "RETHINK")],
)
def test_contract_correction_handoff_infers_replacement_lineage_and_retries_idempotently(
    tmp_path: Path,
    monkeypatch,
    verdict: str,
    kind: str,
):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        planning.initialize_research("P", brief="Study one bounded question.")
        pdir = L.project_dir("P")
        ledger = ProgressLedger(pdir)
        handoffs = HandoffRequests(ledger)
        _finish("P", "low")

        original = _contract(ledger)
        _apply("P")
        review = _review_for(handoffs, original["contract_id"])
        ledger.review_implementation_contract(
            original["contract_id"],
            reviewer="low2",
            verdict=verdict,
            rationale="implementation direction must change",
        )
        _finish("P", "low2")
        _apply("P")

        correction = next(
            item
            for item in handoffs.all()
            if item["kind"] == kind
            and item["subject_type"] == "IMPLEMENTATION_CONTRACT"
            and item["subject_id"] == original["contract_id"]
        )
        researcher_assignment = ledger.current("low")
        assert correction["parent_handoff_id"] == review["handoff_id"]
        assert researcher_assignment and researcher_assignment["handoff_id"] == correction["handoff_id"]

        replacement = _contract(ledger, goal="corrected controller")
        duplicate = _contract(ledger, goal="corrected controller")
        assert replacement["contract_id"] == duplicate["contract_id"]
        assert replacement["replaces_contract_id"] == original["contract_id"]

        _finish("P", "low")
        _apply("P")
        assert handoffs.get(correction["handoff_id"])["status"] == "RESOLVED"
        next_review = _review_for(handoffs, replacement["contract_id"])
        assert next_review["handoff_id"] != review["handoff_id"]
        assert next_review["status"] == "ASSIGNED"


def test_generic_needs_review_creates_one_durable_reviewer_handoff(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        planning.initialize_research("P", brief="Study one bounded question.")
        pdir = L.project_dir("P")
        ledger = ProgressLedger(pdir)
        handoffs = HandoffRequests(ledger)
        _finish("P", "low")

        source = cli.do_assign("P/low", "Investigate ambiguous result.\n", node_id="P0")
        ledger.update(
            worker="low",
            state="BLOCKED",
            actor="runtime",
            note="independent scientific judgment required",
            outcome="NEEDS_REVIEW",
        )

        cli.do_supervise("P", apply=True)
        review = next(
            item
            for item in handoffs.all()
            if item["kind"] == "REVIEW"
            and item["subject_type"] == "ASSIGNMENT"
            and item["subject_id"] == source["assignment_id"]
        )
        reviewer_assignment = ledger.current("low2")
        assert review["source_assignment_id"] == source["assignment_id"]
        assert review["status"] == "ASSIGNED"
        assert reviewer_assignment and reviewer_assignment["handoff_id"] == review["handoff_id"]

        cli.do_supervise("P", apply=True)
        assert len(
            [
                item
                for item in handoffs.all()
                if item["kind"] == "REVIEW"
                and item["subject_type"] == "ASSIGNMENT"
                and item["subject_id"] == source["assignment_id"]
            ]
        ) == 1

        _finish("P", "low2")
        cli.do_supervise("P", apply=True)
        assert handoffs.get(review["handoff_id"])["status"] == "RESOLVED"
        assert ledger.current("low")["state"] == "BLOCKED"
        assert ledger.current("low")["outcome"] == "NEEDS_REVIEW"
