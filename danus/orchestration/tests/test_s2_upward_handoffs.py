from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from danus.core import ProgressLedger
from danus.core.handoff import HandoffRequests
from danus.execution import layout as L
from danus.gateway import server, tools_for
from danus.orchestration import autonomy as A
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


@contextmanager
def _gateway(pdir: Path, worker: str, role: str):
    keys = {
        "DANUS_PROJECT_DIR": str(pdir),
        "DANUS_AUTHOR": worker,
        "DANUS_ROLE": role,
        "DANUS_ASSIGNMENT_ID": str((ProgressLedger(pdir).current(worker) or {}).get("assignment_id") or ""),
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


def _setup(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
    planning.initialize_research("P", brief="Study one bounded question.")
    pdir = L.project_dir("P")
    ledger = ProgressLedger(pdir)
    ledger.update(worker="low", state="DONE", actor="runtime", outcome="COMPLETE")
    cli.do_assign("P/low3", "run bounded experiment", node_id="P0")
    ledger.update(worker="low3", state="RUNNING", actor="runtime")
    return pdir, ledger


def _apply(project: str, decisions=None):
    return A.apply_autonomy(
        project,
        decisions or [],
        rows=_rows(project),
        assign=cli.do_assign,
        start=lambda wl: "started",
        role_of=cli._read_worker_role,
    )


def test_handoff_request_permissions_and_subject_dedupe(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, ledger = _setup(tmp_path, monkeypatch)
        assert "handoff_request" in tools_for("experimenter")
        assert "handoff_request" in tools_for("researcher")
        assert "handoff_request" in tools_for("reviewer")
        assert "handoff_request" not in tools_for("main")
        assert "handoff_request" not in tools_for("worker")
        assert "handoff_request" not in tools_for("verifier")

        source = ledger.current("low3")
        assert source is not None
        with _gateway(pdir, "low3", "experimenter"):
            first = server.handoff_request("INTERPRET", "unexpected result needs scientific interpretation")
            repeated = server.handoff_request("INTERPRET", "different wording must not duplicate the request")
        assert repeated["handoff_id"] == first["handoff_id"]

        handoff = HandoffRequests(ProgressLedger(pdir)).get(first["handoff_id"])
        assert handoff is not None
        assert handoff["subject_type"] == "ASSIGNMENT"
        assert handoff["subject_id"] == source["assignment_id"]
        assert handoff["source_assignment_id"] == source["assignment_id"]
        assert handoff["target_role"] == "researcher"

        cli.do_assign("P/low", "research follow-up", node_id="P0")
        ledger.update(worker="low", state="RUNNING", actor="runtime")
        with _gateway(pdir, "low", "researcher"):
            with pytest.raises(ValueError, match="cannot request INTERPRET"):
                server.handoff_request("INTERPRET", "not allowed")


def test_interpret_dispatches_to_researcher_and_resolves(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, ledger = _setup(tmp_path, monkeypatch)
        with _gateway(pdir, "low3", "experimenter"):
            opened = server.handoff_request("INTERPRET", "result contradicts the expected mechanism")

        applied = _apply("P")
        routed = next(item for item in applied if item.get("action") == "INTERPRET")
        assert routed["result"] == "assigned"
        assert routed["worker"] == "low"

        fresh_ledger = ProgressLedger(pdir)
        fresh_handoffs = HandoffRequests(fresh_ledger)
        current = fresh_handoffs.get(opened["handoff_id"])
        assert current is not None and current["status"] == "ASSIGNED"
        assignment = fresh_ledger.current("low")
        assert assignment is not None
        assert assignment["handoff_id"] == opened["handoff_id"]

        fresh_ledger.update(worker="low", state="DONE", actor="runtime", outcome="COMPLETE")
        applied = _apply("P")
        assert any(
            item.get("action") == "INTERPRET"
            and item.get("handoff_id") == opened["handoff_id"]
            and item.get("result") == "resolved"
            for item in applied
        )
        assert HandoffRequests(ProgressLedger(pdir)).get(opened["handoff_id"])["status"] == "RESOLVED"
        assert ledger.current("low3")["state"] == "RUNNING"


def test_interpret_suppresses_generic_review_and_cold_start_reuses_assignment(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, ledger = _setup(tmp_path, monkeypatch)
        with _gateway(pdir, "low3", "experimenter"):
            opened = server.handoff_request("INTERPRET", "needs researcher judgment")
        ledger.update(
            worker="low3",
            state="BLOCKED",
            actor="runtime",
            outcome="NEEDS_REVIEW",
        )

        report = reconcile(pdir, _rows("P"))
        assert any(
            item.get("assignment_id") == ledger.current("low3")["assignment_id"]
            and item.get("reason") == "assignment_needs_review"
            for item in report["decisions"]
        )
        _apply("P", report["decisions"])

        fresh_ledger = ProgressLedger(pdir)
        fresh_handoffs = HandoffRequests(fresh_ledger)
        current = fresh_handoffs.get(opened["handoff_id"])
        assert current is not None and current["status"] == "ASSIGNED"
        interpret_assignment_id = current["assigned_assignment_id"]
        assert not any(
            item.get("kind") == "REVIEW"
            and item.get("subject_type") == "ASSIGNMENT"
            and item.get("subject_id") == ledger.current("low3")["assignment_id"]
            for item in fresh_handoffs.all()
        )

        # Cold start: reconstruct all state. A dead process with a live Assignment
        # becomes RETASK; the same Assignment/handoff is restarted, not duplicated.
        cold_report = reconcile(pdir, _rows("P"))
        retry = next(
            item
            for item in cold_report["decisions"]
            if item.get("assignment_id") == interpret_assignment_id
            and item.get("reason") == "worker_not_running"
        )
        started = []
        monkeypatch.setattr(cli, "_start_one", lambda wl: started.append(wl.name) or "started")
        cli._apply_supervisor_decisions("P", [retry])
        assert started == ["low"]
        after = HandoffRequests(ProgressLedger(pdir)).get(opened["handoff_id"])
        assert after is not None and after["assigned_assignment_id"] == interpret_assignment_id
        assert len(
            [a for a in ProgressLedger(pdir).assignments() if a.get("handoff_id") == opened["handoff_id"]]
        ) == 1


def test_escalate_persists_for_main_and_blocks_false_complete(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, ledger = _setup(tmp_path, monkeypatch)
        with _gateway(pdir, "low3", "experimenter"):
            opened = server.handoff_request("ESCALATE", "control-plane decision is required")

        # Simulate a cold start by rebuilding the stores from disk only.
        handoff = HandoffRequests(ProgressLedger(pdir)).get(opened["handoff_id"])
        assert handoff is not None
        assert handoff["status"] == "OPEN"
        assert handoff["target_role"] == "main"

        ledger.set_node_state("P0", state="DONE", actor="supervisor")
        report = reconcile(pdir, _rows("P"))
        assert report["project_state"] == "RUNNING"
        pending = next(item for item in report["pending_handoffs"] if item["handoff_id"] == opened["handoff_id"])
        assert pending["kind"] == "ESCALATE"
        assert pending["target_role"] == "main"

        applied = _apply("P")
        escalation = next(item for item in applied if item.get("action") == "ESCALATE")
        assert escalation["handoff_id"] == opened["handoff_id"]
        assert escalation["result"] == "pending_main"
        assert not any(a.get("handoff_id") == opened["handoff_id"] for a in ProgressLedger(pdir).assignments())



def test_reviewer_clarify_routes_to_experimenter_then_resumes_reviewer(tmp_path: Path, monkeypatch):
    """One existing Handoff tool drives role routing; no manual node mutation."""
    from danus.orchestration.supervisor_llm import apply_scientific_route_dispositions

    with _root(tmp_path):
        pdir, ledger = _setup(tmp_path, monkeypatch)
        ledger.update(worker="low3", state="DONE", actor="runtime", outcome="COMPLETE")
        cli.do_assign("P/low2", "Review the completed experiment packet.\n", node_id="P0")
        ledger.update(worker="low2", state="RUNNING", actor="runtime")
        with _gateway(pdir, "low2", "reviewer"):
            opened = server.handoff_request(
                "CLARIFY",
                "State the missing sigma-0.10 result from the existing report; do not rerun E1.",
                target_role="experimenter",
            )
            repeated = server.handoff_request(
                "CLARIFY", "Repeat request", target_role="experimenter",
            )
        assert repeated["handoff_id"] == opened["handoff_id"]
        with _gateway(pdir, "low2", "reviewer"):
            with pytest.raises(ValueError, match="evidence_refs or target_role"):
                server.handoff_request("CLARIFY", "Wrong target", target_role="researcher")

        source_id = ledger.current("low2")["assignment_id"]
        ledger.update(worker="low2", state="BLOCKED", actor="runtime", outcome="NEEDS_REVIEW")
        pause = L.WorkerLayout(L.worker_dir("P", "low2")).pause
        pause.write_text("review:assignment_needs_review\n", encoding="utf-8")
        dispatched = _apply("P")
        claimed = next(x for x in dispatched if x.get("action") == "CLARIFY"
                       and x.get("result") == "assigned")
        assert claimed["worker"] == "low3"
        assert claimed["handoff_id"] == opened["handoff_id"]
        assert ProgressLedger(pdir).current("low3")["handoff_id"] == opened["handoff_id"]

        ledger.update(
            worker="low3", state="DONE", actor="runtime", outcome="COMPLETE",
            result="Existing report says sigma 0.10 worsens RMSE; no rerun was performed.",
        )
        _apply("P")
        assert HandoffRequests(ProgressLedger(pdir)).get(opened["handoff_id"])["status"] == "RESOLVED"
        resumed = apply_scientific_route_dispositions("P")
        assert any(x.get("source_assignment_id") == source_id and x.get("result") == "resumed"
                   for x in resumed)
        assert not pause.exists()
        current = ProgressLedger(pdir).current("low2")
        assert current["assignment_id"] == source_id
        assert current["state"] == "RUNNING"
        assert "P0" not in ProgressLedger(pdir).completed_nodes()



def test_premature_researcher_review_retries_without_completing_own_node(tmp_path, monkeypatch):
    """An R1 proposal must be completed by R1 before the planned V1 review."""
    from danus.core import ResearchPlanGraph

    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        planning.initialize_research("P", brief="Investigate one grounded connectivity hypothesis.")
        root = L.project_dir("P")
        graph = ResearchPlanGraph(root)
        ledger = ProgressLedger(root)
        change = graph.propose_change(
            base_plan_version=1, proposer="low",
            reason="scientific scope before Reviewer approval",
            operations=[
                {"op": "add_node", "node_id": "R1", "title": "Prepare",
                 "summary": "", "parent": "P0", "depends_on": []},
                {"op": "add_node", "node_id": "V1", "title": "Review",
                 "summary": "", "parent": "P0", "depends_on": ["R1"]},
            ],
        )
        graph.review_change(change, reviewer="low2", verdict="APPROVE", rationale="bounded")
        assert graph.commit_change(change, committer="supervisor") == 2
        researcher_task = "Write a scoped connectivity proposal for V1 review.\n"
        reviewer_task = "Independently review R1's saved proposal.\n"
        for role, node, task in (
            ("researcher", "R1", researcher_task),
            ("reviewer", "V1", reviewer_task),
        ):
            path = root / "tasks" / "v2" / role / f"{node}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(task, encoding="utf-8")
        ledger.update(worker="low", state="DONE", actor="runtime", outcome="COMPLETE")
        cli.do_assign("P/low", researcher_task, node_id="R1")
        source_id = ledger.current("low")["assignment_id"]
        proposal = root / "proposals" / "R1_sparse_convergence.md"
        proposal.parent.mkdir(parents=True, exist_ok=True)
        proposal.write_text("An independently reviewable bounded scientific proposal.\n")
        ledger.update(
            worker="low", state="BLOCKED", actor="runtime", outcome="NEEDS_REVIEW",
            result=(f"Saved [Research Proposal]({proposal}) for V1 review.\n"
                    "DANUS_OUTCOME: NEEDS_REVIEW"),
        )
        handoffs = HandoffRequests(ledger)
        review = handoffs.open(
            kind="REVIEW", plan_version=2, node_id="R1",
            subject_type="ASSIGNMENT", subject_id=source_id,
            requested_by="supervisor", source_role="researcher",
            source_assignment_id=source_id, request="Classify R1 NEEDS_REVIEW.",
            reason="assignment explicitly requested scientific review",
        )
        cli.do_assign("P/low2", review["request"], node_id="R1",
                      handoff_id=review["handoff_id"])
        review_id = ledger.current("low2")["assignment_id"]
        ledger.update(
            worker="low2", state="DONE", actor="runtime", outcome="COMPLETE",
            result=("The scientific route remains ambiguous.\n"
                    "DANUS_REVIEW_CLASS_VERIFIED: SCIENTIFIC_AMBIGUITY"),
        )
        assert A._auto_complete(root, {
            "action": "REVIEW", "reason": "completion_not_recorded",
            "worker": "low2", "node_id": "R1", "assignment_id": review_id,
        }) is None
        handoffs.resolve(review["handoff_id"], assignment_id=review_id,
                         actor="low2", summary="classification complete")
        escalation = handoffs.open(
            kind="ESCALATE", plan_version=2, node_id="R1",
            subject_type="ASSIGNMENT", subject_id=source_id,
            requested_by="supervisor", source_role="researcher",
            source_assignment_id=source_id, parent_handoff_id=review["handoff_id"],
            request="Main intervention required.",
            reason="restricted supervisor selected escalate",
        )
        pause = L.WorkerLayout(L.worker_dir("P", "low")).pause
        pause.write_text("review:assignment_needs_review\n", encoding="utf-8")

        recovered = A.recover_premature_proposal_review("P", token_budget=10000000)
        assert recovered["result"] == "assigned"
        assert recovered["action"] == "RETRY_PREMATURE_PROPOSAL_REVIEW"
        assert recovered["previous_assignment_id"] == source_id
        assert recovered["downstream_review_nodes"] == ["V1"]
        assert HandoffRequests(ProgressLedger(root)).get(escalation["handoff_id"])["status"] == "CANCELLED"
        assert HandoffRequests(ProgressLedger(root)).get(review["handoff_id"])["status"] == "RESOLVED"
        assert not pause.exists()
        assert ledger.current("low")["assignment_id"] != source_id
        assert ledger.current("low")["node_id"] == "R1"
        assert ledger.current("low")["state"] == "ASSIGNED"
        assert not ledger.completed_nodes()
        with pytest.raises(ValueError, match="exactly one current blocked Researcher"):
            A.recover_premature_proposal_review("P", token_budget=10000000)


def test_source_opened_escalation_survives_terminal_blocked(tmp_path, monkeypatch):
    """An ESCALATE requested before BLOCKED must not silently turn STALE."""
    with _root(tmp_path):
        root, ledger = _setup(tmp_path, monkeypatch)
        with _gateway(root, "low3", "experimenter"):
            opened = server.handoff_request(
                "ESCALATE", "Numerical precondition cannot be proven from current data."
            )
        assert HandoffRequests(ledger).get(opened["handoff_id"])["status"] == "OPEN"
        ledger.update(
            worker="low3", state="BLOCKED", actor="runtime", outcome="BLOCKED",
            result="No verified numerical prerequisite. DANUS_OUTCOME: BLOCKED",
        )
        revived = HandoffRequests(ProgressLedger(root)).get(opened["handoff_id"])
        assert revived["status"] == "OPEN"
        assert revived["target_role"] == "main"
        assert revived["subject_id"] == ledger.current("low3")["assignment_id"]


