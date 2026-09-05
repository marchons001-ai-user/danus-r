from __future__ import annotations

import json
from pathlib import Path

import pytest

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.execution import context as C
from danus.orchestration import cli as CLI
from danus.orchestration import supervisor_llm as S


def _packet():
    return {
        "source": {"assignment_id": "source-1"},
        "candidate_routes": ["INTERPRET", "RETHINK", "ESCALATE"],
        "allowed_basis": [
            "node:root",
            "source_assignment:source-1",
            "review_handoff:review-1",
            "review_assignment:review-assignment-1",
        ],
    }


def _result(reply: str, *, tool_calls: int = 0, status: str = "completed"):
    return {
        "status": status,
        "error": "boom" if status != "completed" else "",
        "tool_calls": tool_calls,
        "reply": reply,
        "usage": {"input": 10, "output": 4, "reasoning": 2},
    }


def test_valid_route_reply_is_accepted():
    decision = S._validate(
        _packet(),
        _result(json.dumps({
            "route": "INTERPRET",
            "rationale": "The evidence is valid but needs scientific synthesis.",
            "basis": ["source_assignment:source-1", "review_handoff:review-1"],
        })),
    )

    assert decision["status"] == "valid"
    assert decision["route"] == "INTERPRET"
    assert decision["model"] == "gpt-5.6-luna"
    assert decision["effort"] == "low"


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (_result("```json\n{}\n```"), "invalid_json"),
        (_result(json.dumps({"route": "REVIEW", "rationale": "x", "basis": ["node:root"]})), "route_not_allowed"),
        (_result(json.dumps({"route": "INTERPRET", "rationale": "x", "basis": ["made-up"]})), "invalid_basis"),
        (_result(json.dumps({"route": "INTERPRET", "rationale": "x", "basis": ["node:root"], "extra": 1})), "invalid_schema"),
        (_result(json.dumps({"route": "INTERPRET", "rationale": "x", "basis": ["node:root"]}), tool_calls=1), "tool_use_forbidden"),
        (_result("", status="failed"), "boom"),
    ],
)
def test_invalid_or_tool_using_reply_fails_closed(result, error):
    decision = S._validate(_packet(), result)
    assert decision["status"] == "failed"
    assert decision["error"] == error
    assert "route" not in decision


def test_non_chatgpt_backend_never_launches_luna(monkeypatch):
    monkeypatch.setattr(S.codex, "backend", lambda: "api")
    result = S._run_luna("route this")
    assert result["status"] == "failed"
    assert result["model"] == "gpt-5.6-luna"
    assert result["effort"] == "low"
    assert result["error"] == "restricted Supervisor requires CODEX_BACKEND=chatgpt"


def test_build_route_packet_uses_verified_source_and_review_checkpoints(tmp_path: Path):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root", summary="compare valid evidence")
    ledger = ProgressLedger(tmp_path)

    source_dir = tmp_path / "workers" / "experimenter"
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
        note="agent declared NEEDS_REVIEW",
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
        request="review the conflicting evidence",
        reason="independent scientific judgment required",
    )
    reviewer_dir = tmp_path / "workers" / "reviewer"
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
    ledger.update(
        worker="reviewer",
        state="DONE",
        actor="runtime",
        outcome="COMPLETE",
        result=(
            "protocol valid; global superiority is not established\n"
            "DANUS_REVIEW_CLASS: SCIENTIFIC_AMBIGUITY\n"
            "DANUS_OUTCOME: COMPLETE"
        ),
    )
    C.write_checkpoint(
        reviewer_dir,
        role="reviewer",
        round_no=1,
        telemetry={"last_agent_message": "protocol valid; global superiority is not established\nDANUS_OUTCOME: COMPLETE"},
    )
    handoffs.resolve(
        review["handoff_id"],
        assignment_id=reviewer["assignment_id"],
        actor="supervisor",
        summary="generic assignment review completed; source disposition remains explicit",
    )

    action = {
        "kind": "SCIENTIFIC_ROUTE_REQUIRED",
        "plan_version": 1,
        "node_id": "root",
        "assignment_id": source["assignment_id"],
        "worker": "experimenter",
        "reason": "review_completed_source_disposition_ambiguous",
        "subject_type": "ASSIGNMENT",
        "subject_id": source["assignment_id"],
        "source_role": "experimenter",
        "review_handoff_id": review["handoff_id"],
        "review_assignment_id": reviewer["assignment_id"],
        "candidate_routes": ["INTERPRET", "RETHINK", "ESCALATE"],
    }
    packet = S.build_route_packet(tmp_path, action)

    assert packet["source"]["final_message"].startswith("metrics conflict")
    assert packet["review"]["final_message"].startswith("protocol valid")
    assert packet["candidate_routes"] == ["INTERPRET", "RETHINK", "ESCALATE"]
    assert packet["allowed_basis"] == [
        "node:root",
        f"source_assignment:{source['assignment_id']}",
        f"review_handoff:{review['handoff_id']}",
        f"review_assignment:{reviewer['assignment_id']}",
    ]


def test_manual_apply_consults_only_after_stable_deterministic_supervision(monkeypatch, capsys):
    report = {
        "project_state": "RUNNING",
        "convergence": {"stable": True},
        "budget_stop": None,
        "quota_policy": {"block_new_codex_rounds": False},
        "actions": [{"kind": "SCIENTIFIC_ROUTE_REQUIRED", "subject_id": "a1"}],
    }
    monkeypatch.setattr(CLI, "do_supervise", lambda *args, **kwargs: dict(report))
    monkeypatch.setattr(S, "apply_scientific_route_dispositions", lambda project: [])
    calls = []
    monkeypatch.setattr(
        S,
        "resolve_scientific_routes",
        lambda root, actions: calls.append((root, actions)) or [{"status": "valid", "route": "INTERPRET"}],
    )

    assert S.supervise_main(["project-x", "--apply"]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    assert rendered["scientific_route_decisions"] == [{"status": "valid", "route": "INTERPRET"}]


@pytest.mark.parametrize(
    "override",
    [
        {"convergence": {"stable": False}},
        {"budget_stop": "resource_token_budget_exceeded"},
        {"quota_policy": {"block_new_codex_rounds": True}},
        {"project_state": "PAUSED"},
    ],
)
def test_manual_apply_gates_prevent_llm_call(monkeypatch, capsys, override):
    report = {
        "project_state": "RUNNING",
        "convergence": {"stable": True},
        "budget_stop": None,
        "quota_policy": {"block_new_codex_rounds": False},
        "actions": [{"kind": "SCIENTIFIC_ROUTE_REQUIRED", "subject_id": "a1"}],
    }
    report.update(override)
    monkeypatch.setattr(CLI, "do_supervise", lambda *args, **kwargs: dict(report))
    monkeypatch.setattr(S, "apply_scientific_route_dispositions", lambda project: [])
    monkeypatch.setattr(
        S,
        "resolve_scientific_routes",
        lambda *args, **kwargs: pytest.fail("restricted LLM must not run behind this gate"),
    )

    assert S.supervise_main(["project-x", "--apply"]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["scientific_route_decisions"] == []


def test_dry_run_delegates_without_llm(monkeypatch):
    delegated = []
    monkeypatch.setattr(CLI, "main", lambda passed: delegated.append(passed) or 0)
    monkeypatch.setattr(
        S,
        "resolve_scientific_routes",
        lambda *args, **kwargs: pytest.fail("dry-run must not call restricted LLM"),
    )

    assert S.supervise_main(["project-x"]) == 0
    assert delegated == [["supervise", "project-x"]]
