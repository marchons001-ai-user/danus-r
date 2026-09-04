from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from danus.core import EvidenceStore, ProgressLedger, ResearchPlanGraph
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


@contextmanager
def _gateway(pdir: Path, worker: str, role: str):
    keys = {
        "DANUS_PROJECT_DIR": str(pdir),
        "DANUS_AUTHOR": worker,
        "DANUS_ROLE": role,
    }
    old = {key: os.environ.get(key) for key in keys}
    os.environ.update(keys)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _rows(project: str):
    return [
        {"worker": worker, "alive": False, "paused": False}
        for worker in L.list_workers(project)
    ]


def _apply(project: str):
    return A.apply_autonomy(
        project,
        [],
        rows=_rows(project),
        assign=cli.do_assign,
        start=lambda wl: "started",
        role_of=cli._read_worker_role,
    )


def _finish(project: str, worker: str) -> None:
    ProgressLedger(L.project_dir(project)).update(
        worker=worker,
        state="DONE",
        actor="runtime",
        outcome="COMPLETE",
    )


def _evidence(store: EvidenceStore, count: int) -> list[str]:
    protocol_id = store.freeze_protocol(
        question="Which bounded route should follow the observed result?",
        hypothesis="The frozen observation can change the coarse research route.",
        matrix={"cases": list(range(count))},
        primary_metrics=["score"],
        decision_rule="interpret the frozen bounded result",
        author="reviewer",
    )
    return [
        store.add_record(
            protocol_id=protocol_id,
            author="low3",
            matrix_coverage={"case": index},
            primary_metrics={"score": float(index)},
        )
        for index in range(count)
    ]


def _setup_interpret(tmp_path: Path, monkeypatch, *, evidence_count: int = 2):
    monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
    planning.initialize_research("P", brief="Revise the route only when bounded evidence requires it.")
    pdir = L.project_dir("P")
    _finish("P", "low")

    ledger = ProgressLedger(pdir)
    cli.do_assign("P/low3", "run the approved bounded experiment", node_id="P0")
    ledger.update(worker="low3", state="RUNNING", actor="runtime")
    refs = _evidence(EvidenceStore(pdir), evidence_count)
    with _gateway(pdir, "low3", "experimenter"):
        opened = server.handoff_request(
            "INTERPRET",
            "the observed result changes which coarse route is scientifically justified",
            evidence_refs=refs,
        )

    routed = _apply("P")
    assert any(
        item.get("action") == "INTERPRET"
        and item.get("handoff_id") == opened["handoff_id"]
        and item.get("result") == "assigned"
        and item.get("worker") == "low"
        for item in routed
    )
    researcher = ProgressLedger(pdir).current("low")
    assert researcher is not None and researcher.get("handoff_id") == opened["handoff_id"]
    return pdir, refs, opened


def _proposal(refs: list[str], *, post_hoc: bool = True, node_id: str = "R1") -> dict:
    return {
        "base_plan_version": 1,
        "reason": "the bounded evidence requires one different coarse follow-up route",
        "post_hoc": post_hoc,
        "evidence_refs": refs,
        "operations": [
            {
                "op": "add_node",
                "node_id": node_id,
                "title": "Evidence follow-up",
                "summary": "Run the smallest follow-up justified by the observed evidence.",
                "parent": "P0",
                "depends_on": [],
            }
        ],
        "tasks": [
            {
                "node_id": node_id,
                "role": "experimenter",
                "task": "Run only the smallest reviewed follow-up experiment.",
            }
        ],
    }


def _write_proposal(pdir: Path, payload: dict) -> None:
    path = planning._proposal_path(pdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_evidence_interpret_to_reviewed_post_hoc_commit(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, refs, opened = _setup_interpret(tmp_path, monkeypatch)
        _write_proposal(pdir, _proposal(refs))
        _finish("P", "low")

        actions = planning.planning_step("P")
        captured = next(item for item in actions if item.get("action") == "PLAN_PROPOSAL")
        assert captured["result"] == "captured"
        graph = ResearchPlanGraph(pdir)
        change = graph.pending_changes()[0]
        assert change["post_hoc"] is True
        assert change["evidence_refs"] == refs

        review = next(
            item
            for item in HandoffRequests(ProgressLedger(pdir)).all()
            if item.get("kind") == "REVIEW"
            and item.get("subject_type") == "PLAN_CHANGE"
            and item.get("subject_id") == change["change_id"]
        )
        assert review["status"] == "ASSIGNED"
        with _gateway(pdir, "low2", "reviewer"):
            reviewed = server.plan_review_change(
                change["change_id"],
                rationale="the revision is bounded and preserves all triggering evidence",
                verdict="APPROVE",
            )
        assert reviewed["status"] == "APPROVED"
        _finish("P", "low2")

        committed = planning.planning_step("P")
        assert any(
            item.get("action") == "PLAN_COMMIT"
            and item.get("change_id") == change["change_id"]
            and item.get("plan_version") == 2
            for item in committed
        )
        assert graph.snapshot()["plan_version"] == 2
        assert (pdir / "tasks" / "v2" / "experimenter" / "R1.md").is_file()
        assert HandoffRequests(ProgressLedger(pdir)).get(opened["handoff_id"])["evidence_refs"] == refs


def test_evidence_bound_revision_rejects_non_post_hoc_and_direct_mcp_bypass(
    tmp_path: Path, monkeypatch
):
    with _root(tmp_path):
        pdir, refs, _ = _setup_interpret(tmp_path, monkeypatch)
        operations = _proposal(refs)["operations"]
        with _gateway(pdir, "low", "researcher"):
            with pytest.raises(ValueError, match="autonomous plan proposal handoff"):
                server.plan_propose_change(
                    base_plan_version=1,
                    reason="direct result-conditioned change",
                    operations=operations,
                    post_hoc=True,
                    evidence_refs=refs,
                )
        assert ResearchPlanGraph(pdir).pending_changes() == []

        _write_proposal(pdir, _proposal(refs, post_hoc=False))
        _finish("P", "low")
        actions = planning.planning_step("P")
        blocked = next(item for item in actions if item.get("action") == "PLAN_PROPOSAL")
        assert blocked["result"] == "blocked"
        assert "post_hoc=true" in blocked["reason"]
        assert ResearchPlanGraph(pdir).snapshot()["plan_version"] == 1
        assert ResearchPlanGraph(pdir).pending_changes() == []


@pytest.mark.parametrize("include_all", [False, True])
def test_triggering_evidence_cannot_be_dropped_but_extra_evidence_is_allowed(
    tmp_path: Path, monkeypatch, include_all: bool
):
    with _root(tmp_path):
        pdir, refs, _ = _setup_interpret(tmp_path, monkeypatch)
        store = EvidenceStore(pdir)
        extra = _evidence(store, 1)[0]
        proposal_refs = refs + [extra] if include_all else refs[:1]
        _write_proposal(pdir, _proposal(proposal_refs))
        _finish("P", "low")

        actions = planning.planning_step("P")
        captured = next(item for item in actions if item.get("action") == "PLAN_PROPOSAL")
        if include_all:
            assert captured["result"] == "captured"
            change = ResearchPlanGraph(pdir).pending_changes()[0]
            assert change["evidence_refs"] == proposal_refs
        else:
            assert captured["result"] == "blocked"
            assert "preserve triggering evidence_refs" in captured["reason"]
            assert ResearchPlanGraph(pdir).pending_changes() == []


def test_reviewer_revision_cannot_strip_post_hoc_provenance(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, refs, _ = _setup_interpret(tmp_path, monkeypatch)
        _write_proposal(pdir, _proposal(refs))
        _finish("P", "low")
        planning.planning_step("P")

        graph = ResearchPlanGraph(pdir)
        first = graph.pending_changes()[0]
        with _gateway(pdir, "low2", "reviewer"):
            server.plan_review_change(
                first["change_id"],
                rationale="keep the evidence but revise the route",
                verdict="REVISE",
            )
        _finish("P", "low2")
        planning.planning_step("P")

        correction = ProgressLedger(pdir).current("low")
        assert correction is not None and correction.get("handoff_id")
        correction_handoff = HandoffRequests(ProgressLedger(pdir)).get(correction["handoff_id"])
        assert correction_handoff is not None
        assert correction_handoff["kind"] == "REVISION"
        assert correction_handoff["subject_id"] == first["change_id"]

        replacement = _proposal([], post_hoc=False, node_id="R2")
        _write_proposal(pdir, replacement)
        _finish("P", "low")
        actions = planning.planning_step("P")
        blocked = next(item for item in actions if item.get("action") == "PLAN_PROPOSAL")
        assert blocked["result"] == "blocked"
        assert "post_hoc=true" in blocked["reason"]
        assert graph.snapshot()["plan_version"] == 1
