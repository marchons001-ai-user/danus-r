from __future__ import annotations

import json
from pathlib import Path

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core._util import append_jsonl, read_jsonl
from danus.core.handoff import HandoffRequests
from danus.execution import context as C
from danus.orchestration import autonomy as A
from danus.orchestration import cli as CLI
from danus.orchestration import supervisor_llm as S
from danus.orchestration.control import build_supervisor_snapshot, reduce_supervisor_snapshot


def _report(root: Path):
    return reduce_supervisor_snapshot(build_supervisor_snapshot(root, []))


def _setup_ambiguity(root: Path):
    ResearchPlanGraph.create(root, node_id="root", title="Root", summary="route reviewed evidence")
    ledger = ProgressLedger(root)

    source_dir = root / "workers" / "experimenter"
    source_dir.mkdir(parents=True)
    source_task = "run bounded experiment\n"
    (source_dir / "TASK.md").write_text(source_task, encoding="utf-8")
    source = ledger.assign(
        worker="experimenter",
        role="experimenter",
        node_id="root",
        plan_version=1,
        task=source_task,
    )
    ledger.update(
        worker="experimenter",
        state="BLOCKED",
        actor="runtime",
        note="valid evidence needs scientific disposition",
        outcome="NEEDS_REVIEW",
    )
    C.write_checkpoint(
        source_dir,
        role="experimenter",
        round_no=1,
        telemetry={"last_agent_message": "metrics conflict\nDANUS_OUTCOME: NEEDS_REVIEW"},
    )

    handoffs = HandoffRequests(ledger)
    review = handoffs.open(
        kind="REVIEW",
        plan_version=1,
        node_id="root",
        subject_type="ASSIGNMENT",
        subject_id=source["assignment_id"],
        requested_by="experimenter",
        source_role="experimenter",
        source_assignment_id=source["assignment_id"],
        request="review the ambiguous result",
        reason="independent scientific judgment required",
    )
    reviewer_dir = root / "workers" / "reviewer"
    reviewer_dir.mkdir(parents=True)
    (reviewer_dir / "TASK.md").write_text(review["request"], encoding="utf-8")
    reviewer = ledger.assign(
        worker="reviewer",
        role="reviewer",
        node_id="root",
        plan_version=1,
        task=review["request"],
        handoff_id=review["handoff_id"],
    )
    ledger.update(worker="reviewer", state="DONE", actor="runtime", outcome="COMPLETE")
    C.write_checkpoint(
        reviewer_dir,
        role="reviewer",
        round_no=1,
        telemetry={"last_agent_message": "review complete; disposition remains ambiguous\nDANUS_OUTCOME: COMPLETE"},
    )
    handoffs.resolve(
        review["handoff_id"],
        assignment_id=reviewer["assignment_id"],
        actor="supervisor",
        summary="generic assignment review completed; source disposition remains explicit",
    )
    action = next(
        item for item in _report(root)["actions"]
        if item.get("kind") == "SCIENTIFIC_ROUTE_REQUIRED"
    )
    return ledger, source, review, reviewer, action


def _luna_result(route: str = "INTERPRET", *, tool_calls: int = 0, reply: str | None = None):
    if reply is None:
        reply = json.dumps({
            "route": route,
            "rationale": "The reviewed evidence requires this bounded scientific route.",
            "basis": ["node:root"],
        })
    return {
        "status": "completed",
        "error": "",
        "tool_calls": tool_calls,
        "reply": reply,
        "usage": {"input": 100, "output": 20, "reasoning": 0},
    }


def _decision_events(ledger: ProgressLedger):
    return [
        event for event in read_jsonl(ledger.path)
        if event.get("event") == "SUPERVISOR_ROUTE_DECIDED"
    ]


def _scientific_handoffs(ledger: ProgressLedger, source_id: str):
    return [
        item for item in HandoffRequests(ledger).all()
        if item.get("kind") in {"INTERPRET", "RETHINK", "ESCALATE"}
        and item.get("subject_type") == "ASSIGNMENT"
        and item.get("subject_id") == source_id
    ]


def test_decision_key_is_stable_and_review_bound(tmp_path: Path):
    _, _, _, _, action = _setup_ambiguity(tmp_path)
    assert S._decision_key(action) == S._decision_key(dict(action))

    changed = dict(action)
    changed["review_assignment_id"] = "different-review-attempt"
    assert S._decision_key(changed) != S._decision_key(action)


def test_durable_decision_and_handoff_replay_without_second_luna(tmp_path: Path, monkeypatch):
    ledger, source, _, _, action = _setup_ambiguity(tmp_path)
    calls = []
    monkeypatch.setattr(S, "_run_luna", lambda prompt: calls.append(prompt) or _luna_result("INTERPRET"))

    first = S.resolve_scientific_routes(tmp_path, [action])
    assert len(calls) == 1
    assert first[0]["status"] == "valid"
    assert first[0]["route"] == "INTERPRET"
    assert first[0]["replayed"] is False
    assert len(_decision_events(ledger)) == 1
    lineage = _scientific_handoffs(ledger, source["assignment_id"])
    assert len(lineage) == 1
    assert lineage[0]["kind"] == "INTERPRET"

    monkeypatch.setattr(S, "_run_luna", lambda prompt: pytest.fail("durable decision must suppress Luna"))
    second = S.resolve_scientific_routes(tmp_path, [action])
    assert second[0]["replayed"] is True
    assert second[0]["handoff_id"] == first[0]["handoff_id"]
    assert len(_decision_events(ledger)) == 1
    assert len(_scientific_handoffs(ledger, source["assignment_id"])) == 1


def test_crash_after_decision_append_replays_without_luna(tmp_path: Path, monkeypatch):
    ledger, source, _, _, action = _setup_ambiguity(tmp_path)
    calls = []
    monkeypatch.setattr(S, "_run_luna", lambda prompt: calls.append(prompt) or _luna_result("RETHINK"))
    materialize = S._materialize_decision
    monkeypatch.setattr(
        S,
        "_materialize_decision",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        S.resolve_scientific_routes(tmp_path, [action])
    assert len(calls) == 1
    assert len(_decision_events(ledger)) == 1
    assert not _scientific_handoffs(ledger, source["assignment_id"])

    monkeypatch.setattr(S, "_materialize_decision", materialize)
    monkeypatch.setattr(S, "_run_luna", lambda prompt: pytest.fail("cold-start replay must not call Luna"))
    replay = S.resolve_scientific_routes(tmp_path, [action])
    assert replay[0]["replayed"] is True
    assert replay[0]["route"] == "RETHINK"
    assert len(_scientific_handoffs(ledger, source["assignment_id"])) == 1


@pytest.mark.parametrize(
    "result",
    [
        _luna_result(reply="not-json"),
        _luna_result(tool_calls=1),
        {"status": "failed", "error": "timeout", "tool_calls": 0, "reply": "", "usage": {}},
    ],
)
def test_model_protocol_failure_persists_one_fail_closed_escalation(
    tmp_path: Path,
    monkeypatch,
    result,
):
    ledger, source, _, _, action = _setup_ambiguity(tmp_path)
    calls = []
    monkeypatch.setattr(S, "_run_luna", lambda prompt: calls.append(prompt) or result)

    decision = S.resolve_scientific_routes(tmp_path, [action])[0]
    assert len(calls) == 1
    assert decision["status"] == "fail_closed"
    assert decision["route"] == "ESCALATE"
    assert decision["decision_source"] == "fail_closed"
    event = _decision_events(ledger)[0]
    assert event["route"] == "ESCALATE"
    assert event["decision_source"] == "fail_closed"
    lineage = _scientific_handoffs(ledger, source["assignment_id"])
    assert len(lineage) == 1
    assert lineage[0]["target_role"] == "main"

    monkeypatch.setattr(S, "_run_luna", lambda prompt: pytest.fail("fail-closed decision must replay"))
    replay = S.resolve_scientific_routes(tmp_path, [action])[0]
    assert replay["replayed"] is True
    assert replay["route"] == "ESCALATE"


def test_conflicting_duplicate_decision_fails_closed_without_luna(tmp_path: Path, monkeypatch):
    ledger, _, _, _, action = _setup_ambiguity(tmp_path)
    monkeypatch.setattr(S, "_run_luna", lambda prompt: _luna_result("INTERPRET"))
    S.resolve_scientific_routes(tmp_path, [action])

    conflict = dict(_decision_events(ledger)[0])
    conflict["route"] = "RETHINK"
    conflict["request"] = "conflicting request"
    append_jsonl(ledger.path, conflict)
    monkeypatch.setattr(S, "_run_luna", lambda prompt: pytest.fail("decision conflict must not call Luna"))

    result = S.resolve_scientific_routes(tmp_path, [action])[0]
    assert result["status"] == "failed"
    assert result["error"].startswith("decision_conflict:")


def test_assignment_rethink_completes_and_is_labeled_as_rethink(tmp_path: Path, monkeypatch):
    ledger, source, _, _, action = _setup_ambiguity(tmp_path)
    monkeypatch.setattr(S, "_run_luna", lambda prompt: _luna_result("RETHINK"))
    decision = S.resolve_scientific_routes(tmp_path, [action])[0]
    handoff = HandoffRequests(ledger).get(decision["handoff_id"])
    assert handoff and handoff["kind"] == "RETHINK"

    researcher = ledger.assign(
        worker="researcher",
        role="researcher",
        node_id="root",
        plan_version=1,
        task=handoff["request"],
        handoff_id=handoff["handoff_id"],
    )
    ledger.update(worker="researcher", state="DONE", actor="runtime", outcome="COMPLETE")
    report = _report(tmp_path)
    resolve = next(
        item for item in report["actions"]
        if item.get("kind") == "RESOLVE_HANDOFF"
        and item.get("handoff_id") == handoff["handoff_id"]
    )
    assert resolve["summary"] == "assignment rethink completed; source disposition remains explicit"
    assert A._routing_label(resolve) == "RETHINK"

    HandoffRequests(ledger).resolve(
        handoff["handoff_id"],
        assignment_id=researcher["assignment_id"],
        actor="supervisor",
        summary=resolve["summary"],
    )
    assert not any(
        item.get("kind") == "SCIENTIFIC_ROUTE_REQUIRED"
        for item in _report(tmp_path)["actions"]
    )
    assert source["assignment_id"]


@pytest.mark.parametrize("terminal", ["CANCELLED", "STALE", "RESOLVED"])
def test_terminal_scientific_route_lineage_suppresses_original_ambiguity(
    tmp_path: Path,
    monkeypatch,
    terminal: str,
):
    ledger, _, _, _, action = _setup_ambiguity(tmp_path)
    monkeypatch.setattr(S, "_run_luna", lambda prompt: _luna_result("INTERPRET"))
    decision = S.resolve_scientific_routes(tmp_path, [action])[0]
    handoffs = HandoffRequests(ledger)
    handoff = handoffs.get(decision["handoff_id"])
    assert handoff

    if terminal == "CANCELLED":
        handoffs.cancel(handoff["handoff_id"], actor="test", reason="operator cancelled route")
    elif terminal == "STALE":
        handoffs.stale(handoff["handoff_id"], actor="test", reason="route became stale")
    else:
        assignment = ledger.assign(
            worker="researcher",
            role="researcher",
            node_id="root",
            plan_version=1,
            task=handoff["request"],
            handoff_id=handoff["handoff_id"],
        )
        ledger.update(worker="researcher", state="DONE", actor="runtime", outcome="COMPLETE")
        handoffs.resolve(
            handoff["handoff_id"],
            assignment_id=assignment["assignment_id"],
            actor="test",
            summary="interpretation completed",
        )

    assert not any(
        item.get("kind") == "SCIENTIFIC_ROUTE_REQUIRED"
        for item in _report(tmp_path)["actions"]
    )


def test_watch_replays_same_decision_without_second_luna(tmp_path: Path, monkeypatch, capsys):
    _, _, _, _, action = _setup_ambiguity(tmp_path)
    report = {
        "project_state": "RUNNING",
        "convergence": {"stable": True},
        "budget_stop": None,
        "quota_policy": {"block_new_codex_rounds": False},
        "actions": [action],
    }
    monkeypatch.setattr(S.L, "project_dir", lambda project: tmp_path)
    monkeypatch.setattr(CLI, "watch_supervisor", lambda *args, **kwargs: iter([dict(report), dict(report)]))
    monkeypatch.setattr(
        CLI,
        "do_supervise",
        lambda *args, **kwargs: {
            "project_state": "RUNNING",
            "convergence": {"stable": True},
            "budget_stop": None,
            "quota_policy": {"block_new_codex_rounds": False},
            "actions": [],
        },
    )
    calls = []
    monkeypatch.setattr(S, "_run_luna", lambda prompt: calls.append(prompt) or _luna_result("INTERPRET"))

    assert S.supervise_main(["project-x", "--watch", "--poll", "0.01"]) == 0
    assert len(calls) == 1
    rendered = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(rendered) == 2
    assert rendered[0]["scientific_route_decisions"][0]["replayed"] is False
    assert rendered[1]["scientific_route_decisions"][0]["replayed"] is True
