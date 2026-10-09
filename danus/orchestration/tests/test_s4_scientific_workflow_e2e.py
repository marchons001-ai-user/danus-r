from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from danus.core import EvidenceStore, ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.execution import layout as L
from danus.execution.experiment import run_experiment
from danus.gateway import server
from danus.orchestration import autonomy as A
from danus.orchestration import cli, planning


RUNNER = """from pathlib import Path\nimport json, sys\np=Path(sys.argv[1])\np.parent.mkdir(parents=True,exist_ok=True)\np.write_text(json.dumps({'score': 2.0}))\n"""

REDUCER = """import json\n\ndef reduce_metrics(protocol, artifacts):\n    return {'score': json.loads(artifacts[0].read_text())['score']}\n"""


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
    values = {
        "DANUS_PROJECT_DIR": str(pdir),
        "DANUS_AUTHOR": worker,
        "DANUS_ROLE": role,
        "DANUS_ASSIGNMENT_ID": str((ProgressLedger(pdir).current(worker) or {}).get("assignment_id") or ""),
    }
    old = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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


def _runnable_protocol(pdir: Path) -> str:
    runner = pdir / "runner.py"
    reducer = pdir / "reducer.py"
    runner.write_text(RUNNER, encoding="utf-8")
    reducer.write_text(REDUCER, encoding="utf-8")
    store = EvidenceStore(pdir)
    reducer_ref = store.artifact_ref(reducer)
    reducer_ref["function"] = "reduce_metrics"
    return store.freeze_protocol(
        question="What is the score under the one-cell frozen protocol?",
        hypothesis="The frozen run returns the pre-specified score.",
        matrix={
            "units": [
                {
                    "id": "u1",
                    "argv": [sys.executable, "runner.py", "raw/result.json"],
                    "artifacts": ["raw/result.json"],
                }
            ]
        },
        primary_metrics=["score"],
        decision_rule="record the frozen score without adapting the run",
        author="low2",
        metric_reducer=reducer_ref,
        execution_files=["runner.py"],
    )


def _handoff(handoffs: HandoffRequests, *, kind: str, subject_type: str, subject_id: str):
    return next(
        item
        for item in handoffs.all()
        if item.get("kind") == kind
        and item.get("subject_type") == subject_type
        and item.get("subject_id") == subject_id
    )


def _contract_to_interpret(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
    planning.initialize_research("P", brief="Run one reviewed bounded experiment and interpret its evidence.")
    pdir = L.project_dir("P")
    ledger = ProgressLedger(pdir)
    handoffs = HandoffRequests(ledger)
    protocol_id = _runnable_protocol(pdir)
    with _gateway(pdir, "low", "researcher"):
        contract = server.implementation_submit_contract(
            node_id="P0",
            plan_version=1,
            goal=f"Execute frozen protocol {protocol_id} exactly and return its immutable Evidence Record.",
            must_preserve_invariants=["frozen protocol, matrix, and primary metric"],
            forbidden_behaviors=["result-dependent adaptation or extra experiment cells"],
            acceptance_conditions=["one immutable Evidence Record is produced by the Experiment Runtime"],
        )
    _finish("P", "low")

    _apply("P")
    contract_review = _handoff(
        handoffs,
        kind="REVIEW",
        subject_type="IMPLEMENTATION_CONTRACT",
        subject_id=contract["contract_id"],
    )
    reviewer_assignment = ledger.current("low2")
    assert contract_review["status"] == "ASSIGNED"
    assert reviewer_assignment and reviewer_assignment.get("handoff_id") == contract_review["handoff_id"]

    with _gateway(pdir, "low2", "reviewer"):
        server.implementation_review_contract(
            contract["contract_id"],
            verdict="APPROVE",
            rationale="the contract is bounded and preserves the frozen protocol",
        )
    _finish("P", "low2")
    _apply("P")

    experiment = ledger.current("low3")
    assert experiment and experiment.get("implementation_contract_id") == contract["contract_id"]
    assert experiment.get("task_sha256") == ProgressLedger.task_sha256(contract["task"])
    assert handoffs.get(contract_review["handoff_id"])["status"] == "RESOLVED"
    ledger.update(worker="low3", state="RUNNING", actor="runtime")

    first = run_experiment(pdir, protocol_id, author="low3")
    second = run_experiment(pdir, protocol_id, author="low3")
    assert second["evidence_id"] == first["evidence_id"]
    assert len(list(EvidenceStore(pdir).records_dir.glob("*.json"))) == 1

    with _gateway(pdir, "low3", "experimenter"):
        opened = server.handoff_request(
            "INTERPRET",
            "interpret the completed frozen experiment without broadening it",
            evidence_refs=[first["evidence_id"]],
        )
        repeated = server.handoff_request(
            "INTERPRET",
            "same evidence, same durable interpretation route",
            evidence_refs=[first["evidence_id"]],
        )
    assert repeated["handoff_id"] == opened["handoff_id"]
    _finish("P", "low3")
    _apply("P")

    researcher = ledger.current("low")
    assert researcher and researcher.get("handoff_id") == opened["handoff_id"]
    interpret = handoffs.get(opened["handoff_id"])
    assert interpret and interpret["evidence_refs"] == [first["evidence_id"]]
    assert interpret["subject_id"] == experiment["assignment_id"]
    return pdir, ledger, handoffs, contract, contract_review, experiment, first, interpret


def test_scientific_workflow_closes_without_automatic_plan_revision(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, ledger, handoffs, contract, contract_review, experiment, evidence, interpret = (
            _contract_to_interpret(tmp_path, monkeypatch)
        )
        assignment_count = len(ledger.assignments())
        handoff_count = len(handoffs.all())

        _finish("P", "low")
        _apply("P")
        _apply("P")

        assert ResearchPlanGraph(pdir).snapshot()["plan_version"] == 1
        assert ResearchPlanGraph(pdir).pending_changes() == []
        assert handoffs.get(interpret["handoff_id"])["status"] == "RESOLVED"
        assert handoffs.get(contract_review["handoff_id"])["status"] == "RESOLVED"
        assert ledger.implementation_contract(contract["contract_id"])["status"] == "CONSUMED"
        assert EvidenceStore(pdir).get_record(evidence["evidence_id"])["protocol_id"] == evidence["protocol_id"]
        assert len(ledger.assignments()) == assignment_count
        assert len(handoffs.all()) == handoff_count
        assert len(list(EvidenceStore(pdir).records_dir.glob("*.json"))) == 1
        assert experiment["assignment_id"] == interpret["subject_id"]


def test_scientific_workflow_commits_evidence_bound_post_hoc_revision(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, ledger, handoffs, contract, contract_review, experiment, evidence, interpret = (
            _contract_to_interpret(tmp_path, monkeypatch)
        )
        evidence_id = evidence["evidence_id"]
        proposal = {
            "base_plan_version": 1,
            "reason": "the frozen evidence justifies one bounded structural follow-up",
            "post_hoc": True,
            "evidence_refs": [evidence_id],
            "operations": [
                {
                    "op": "add_node",
                    "node_id": "R1",
                    "title": "Evidence follow-up",
                    "summary": "Run only the smallest follow-up justified by the observed evidence.",
                    "parent": "P0",
                    "depends_on": [],
                }
            ],
            "tasks": [
                {
                    "node_id": "R1",
                    "role": "experimenter",
                    "task": "Run only the reviewed bounded follow-up; do not broaden the matrix.\n",
                }
            ],
        }
        path = planning._proposal_path(pdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        proposal["assignment_id"] = ledger.current("low")["assignment_id"]
        path.write_text(json.dumps(proposal), encoding="utf-8")
        _finish("P", "low")

        actions = planning.planning_step("P")
        captured = next(item for item in actions if item.get("action") == "PLAN_PROPOSAL")
        assert captured["result"] == "captured"
        _apply("P")
        graph = ResearchPlanGraph(pdir)
        change = graph.pending_changes()[0]
        assert change["post_hoc"] is True
        assert change["evidence_refs"] == [evidence_id]
        assert handoffs.get(interpret["handoff_id"])["status"] == "RESOLVED"

        plan_review = _handoff(
            handoffs,
            kind="REVIEW",
            subject_type="PLAN_CHANGE",
            subject_id=change["change_id"],
        )
        assert plan_review["status"] == "ASSIGNED"
        with _gateway(pdir, "low2", "reviewer"):
            server.plan_review_change(
                change["change_id"],
                rationale="the post-hoc change is bounded and preserves the triggering evidence",
                verdict="APPROVE",
            )
        _finish("P", "low2")
        committed = planning.planning_step("P")
        assert any(
            item.get("action") == "PLAN_COMMIT"
            and item.get("change_id") == change["change_id"]
            and item.get("plan_version") == 2
            for item in committed
        )

        assignment_count = len(ledger.assignments())
        handoff_count = len(handoffs.all())
        evidence_count = len(list(EvidenceStore(pdir).records_dir.glob("*.json")))
        planning.planning_step("P")
        planning.planning_step("P")
        replay = run_experiment(pdir, evidence["protocol_id"], author="low3")

        assert replay["evidence_id"] == evidence_id
        assert graph.snapshot()["plan_version"] == 2
        assert graph.get_change(change["change_id"])["status"] == "COMMITTED"
        assert (pdir / "tasks" / "v2" / "experimenter" / "R1.md").is_file()
        assert handoffs.get(plan_review["handoff_id"])["status"] == "RESOLVED"
        assert handoffs.get(contract_review["handoff_id"])["status"] == "RESOLVED"
        assert ledger.implementation_contract(contract["contract_id"])["status"] == "CONSUMED"
        assert experiment["assignment_id"] == interpret["subject_id"]
        assert interpret["evidence_refs"] == [evidence_id]
        assert len(ledger.assignments()) == assignment_count
        assert len(handoffs.all()) == handoff_count
        assert len(list(EvidenceStore(pdir).records_dir.glob("*.json"))) == evidence_count


def _crossnode_contract_fixture(tmp_path, monkeypatch):
    """Researcher R1 may propose only the exact preapproved DAG-dependent E1."""
    from danus.gateway import capability

    monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
    planning.initialize_research("P", brief="Use the frozen protocol, no extra cells.")
    pdir = L.project_dir("P")
    ledger = ProgressLedger(pdir)
    protocol_id = EvidenceStore(pdir).freeze_protocol(
        question="Frozen comparison?", hypothesis="Experimental intervention helps.",
        matrix={"units": [{"id": "a", "argv": [sys.executable, "run_case.py"]}]},
        primary_metrics=["loss"], decision_rule="Never change protocol after reviewing.",
        author="fixture",
    )
    goal = f"Run only frozen protocol {protocol_id}\nEXPERIMENT_PROTOCOL_ID={protocol_id}"
    fields = {
        "goal": goal,
        "must_preserve_invariants": ["Do not alter frozen input"],
        "forbidden_behaviors": ["No additional experiment cells"],
        "acceptance_conditions": ["Return immutable evidence"],
    }
    exact = ProgressLedger.implementation_task(**fields)
    brief = pdir / "RESEARCH_BRIEF.md"
    brief.write_text("Frozen protocol ID: " + protocol_id)
    graph = ResearchPlanGraph(pdir)
    ops = [
        {"op": "add_node", "node_id": "R1", "title": "Frame",
         "summary": "", "parent": "P0", "depends_on": []},
        {"op": "add_node", "node_id": "V1", "title": "Review",
         "summary": "", "parent": "P0", "depends_on": ["R1"]},
        {"op": "add_node", "node_id": "E1", "title": "Experiment",
         "summary": "", "parent": "P0", "depends_on": ["V1"]},
        {"op": "add_node", "node_id": "E2", "title": "Unrelated experiment",
         "summary": "", "parent": "P0", "depends_on": []},
    ]
    change_id = graph.propose_change(
        base_plan_version=1, proposer="low", reason="fixed route", operations=ops,
    )
    graph.review_change(change_id, reviewer="low2", verdict="APPROVE", rationale="fixed")
    assert graph.commit_change(change_id, committer="supervisor") == 2
    for node_id in ("E1", "E2"):
        spec = pdir / "tasks" / "v2" / "experimenter" / f"{node_id}.md"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(exact, encoding="utf-8")
    ledger.assign(
        worker="low", role="researcher", node_id="R1", plan_version=2,
        task="Inspect frozen protocol " + protocol_id + " and submit exact E1 contract.",
        write_task=True,
    )
    binding = capability.issue(pdir, "low", "researcher")
    monkeypatch.setattr(server, "_BOUND_WORKER", binding)
    return pdir, ledger, protocol_id, fields


def test_researcher_r1_submits_exact_downstream_e1_and_reads_bound_protocol(tmp_path, monkeypatch):
    from danus.gateway.roles import tools_for
    with _root(tmp_path):
        pdir, ledger, protocol_id, fields = _crossnode_contract_fixture(tmp_path, monkeypatch)
        assert "experiment_protocol_get" in tools_for("researcher")
        assert "experiment_protocol_get" in tools_for("reviewer")
        assert "experiment_protocol_get" not in tools_for("worker")
        loaded = server.experiment_protocol_get(protocol_id)["protocol"]
        assert loaded["protocol_id"] == protocol_id and loaded["status"] == "frozen"
        assert loaded["decision_rule"] == "Never change protocol after reviewing."
        contract = server.implementation_submit_contract(
            node_id="E1", plan_version=2, **fields
        )
        record = ledger.implementation_contract(contract["contract_id"])
        assert record["node_id"] == "E1"
        assert record["task"] == (pdir / "tasks" / "v2" / "experimenter" / "E1.md").read_text()
        assert record["status"] == "PROPOSED"
        assert len(ledger.implementation_contracts()) == 1


def test_crossnode_contract_rejects_unrelated_node_or_altered_task(tmp_path, monkeypatch):
    import pytest
    with _root(tmp_path):
        pdir, ledger, protocol_id, fields = _crossnode_contract_fixture(tmp_path, monkeypatch)
        with pytest.raises(PermissionError, match="not downstream"):
            server.implementation_submit_contract(node_id="E2", plan_version=2, **fields)
        with pytest.raises(PermissionError, match="exact frozen Experimenter task"):
            server.implementation_submit_contract(
                node_id="E1", plan_version=2, **{**fields, "goal": "altered target"}
            )
        with pytest.raises(PermissionError, match="exact frozen Experimenter task"):
            server.implementation_submit_contract(node_id="V1", plan_version=2, **fields)
        assert ledger.implementation_contracts() == []


def test_frozen_protocol_get_rejects_other_task_and_revoked_assignment(tmp_path, monkeypatch):
    import pytest
    from danus.gateway import capability
    with _root(tmp_path):
        pdir, ledger, protocol_id, fields = _crossnode_contract_fixture(tmp_path, monkeypatch)
        second = EvidenceStore(pdir).freeze_protocol(
            question="Different protocol?", hypothesis="Different result",
            matrix={"units": [{"id": "b"}]}, primary_metrics=["loss"],
            decision_rule="Unrelated", author="fixture",
        )
        with pytest.raises(PermissionError, match="assigned task"):
            server.experiment_protocol_get(second)
        with pytest.raises(ValueError, match="invalid protocol identity"):
            server.experiment_protocol_get("../secret")
        binding = server._BOUND_WORKER
        capability.revoke(binding)
        with pytest.raises(PermissionError, match="revoked|epoch"):
            server.experiment_protocol_get(protocol_id)


def _invoke_bound_protocol_mcp_child(protocol_id, response_queue):
    """Call the same permission wrapper installed by build_app()."""
    try:
        result = server._bound_tool(server.experiment_protocol_get)(protocol_id)
        response_queue.put(("ok", result["protocol"]["protocol_id"]))
    except BaseException as exc:
        response_queue.put(("error", type(exc).__name__ + ": " + str(exc)))


def test_bound_protocol_mcp_wrapper_finishes_without_recursive_lock_deadlock(tmp_path, monkeypatch):
    """A bound MCP read must return promptly; separate process catches real flock hangs."""
    import multiprocessing
    import pytest

    with _root(tmp_path):
        _, _, protocol_id, _ = _crossnode_contract_fixture(tmp_path, monkeypatch)
        context = multiprocessing.get_context("fork")  # Linux/WSL: preserve issued capability
        responses = context.Queue()
        child = context.Process(
            target=_invoke_bound_protocol_mcp_child,
            args=(protocol_id, responses),
        )
        child.start()
        try:
            child.join(timeout=6)
            if child.is_alive():
                pytest.fail("Bound MCP protocol read deadlocked acquiring capability locks")
            assert child.exitcode == 0
            assert responses.get(timeout=2) == ("ok", protocol_id)
        finally:
            if child.is_alive():
                child.kill()
            child.join(timeout=2)
            responses.close()
