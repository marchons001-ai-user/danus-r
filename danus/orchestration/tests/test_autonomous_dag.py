"""A2-8b autonomous DAG execution regressions. No LLM or network."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.execution import layout as L
from danus.orchestration import cli
from danus.orchestration.control import reconcile


@contextmanager
def _agents_root(tmp_path: Path):
    old = os.environ.get("DANUS_AGENTS_ROOT")
    os.environ["DANUS_AGENTS_ROOT"] = str(tmp_path / "projects")
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("DANUS_AGENTS_ROOT", None)
        else:
            os.environ["DANUS_AGENTS_ROOT"] = old


def _project(tmp_path: Path):
    ctx = _agents_root(tmp_path)
    ctx.__enter__()
    cli.do_new("P", roles="low:2")
    pdir = L.project_dir("P")
    (L.worker_dir("P", "low") / L.ROLE_FILE).write_text(
        "ROLE=researcher\nREASONING_EFFORT=low\n", encoding="utf-8"
    )
    (L.worker_dir("P", "low2") / L.ROLE_FILE).write_text(
        "ROLE=reviewer\nREASONING_EFFORT=low\nBACKEND=dsh\n", encoding="utf-8"
    )
    graph = ResearchPlanGraph.create(pdir, node_id="P0", title="Root")
    change = graph.propose_change(
        base_plan_version=1,
        proposer="test",
        reason="two-step route",
        operations=[
            {"op": "add_node", "node_id": "A", "title": "Research", "summary": "", "parent": "P0", "depends_on": []},
            {"op": "add_node", "node_id": "B", "title": "Review", "summary": "", "parent": "P0", "depends_on": ["A"]},
        ],
    )
    graph.review_change(change, reviewer="test", approve=True, rationale="fixture")
    assert graph.commit_change(change, committer="test") == 2
    return ctx, pdir


def _task(pdir: Path, role: str, node_id: str, text: str) -> Path:
    path = pdir / "tasks" / "v2" / role / f"{node_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return path


def test_supervisor_dispatches_completes_and_advances_dag(tmp_path: Path, monkeypatch):
    ctx, pdir = _project(tmp_path)
    try:
        _task(pdir, "researcher", "A", "do A")
        _task(pdir, "reviewer", "B", "review B")
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")

        first = cli.do_supervise("P", apply=True)
        dispatch_a = next(x for x in first["autonomy_applied"] if x["action"] == "AUTO_DISPATCH")
        assert dispatch_a["node_id"] == "A"
        assert dispatch_a["worker"] == "low"
        assert dispatch_a["start_result"] == "started"

        ledger = ProgressLedger(pdir)
        a = ledger.current("low")
        assert a and a["node_id"] == "A" and a["state"] == "ASSIGNED"
        ledger.update(worker="low", state="DONE", actor="runtime", outcome="COMPLETE")

        second = cli.do_supervise("P", apply=True)
        assert any(
            x["action"] == "AUTO_COMPLETE" and x["node_id"] == "A" and x["result"] == "completed"
            for x in second["autonomy_applied"]
        )
        assert "A" in ledger.completed_nodes()

        third = cli.do_supervise("P", apply=True)
        dispatch_b = next(x for x in third["autonomy_applied"] if x["action"] == "AUTO_DISPATCH")
        assert dispatch_b["node_id"] == "B"
        assert dispatch_b["worker"] == "low2"
        b = ledger.current("low2")
        assert b and b["node_id"] == "B" and b["state"] == "ASSIGNED"
        ledger.update(worker="low2", state="DONE", actor="runtime", outcome="COMPLETE")

        fourth = cli.do_supervise("P", apply=True)
        actions = fourth["autonomy_applied"]
        assert any(x["action"] == "AUTO_COMPLETE" and x["node_id"] == "B" for x in actions)
        assert any(x["action"] == "AUTO_COMPLETE_CONTAINER" and x["node_id"] == "P0" for x in actions)

        final = cli.do_supervise("P")
        assert final["project_state"] == "COMPLETE"
        assert final["frontier"] == []
        assert final["decisions"] == []
        assert {x["node_id"] for x in ledger.assignments()} == {"A", "B"}
    finally:
        ctx.__exit__(None, None, None)


def test_watch_immediately_observes_autonomous_transitions(tmp_path: Path, monkeypatch):
    ctx, pdir = _project(tmp_path)
    try:
        _task(pdir, "researcher", "A", "do A")
        _task(pdir, "reviewer", "B", "review B")
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        monkeypatch.setattr(cli.Q, "refresh_quota", lambda pdir: {})
        monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)

        watch = cli.watch_supervisor("P", heartbeat=999999, poll=0.01)
        first = next(watch)
        assert any(x["action"] == "AUTO_DISPATCH" and x["node_id"] == "A" for x in first["autonomy_applied"])

        ledger = ProgressLedger(pdir)
        ledger.update(worker="low", state="DONE", actor="runtime", outcome="COMPLETE")
        second = next(watch)
        assert any(x["action"] == "AUTO_COMPLETE" and x["node_id"] == "A" for x in second["autonomy_applied"])

        third = next(watch)
        assert any(x["action"] == "AUTO_DISPATCH" and x["node_id"] == "B" for x in third["autonomy_applied"])
        ledger.update(worker="low2", state="DONE", actor="runtime", outcome="COMPLETE")

        fourth = next(watch)
        assert any(x["action"] == "AUTO_COMPLETE" and x["node_id"] == "B" for x in fourth["autonomy_applied"])
        fifth = next(watch)
        assert fifth["project_state"] == "COMPLETE"
        assert fifth["decisions"] == []
        try:
            next(watch)
        except StopIteration:
            pass
        else:
            raise AssertionError("completed watch did not exit")
    finally:
        ctx.__exit__(None, None, None)


def test_changed_task_is_not_auto_completed(tmp_path: Path, monkeypatch):
    ctx, pdir = _project(tmp_path)
    try:
        task = _task(pdir, "researcher", "A", "original A")
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        cli.do_supervise("P", apply=True)
        ledger = ProgressLedger(pdir)
        ledger.update(worker="low", state="DONE", actor="runtime", outcome="COMPLETE")
        task.write_text("changed after assignment\n", encoding="utf-8")

        report = cli.do_supervise("P", apply=True)
        result = next(x for x in report["autonomy_applied"] if x["action"] == "AUTO_COMPLETE")
        assert result["result"] == "task_changed"
        assert "A" not in ledger.completed_nodes()
    finally:
        ctx.__exit__(None, None, None)


def test_missing_auto_task_preserves_manual_pending_assignment(tmp_path: Path):
    ctx, _ = _project(tmp_path)
    try:
        report = cli.do_supervise("P", apply=True)
        assert report["autonomy_applied"] == []
        pending = next(x for x in report["applied"] if x["action"] == "RETASK")
        assert pending["result"] == "pending_assignment"
    finally:
        ctx.__exit__(None, None, None)


def test_superseded_done_assignment_does_not_review_new_worker_task(tmp_path: Path):
    ctx, pdir = _project(tmp_path)
    try:
        ledger = ProgressLedger(pdir)
        old = ledger.assign(
            worker="low2", role="reviewer", node_id="P0", plan_version=2, task="old plan review\n"
        )
        ledger.update(worker="low2", state="DONE", actor="runtime", outcome="COMPLETE")
        ledger.set_node_state("A", state="DONE", actor="supervisor")
        new = ledger.assign(
            worker="low2", role="reviewer", node_id="B", plan_version=2, task="new node review\n"
        )

        report = reconcile(pdir, cli._project_rows("P"))
        assert not any(d.get("assignment_id") == old["assignment_id"] for d in report["decisions"])
        assert any(d.get("assignment_id") == new["assignment_id"] for d in report["decisions"])
    finally:
        ctx.__exit__(None, None, None)
