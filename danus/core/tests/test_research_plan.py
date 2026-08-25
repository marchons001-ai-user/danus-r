from __future__ import annotations

import pytest

from danus.core import ResearchPlanGraph


def test_plan_expands_on_demand_without_fixed_layer_types(tmp_path):
    plan = ResearchPlanGraph.create(
        tmp_path,
        node_id="A2",
        title="Build Danus-R research system",
        summary="Top-level direction only.",
    )
    plan.add_node(node_id="A2-6", title="Research plan control", parent="A2")
    plan.add_node(node_id="A2-6a", title="Progressive plan graph", parent="A2-6")

    snapshot = plan.snapshot()
    assert snapshot["root_id"] == "A2"
    assert snapshot["nodes"]["A2-6a"]["parent"] == "A2-6"
    assert set(snapshot["nodes"]["A2-6a"]) == {"title", "summary", "parent", "depends_on"}


def test_plan_allows_five_levels_but_rejects_six(tmp_path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="L1", title="L1")
    for level in range(2, 6):
        plan.add_node(
            node_id=f"L{level}",
            title=f"L{level}",
            parent=f"L{level - 1}",
        )

    with pytest.raises(ValueError, match="maximum depth 5"):
        plan.add_node(node_id="L6", title="L6", parent="L5")
    assert "L6" not in plan.snapshot()["nodes"]


def test_cross_node_dependencies_can_be_added_later_and_must_remain_acyclic(tmp_path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    plan.add_node(node_id="a", title="Direction A", parent="root")
    plan.add_node(node_id="b", title="Direction B", parent="root")
    plan.set_dependencies("a", ["b"])

    assert plan.snapshot()["nodes"]["a"]["depends_on"] == ["b"]
    with pytest.raises(ValueError, match="dependency cycle"):
        plan.set_dependencies("b", ["a"])
    assert plan.snapshot()["nodes"]["b"]["depends_on"] == []


def test_plan_rejects_duplicate_unknown_and_oversized_nodes(tmp_path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")

    with pytest.raises(ValueError, match="duplicate node_id"):
        plan.add_node(node_id="root", title="Duplicate", parent="root")
    with pytest.raises(ValueError, match="unknown parent"):
        plan.add_node(node_id="orphan", title="Orphan", parent="missing")
    with pytest.raises(ValueError, match="unknown dependency"):
        plan.add_node(
            node_id="bad-dep",
            title="Bad dependency",
            parent="root",
            depends_on=["missing"],
        )
    with pytest.raises(ValueError, match="title exceeds 35"):
        plan.add_node(node_id="long-title", title="x" * 36, parent="root")
    with pytest.raises(ValueError, match="summary exceeds 100"):
        plan.add_node(node_id="long-summary", title="Summary", summary="x" * 101, parent="root")
