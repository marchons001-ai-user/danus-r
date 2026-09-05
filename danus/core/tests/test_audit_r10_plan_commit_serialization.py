from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest

from danus.core import ResearchPlanGraph


def _approved_pair(plan: ResearchPlanGraph) -> list[str]:
    change_ids = []
    for node_id in ("a", "b"):
        change_id = plan.propose_change(
            base_plan_version=1,
            proposer="researcher",
            reason=f"Add route {node_id}.",
            operations=[
                {
                    "op": "add_node",
                    "node_id": node_id,
                    "title": node_id.upper(),
                    "parent": "root",
                }
            ],
        )
        plan.review_change(
            change_id,
            reviewer="reviewer",
            verdict="APPROVE",
            rationale="Material route change.",
        )
        change_ids.append(change_id)
    return change_ids


def _commit_after_snapshot(
    root: str,
    change_id: str,
    ready,
    peer_ready,
    results,
) -> None:
    graph = ResearchPlanGraph(Path(root))
    snapshot = graph.snapshot

    def gated_snapshot():
        current = snapshot()
        ready.set()
        peer_ready.wait(0.5)
        return current

    graph.snapshot = gated_snapshot  # type: ignore[method-assign]
    try:
        version = graph.commit_change(change_id, committer="supervisor")
        results.put((change_id, "COMMITTED", version))
    except ValueError as exc:
        results.put((change_id, "ERROR", str(exc)))


def test_concurrent_plan_commits_serialize_at_core_boundary(tmp_path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    change_ids = _approved_pair(plan)

    ctx = mp.get_context("fork")
    ready_a = ctx.Event()
    ready_b = ctx.Event()
    results = ctx.Queue()
    processes = [
        ctx.Process(
            target=_commit_after_snapshot,
            args=(str(tmp_path), change_ids[0], ready_a, ready_b, results),
        ),
        ctx.Process(
            target=_commit_after_snapshot,
            args=(str(tmp_path), change_ids[1], ready_b, ready_a, results),
        ),
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(5)
        assert not process.is_alive()
        assert process.exitcode == 0

    outcomes = [results.get(timeout=1) for _ in processes]
    committed = [item for item in outcomes if item[1] == "COMMITTED"]
    stale = [item for item in outcomes if item[1] == "ERROR" and "stale change" in item[2]]
    assert len(committed) == 1
    assert len(stale) == 1

    current = plan.snapshot()
    winner = committed[0][0]
    loser = stale[0][0]
    assert current["plan_version"] == 2
    assert current["committed_change_id"] == winner
    assert plan.revision(2) == current
    assert not (tmp_path / "plan" / "revisions" / "0003.json").exists()
    assert plan.get_change(winner)["status"] == "COMMITTED"
    assert plan.get_change(winner)["committed_version"] == 2
    assert plan.get_change(loser)["status"] == "STALE"
    assert plan.get_change(loser)["committed_version"] is None


def test_commit_lock_preserves_same_change_crash_recovery(tmp_path, monkeypatch):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    change_id = plan.propose_change(
        base_plan_version=1,
        proposer="researcher",
        reason="Add route.",
        operations=[{"op": "add_node", "node_id": "a", "title": "A", "parent": "root"}],
    )
    plan.review_change(
        change_id,
        reviewer="reviewer",
        verdict="APPROVE",
        rationale="Material route change.",
    )

    write_json = plan._write_json
    failed = False

    def fail_before_commit_record(path, payload):
        nonlocal failed
        if (
            not failed
            and path == plan._change_path(change_id)
            and payload.get("status") == "COMMITTED"
        ):
            failed = True
            raise RuntimeError("simulated crash before commit record")
        return write_json(path, payload)

    monkeypatch.setattr(plan, "_write_json", fail_before_commit_record)
    with pytest.raises(RuntimeError, match="simulated crash"):
        plan.commit_change(change_id, committer="supervisor")

    assert plan.snapshot()["plan_version"] == 2
    assert plan.get_change(change_id)["status"] == "APPROVED"

    monkeypatch.setattr(plan, "_write_json", write_json)
    assert plan.commit_change(change_id, committer="supervisor") == 2
    assert plan.get_change(change_id)["status"] == "COMMITTED"
    assert plan.get_change(change_id)["committed_version"] == 2
    assert plan.revision(2) == plan.snapshot()
    assert not (tmp_path / "plan" / "revisions" / "0003.json").exists()
