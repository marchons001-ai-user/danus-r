from __future__ import annotations

import json

import pytest

from danus.core import ResearchPlanGraph


def _commit(plan: ResearchPlanGraph, operations, *, reason="needed", post_hoc=False, evidence_refs=None):
    base = plan.snapshot()["plan_version"]
    change_id = plan.propose_change(
        base_plan_version=base,
        proposer="researcher",
        reason=reason,
        operations=operations,
        post_hoc=post_hoc,
        evidence_refs=evidence_refs,
    )
    review = plan.review_change(
        change_id,
        reviewer="reviewer",
        approve=True,
        rationale="Material route change is justified.",
    )
    assert review["status"] == "APPROVED"
    return change_id, plan.commit_change(change_id, committer="supervisor")


def test_plan_expands_only_through_reviewed_revisions(tmp_path):
    plan = ResearchPlanGraph.create(
        tmp_path,
        node_id="A2",
        title="Build Danus-R research system",
        summary="Top-level direction only.",
    )
    assert plan.snapshot()["plan_version"] == 1
    assert (tmp_path / "plan" / "revisions" / "0001.json").is_file()

    change_id = plan.propose_change(
        base_plan_version=1,
        proposer="researcher",
        reason="A2-6 now needs a concrete research-plan branch.",
        operations=[
            {
                "op": "add_node",
                "node_id": "A2-6",
                "title": "Research plan control",
                "summary": "Versioned coarse research route.",
                "parent": "A2",
            }
        ],
    )
    assert "A2-6" not in plan.snapshot()["nodes"]
    assert plan.get_change(change_id)["status"] == "PROPOSED"

    plan.review_change(
        change_id,
        reviewer="reviewer",
        approve=True,
        rationale="This is a material route-level expansion, not task detail.",
    )
    version = plan.commit_change(change_id, committer="supervisor")

    current = plan.snapshot()
    assert version == current["plan_version"] == 2
    assert current["committed_change_id"] == change_id
    assert current["nodes"]["A2-6"]["state"] == "ACTIVE"
    assert plan.revision(1)["nodes"].keys() == {"A2"}
    assert "A2-6" in plan.revision(2)["nodes"]
    assert plan.get_change(change_id)["status"] == "COMMITTED"


def test_rejected_change_cannot_commit(tmp_path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    change_id = plan.propose_change(
        base_plan_version=1,
        proposer="researcher",
        reason="Try a branch.",
        operations=[{"op": "add_node", "node_id": "x", "title": "X", "parent": "root"}],
    )
    plan.review_change(
        change_id,
        reviewer="reviewer",
        approve=False,
        rationale="This is execution detail, not a material research-route change.",
    )
    with pytest.raises(ValueError, match="not approved"):
        plan.commit_change(change_id, committer="supervisor")
    assert plan.snapshot()["plan_version"] == 1


def test_concurrent_approved_change_becomes_stale_after_other_commit(tmp_path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ids = []
    for node in ("a", "b"):
        cid = plan.propose_change(
            base_plan_version=1,
            proposer="researcher",
            reason=f"Add direction {node}.",
            operations=[
                {"op": "add_node", "node_id": node, "title": node.upper(), "parent": "root"}
            ],
        )
        plan.review_change(
            cid,
            reviewer="reviewer",
            approve=True,
            rationale="Material and bounded.",
        )
        ids.append(cid)

    assert plan.commit_change(ids[0], committer="supervisor") == 2
    with pytest.raises(ValueError, match="stale change"):
        plan.commit_change(ids[1], committer="supervisor")
    assert plan.get_change(ids[1])["status"] == "STALE"


def test_direct_current_rewrite_is_detected(tmp_path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    path = tmp_path / "plan" / "current.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["nodes"]["root"]["title"] = "Silently changed"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="diverges from committed revision"):
        plan.snapshot()


def test_constraints_are_checked_before_review(tmp_path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="L1", title="L1")
    for level in range(2, 6):
        _commit(
            plan,
            [{
                "op": "add_node",
                "node_id": f"L{level}",
                "title": f"L{level}",
                "parent": f"L{level - 1}",
            }],
        )

    with pytest.raises(ValueError, match="maximum depth 5"):
        plan.propose_change(
            base_plan_version=plan.snapshot()["plan_version"],
            proposer="researcher",
            reason="Too deep.",
            operations=[{"op": "add_node", "node_id": "L6", "title": "L6", "parent": "L5"}],
        )

    _commit(
        plan,
        [{"op": "add_node", "node_id": "peer", "title": "Peer", "parent": "L1"}],
    )
    _commit(plan, [{"op": "set_dependencies", "node_id": "L2", "depends_on": ["peer"]}])
    with pytest.raises(ValueError, match="dependency cycle"):
        plan.propose_change(
            base_plan_version=plan.snapshot()["plan_version"],
            proposer="researcher",
            reason="Would create a cycle.",
            operations=[{"op": "set_dependencies", "node_id": "peer", "depends_on": ["L2"]}],
        )


def test_post_hoc_change_requires_evidence_and_node_ids_cannot_be_redefined(tmp_path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    with pytest.raises(ValueError, match="requires evidence_refs"):
        plan.propose_change(
            base_plan_version=1,
            proposer="researcher",
            reason="Observed result changes the route.",
            operations=[{"op": "set_state", "node_id": "root", "state": "SUPERSEDED"}],
            post_hoc=True,
        )
    with pytest.raises(ValueError, match="unsupported plan operation"):
        plan.propose_change(
            base_plan_version=1,
            proposer="researcher",
            reason="Attempt to rewrite existing meaning.",
            operations=[{"op": "update_node", "node_id": "root", "title": "New meaning"}],
        )


def test_a2_6a_plan_is_migrated_to_versioned_history(tmp_path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    (plan_dir / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "root_id": "root",
                "nodes": {
                    "root": {
                        "title": "Root",
                        "summary": "",
                        "parent": None,
                        "depends_on": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    plan = ResearchPlanGraph(tmp_path)
    snapshot = plan.snapshot()
    assert snapshot["schema_version"] == 2
    assert snapshot["plan_version"] == 1
    assert snapshot["nodes"]["root"]["state"] == "ACTIVE"
    assert plan.revision(1) == snapshot
