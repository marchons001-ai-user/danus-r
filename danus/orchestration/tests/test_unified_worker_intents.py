"""Focused invariants for unified dispatch and advisory Worker Result protocol."""
from __future__ import annotations

import json

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.orchestration import intent


def _project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    ResearchPlanGraph.create(root, node_id="P0", title="Research")
    return root


def _assignment(root, worker="researcher"):
    ledger = ProgressLedger(root)
    return ledger.assign(
        worker=worker, role="researcher", node_id="P0",
        plan_version=1, task="Bounded research\n",
    )


def test_worker_result_schema_and_immutable_retry(tmp_path):
    root = _project(tmp_path)
    assignment = _assignment(root)
    args = dict(
        outcome="COMPLETE", next_intent="REVIEW_CONTRACT",
        subject_id="c123", artifact_refs=["artifact-1"],
    )
    first = intent.submit(root, assignment, **args)
    second = intent.submit(root, assignment, **args)
    assert first == second
    assert first["event_type"] == "WORKER_RESULT"
    assert first["schema_version"] == 1
    assert len(intent._path(root).read_text().splitlines()) == 1
    with pytest.raises(ValueError, match="conflicting"):
        intent.submit(root, assignment, outcome="COMPLETE",
                      next_intent="FINISH", artifact_refs=[])
    assert len(intent._path(root).read_text().splitlines()) == 1


def test_worker_result_rejects_invalid_semantics_without_state_mutation(tmp_path):
    root = _project(tmp_path)
    assignment = _assignment(root)
    with pytest.raises(ValueError, match="next_intent requires"):
        intent.submit(root, assignment, outcome="COMPLETE",
                      next_intent="REVIEW_PLAN", subject_id="../escape")
    with pytest.raises(ValueError, match="FINISH requires"):
        intent.submit(root, assignment, outcome="BLOCKED", next_intent="FINISH")
    with pytest.raises(ValueError, match="artifact"):
        intent.submit(root, assignment, outcome="COMPLETE",
                      next_intent="WAIT", artifact_refs=["/tmp/host-file"])
    assert not intent._path(root).exists()
    assert ProgressLedger(root).current("researcher")["state"] == "ASSIGNED"


def test_next_intent_only_accepted_after_exact_terminal_assignment(tmp_path):
    root = _project(tmp_path)
    assignment = _assignment(root)
    intent.submit(root, assignment, outcome="COMPLETE",
                  next_intent="RUN_NODE", subject_id="P0")
    assert intent.terminal_intents(root) == []
    ledger = ProgressLedger(root)
    ledger.update(worker="researcher", state="DONE", actor="runtime",
                  outcome="COMPLETE",
                  expected_assignment_id=assignment["assignment_id"])
    accepted = intent.terminal_intents(root)
    assert len(accepted) == 1
    assert intent.preference(root, "RUN_NODE", "P0") == 0
    assert intent.preference(root, "RUN_NODE", "UNAUTHORIZED_NODE") == 1
    # A re-bound worker revokes the previous result's dispatch preference.
    ledger.assign(worker="researcher", role="researcher", node_id="P0",
                  plan_version=1, task="Another task\n")
    assert intent.terminal_intents(root) == []


def test_worker_result_does_not_mutate_plan_or_grant_contract(tmp_path):
    root = _project(tmp_path)
    assignment = _assignment(root)
    before = ResearchPlanGraph(root).snapshot()
    intent.submit(root, assignment, outcome="COMPLETE",
                  next_intent="RUN_EXPERIMENT", subject_id="nonexistent-contract")
    assert ResearchPlanGraph(root).snapshot() == before
    assert ProgressLedger(root).implementation_contracts() == []
    assert ProgressLedger(root).completed_nodes() == []


def test_structured_intent_is_not_a_worker_credential(tmp_path):
    root = _project(tmp_path)
    a = _assignment(root)
    intent.submit(root, a, outcome="COMPLETE",
                  next_intent="FINISH")
    # Even a forged-looking FINISH request never closes a Plan node.
    assert ResearchPlanGraph(root).snapshot()["plan_version"] == 1
    assert ProgressLedger(root).current("researcher")["state"] == "ASSIGNED"


def test_runtime_terminal_bridge_defaults_to_wait_and_is_idempotent(tmp_path):
    root = _project(tmp_path)
    assignment = _assignment(root)
    ledger = ProgressLedger(root)
    ledger.update(worker="researcher", state="DONE", actor="runtime",
                  outcome="COMPLETE",
                  expected_assignment_id=assignment["assignment_id"])
    first = intent.ensure_terminal_result(root, assignment["assignment_id"], "COMPLETE")
    second = intent.ensure_terminal_result(root, assignment["assignment_id"], "COMPLETE")
    assert first == second
    assert first["next_intent"] == {"kind": "WAIT", "subject_id": ""}
    assert len(intent._path(root).read_text().splitlines()) == 1
    assert len(intent.terminal_intents(root)) == 1


def test_runtime_bridge_preserves_explicit_model_intent_and_rejects_rebind(tmp_path):
    root = _project(tmp_path)
    assignment = _assignment(root)
    proposed = intent.submit(root, assignment, outcome="COMPLETE",
                             next_intent="REVIEW_PLAN", subject_id="change123")
    ledger = ProgressLedger(root)
    ledger.update(worker="researcher", state="DONE", actor="runtime",
                  outcome="COMPLETE",
                  expected_assignment_id=assignment["assignment_id"])
    assert intent.ensure_terminal_result(root, assignment["assignment_id"], "COMPLETE") == proposed
    ledger.assign(worker="researcher", role="researcher", node_id="P0",
                  plan_version=1, task="Fresh assignment\n")
    with pytest.raises(ValueError, match="current Assignment"):
        intent.ensure_terminal_result(root, assignment["assignment_id"], "COMPLETE")
