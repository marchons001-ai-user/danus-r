from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.execution import layout as L
from danus.orchestration import cli, control, planning
from danus.orchestration import supervisor_llm as S


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


def _change(change_id: str, proposal_id: str, status: str = "APPROVED"):
    return {
        "change_id": change_id,
        "base_plan_version": 1,
        "proposer": "researcher",
        "reason": f"[proposal:{proposal_id}] route",
        "operations": [],
        "post_hoc": False,
        "evidence_refs": [],
        "status": status,
        "review_verdict": "APPROVE" if status == "APPROVED" else None,
    }


def _snapshot(changes):
    assignments = []
    handoffs = []
    for index, change in enumerate(changes, 1):
        if change["status"] != "APPROVED":
            continue
        assignment_id = f"review-assignment-{index}"
        handoff_id = f"review-{index}"
        assignments.append({
            "assignment_id": assignment_id,
            "worker": f"reviewer-{index}",
            "role": "reviewer",
            "node_id": "P0",
            "plan_version": 1,
            "state": "DONE",
            "outcome": "COMPLETE",
        })
        handoffs.append({
            "handoff_id": handoff_id,
            "kind": "REVIEW",
            "status": "RESOLVED",
            "plan_version": 1,
            "node_id": "P0",
            "subject_type": "PLAN_CHANGE",
            "subject_id": change["change_id"],
            "target_role": "reviewer",
            "resolution_assignment_id": assignment_id,
        })
    return {
        "plan": {
            "plan_version": 1,
            "root_id": "P0",
            "nodes": {
                "P0": {
                    "title": "Project",
                    "summary": "",
                    "parent": None,
                    "depends_on": [],
                    "state": "ACTIVE",
                }
            },
        },
        "plan_changes": list(changes),
        "all_plan_changes": list(changes),
        "current_assignments": [],
        "assignments": assignments,
        "completed_nodes": [],
        "implementation_contracts": [],
        "pending_implementation_contracts": [],
        "handoffs": handoffs,
        "workers": [],
        "project_paused": False,
    }


def test_reducer_blocks_all_competing_approved_plan_commits():
    changes = [_change("change-a", "proposal-a"), _change("change-b", "proposal-b")]
    report = control.reduce_supervisor_snapshot(_snapshot(changes))

    assert not [item for item in report["actions"] if item["kind"] == "COMMIT_PLAN_CHANGE"]
    waits = [
        item for item in report["actions"]
        if item["kind"] == "WAIT" and item.get("reason") == "competing_approved_plan_changes"
    ]
    assert {item.get("change_id") for item in waits} == {"change-a", "change-b"}
    assert all(item.get("competing_change_ids") == ["change-a", "change-b"] for item in waits)


def test_reducer_competing_plan_result_is_order_independent():
    changes = [_change("change-a", "proposal-a"), _change("change-b", "proposal-b")]
    forward = control.reduce_supervisor_snapshot(_snapshot(changes))["actions"]
    reverse = control.reduce_supervisor_snapshot(_snapshot(list(reversed(changes))))["actions"]
    assert forward == reverse


def test_reducer_keeps_single_approved_plan_commit_path():
    report = control.reduce_supervisor_snapshot(_snapshot([_change("change-a", "proposal-a")]))
    commits = [item for item in report["actions"] if item["kind"] == "COMMIT_PLAN_CHANGE"]
    assert [item["change_id"] for item in commits] == ["change-a"]


def test_reducer_does_not_treat_approved_plus_proposed_as_approved_conflict():
    changes = [
        _change("change-a", "proposal-a", "APPROVED"),
        _change("change-b", "proposal-b", "PROPOSED"),
    ]
    report = control.reduce_supervisor_snapshot(_snapshot(changes))
    assert any(
        item["kind"] == "COMMIT_PLAN_CHANGE" and item.get("change_id") == "change-a"
        for item in report["actions"]
    )
    assert not any(item.get("reason") == "competing_approved_plan_changes" for item in report["actions"])


def test_planning_executor_fails_closed_before_selecting_competing_approved_plan(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        planning.initialize_research("P", brief="Study a bounded question.")
        pdir = L.project_dir("P")
        graph = ResearchPlanGraph(pdir)
        first = graph.propose_change(
            base_plan_version=1,
            proposer="researcher",
            reason="[proposal:proposal-a] route a",
            operations=[{
                "op": "add_node", "node_id": "A", "title": "A", "summary": "",
                "parent": "P0", "depends_on": [],
            }],
        )
        second = graph.propose_change(
            base_plan_version=1,
            proposer="researcher",
            reason="[proposal:proposal-b] route b",
            operations=[{
                "op": "add_node", "node_id": "B", "title": "B", "summary": "",
                "parent": "P0", "depends_on": [],
            }],
        )
        graph.review_change(first, reviewer="reviewer", verdict="APPROVE", rationale="valid a")
        graph.review_change(second, reviewer="reviewer", verdict="APPROVE", rationale="valid b")

        actions = planning.planning_step("P")
        blocked = next(item for item in actions if item.get("reason") == "competing_approved_plan_changes")
        assert blocked["action"] == "PLAN_GOVERNANCE"
        assert blocked["result"] == "blocked"
        assert blocked["change_ids"] == sorted([first, second])
        assert graph.snapshot()["plan_version"] == 1
        assert not (pdir / "tasks" / "v2").exists()


def test_supervise_watch_uses_research_planning_loop_for_research_project(tmp_path: Path, monkeypatch, capsys):
    with _root(tmp_path):
        pdir = L.project_dir("P")
        pdir.mkdir(parents=True)
        (pdir / planning.BRIEF_FILE).write_text("bounded\n", encoding="utf-8")
        calls = []
        report = {"project_state": "RUNNING", "actions": []}
        monkeypatch.setattr(
            planning,
            "watch_research",
            lambda project, **kwargs: calls.append((project, kwargs)) or iter([dict(report)]),
        )
        monkeypatch.setattr(
            cli,
            "watch_supervisor",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("raw watch must not run")),
        )
        monkeypatch.setattr(S, "_route_report", lambda project, value, token_budget: dict(value))

        assert S.supervise_main(["P", "--watch", "--poll", "0.25", "--token-budget", "17"]) == 0
        assert calls == [("P", {"heartbeat": 1800.0, "poll": 0.25, "token_budget": 17})]
        assert json.loads(capsys.readouterr().out)["project_state"] == "RUNNING"


def test_supervise_watch_keeps_raw_supervisor_for_nonresearch_project(tmp_path: Path, monkeypatch, capsys):
    with _root(tmp_path):
        L.project_dir("P").mkdir(parents=True)
        calls = []
        report = {"project_state": "RUNNING", "actions": []}
        monkeypatch.setattr(
            cli,
            "watch_supervisor",
            lambda project, **kwargs: calls.append((project, kwargs)) or iter([dict(report)]),
        )
        monkeypatch.setattr(
            planning,
            "watch_research",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("planning watch must not run")),
        )
        monkeypatch.setattr(S, "_route_report", lambda project, value, token_budget: dict(value))

        assert S.supervise_main(["P", "--watch", "--poll", "0.5"]) == 0
        assert calls == [("P", {"heartbeat": 1800.0, "poll": 0.5, "token_budget": 0})]
        assert json.loads(capsys.readouterr().out)["project_state"] == "RUNNING"


def test_supervise_watch_captures_researcher_plan_proposal(tmp_path: Path, monkeypatch, capsys):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        init = planning.initialize_research("P", brief="Study a bounded question.")
        pdir = L.project_dir("P")
        ProgressLedger(pdir).update(
            worker="low", state="DONE", actor="runtime", outcome="COMPLETE"
        )
        proposal = Path(init["proposal_handoff"])
        proposal.parent.mkdir(parents=True, exist_ok=True)
        proposal.write_text(json.dumps({
            "assignment_id": ProgressLedger(pdir).current("low")["assignment_id"],
            "base_plan_version": 1,
            "reason": "small route",
            "post_hoc": False,
            "evidence_refs": [],
            "operations": [{
                "op": "add_node", "node_id": "R1", "title": "Research", "summary": "",
                "parent": "P0", "depends_on": [],
            }],
            "tasks": [{"node_id": "R1", "role": "researcher", "task": "research task"}],
        }), encoding="utf-8")

        def one_report(*args, **kwargs):
            yield {
                "project_state": "RUNNING",
                "convergence": {"stable": True},
                "budget_stop": None,
                "quota_policy": {"block_new_codex_rounds": False},
                "actions": [],
                "decisions": [],
            }

        monkeypatch.setattr(cli, "watch_supervisor", one_report)
        assert S.supervise_main(["P", "--watch"]) == 0
        rendered = json.loads(capsys.readouterr().out)
        assert any(item["action"] == "PLAN_PROPOSAL" for item in rendered["planning_applied"])
        assert not proposal.exists()
        assert ResearchPlanGraph(pdir).pending_changes()
