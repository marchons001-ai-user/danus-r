from __future__ import annotations

import json

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests


def _handoffs(tmp_path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    return plan, ledger, HandoffRequests(ledger)


def _open(handoffs: HandoffRequests, version: int, **overrides):
    values = {
        "kind": "REVIEW",
        "plan_version": version,
        "node_id": "root",
        "subject_type": "PLAN_NODE",
        "subject_id": "root",
        "requested_by": "r1",
        "source_role": "researcher",
        "request": "Review the current result.",
        "reason": "scientific review required",
    }
    values.update(overrides)
    return handoffs.open(**values)


def _bind(ledger: ProgressLedger, request, worker="v1"):
    return ledger.assign(
        worker=worker,
        role=request["target_role"],
        node_id=request["node_id"],
        plan_version=request["plan_version"],
        task=request["request"],
        handoff_id=request["handoff_id"],
    )


def test_handoff_open_is_durable_and_idempotent(tmp_path):
    plan, ledger, handoffs = _handoffs(tmp_path)
    version = plan.snapshot()["plan_version"]

    first = _open(handoffs, version)
    second = _open(handoffs, version)

    assert first["handoff_id"] == second["handoff_id"]
    assert first["status"] == "OPEN"
    assert first["target_role"] == "reviewer"
    assert first["attempt"] == 0
    assert first["request_sha256"] == ProgressLedger.task_sha256(first["request"])
    assert [item["handoff_id"] for item in handoffs.pending()] == [first["handoff_id"]]

    events = [json.loads(line) for line in ledger.path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["HANDOFF_OPENED"]


def test_assignment_event_is_the_handoff_binding_and_is_idempotent(tmp_path):
    plan, ledger, handoffs = _handoffs(tmp_path)
    request = _open(handoffs, plan.snapshot()["plan_version"])

    first = _bind(ledger, request)
    second = _bind(ledger, request)

    assert second["assignment_id"] == first["assignment_id"]
    assert first["handoff_id"] == request["handoff_id"]
    bound = handoffs.get(request["handoff_id"])
    assert bound is not None
    assert bound["status"] == "ASSIGNED"
    assert bound["attempt"] == 1
    assert bound["assigned_assignment_id"] == first["assignment_id"]

    events = [json.loads(line) for line in ledger.path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["HANDOFF_OPENED", "ASSIGNED"]
    assert events[-1]["handoff_id"] == request["handoff_id"]


def test_handoff_binding_rejects_wrong_role_task_and_unknown_id(tmp_path):
    plan, ledger, handoffs = _handoffs(tmp_path)
    request = _open(handoffs, plan.snapshot()["plan_version"])

    with pytest.raises(ValueError, match="target role"):
        ledger.assign(
            worker="r2",
            role="researcher",
            node_id="root",
            plan_version=request["plan_version"],
            task=request["request"],
            handoff_id=request["handoff_id"],
        )
    with pytest.raises(ValueError, match="handoff request"):
        ledger.assign(
            worker="v1",
            role="reviewer",
            node_id="root",
            plan_version=request["plan_version"],
            task="different task",
            handoff_id=request["handoff_id"],
        )
    with pytest.raises(ValueError, match="unknown handoff"):
        ledger.assign(
            worker="v1",
            role="reviewer",
            node_id="root",
            plan_version=request["plan_version"],
            task=request["request"],
            handoff_id="missing",
        )

    assert handoffs.get(request["handoff_id"])["status"] == "OPEN"


def test_handoff_retry_reuses_request_and_replaces_previous_attempt(tmp_path):
    plan, ledger, handoffs = _handoffs(tmp_path)
    request = _open(handoffs, plan.snapshot()["plan_version"])
    hid = request["handoff_id"]

    first = _bind(ledger, request, worker="v1")
    reopened = handoffs.reopen(
        hid,
        assignment_id=first["assignment_id"],
        actor="system",
        reason="worker crashed",
    )
    assert reopened["status"] == "OPEN"
    assert reopened["attempt"] == 1
    assert reopened["assigned_assignment_id"] is None

    second = _bind(ledger, request, worker="v2")
    current = handoffs.get(hid)
    assert current is not None
    assert current["status"] == "ASSIGNED"
    assert current["attempt"] == 2
    assert current["assigned_assignment_id"] == second["assignment_id"]

    by_id = {record["assignment_id"]: record for record in ledger.assignments()}
    assert by_id[first["assignment_id"]]["state"] == "STALE"
    assert by_id[second["assignment_id"]]["state"] == "ASSIGNED"

    with pytest.raises(ValueError, match="does not match current"):
        handoffs.reopen(
            hid,
            assignment_id=first["assignment_id"],
            actor="system",
            reason="late crash report",
        )
    with pytest.raises(ValueError, match="does not match current"):
        handoffs.resolve(
            hid,
            assignment_id=first["assignment_id"],
            actor="reviewer",
            summary="late result",
        )

    resolved = handoffs.resolve(
        hid,
        assignment_id=second["assignment_id"],
        actor="reviewer",
        summary="review completed",
    )
    assert resolved["status"] == "RESOLVED"
    assert resolved["resolution_assignment_id"] == second["assignment_id"]
    assert handoffs.pending() == []
    assert handoffs.resolve(
        hid,
        assignment_id=second["assignment_id"],
        actor="reviewer",
        summary="duplicate completion",
    )["status"] == "RESOLVED"


def test_handoff_resolution_requires_a_real_bound_assignment(tmp_path):
    plan, ledger, handoffs = _handoffs(tmp_path)
    request = _open(handoffs, plan.snapshot()["plan_version"])
    bound = _bind(ledger, request)

    with pytest.raises(ValueError, match="does not match current"):
        handoffs.resolve(
            request["handoff_id"],
            assignment_id="fake",
            actor="reviewer",
            summary="forged completion",
        )

    assert handoffs.resolve(
        request["handoff_id"],
        assignment_id=bound["assignment_id"],
        actor="reviewer",
        summary="real completion",
    )["status"] == "RESOLVED"


def test_handoff_terminal_states_are_final(tmp_path):
    plan, _, handoffs = _handoffs(tmp_path)
    version = plan.snapshot()["plan_version"]

    stale = _open(handoffs, version, kind="INTERPRET", subject_id="exp-1")
    stale = handoffs.stale(stale["handoff_id"], reason="bound subject retired")
    assert stale["status"] == "STALE"
    assert stale["target_role"] == "researcher"
    with pytest.raises(ValueError, match="already STALE"):
        handoffs.cancel(stale["handoff_id"], actor="main", reason="late cancel")

    cancelled = _open(handoffs, version, kind="ESCALATE", subject_id="blocker-1")
    cancelled = handoffs.cancel(cancelled["handoff_id"], actor="main", reason="no longer needed")
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["target_role"] == "main"


def test_handoff_rejects_invalid_open_and_transition(tmp_path):
    plan, _, handoffs = _handoffs(tmp_path)
    version = plan.snapshot()["plan_version"]

    with pytest.raises(ValueError, match="kind must be one of"):
        _open(handoffs, version, kind="EXECUTE")
    with pytest.raises(ValueError, match="subject_type must be one of"):
        _open(handoffs, version, subject_type="EVIDENCE")
    with pytest.raises(ValueError, match="stale plan_version"):
        _open(handoffs, version - 1)
    with pytest.raises(ValueError, match="unknown plan node"):
        _open(handoffs, version, node_id="missing")
    with pytest.raises(ValueError, match="unknown parent handoff"):
        _open(handoffs, version, parent_handoff_id="missing")

    request = _open(handoffs, version)
    with pytest.raises(ValueError, match="expected ASSIGNED"):
        handoffs.resolve(
            request["handoff_id"],
            assignment_id="a1",
            actor="reviewer",
            summary="too early",
        )


def test_legacy_assignment_remains_unlinked(tmp_path):
    plan, ledger, _ = _handoffs(tmp_path)
    assignment = ledger.assign(
        worker="r1",
        role="researcher",
        node_id="root",
        plan_version=plan.snapshot()["plan_version"],
        task="ordinary work",
    )

    assert "handoff_id" not in assignment
    assert "handoff_id" not in ledger.assignments()[0]
