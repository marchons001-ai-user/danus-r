from __future__ import annotations

import os
from contextlib import contextmanager

from danus.core import ResearchPlanGraph
from danus.gateway import server
from danus.gateway.roles import tools_for


@contextmanager
def _env(**values):
    old = {k: os.environ.get(k) for k in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_plan_change_gate_round_trip_is_offline(tmp_path):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")

    with _env(DANUS_PROJECT_DIR=tmp_path, DANUS_AUTHOR="researcher"):
        proposed = server.plan_propose_change(
            base_plan_version=1,
            reason="A material branch is now needed.",
            operations=[
                {"op": "add_node", "node_id": "branch", "title": "Branch", "parent": "root"}
            ],
        )
        pending = server.plan_pending_changes()

    assert pending["count"] == 1
    assert pending["changes"][0]["change_id"] == proposed["change_id"]
    assert pending["changes"][0]["status"] == "PROPOSED"

    with _env(DANUS_PROJECT_DIR=tmp_path, DANUS_AUTHOR="reviewer"):
        reviewed = server.plan_review_change(
            proposed["change_id"],
            approve=True,
            rationale="Route-level change is necessary and bounded.",
        )
        approved = server.plan_pending_changes()

    assert approved["changes"][0]["status"] == "APPROVED"

    with _env(DANUS_PROJECT_DIR=tmp_path, DANUS_AUTHOR="supervisor"):
        committed = server.plan_commit_change(proposed["change_id"])
        assert server.plan_pending_changes()["count"] == 0

    assert proposed["status"] == "PROPOSED"
    assert reviewed["status"] == "APPROVED"
    assert committed["status"] == "COMMITTED"
    assert committed["plan_version"] == 2
    assert "branch" in ResearchPlanGraph(tmp_path).snapshot()["nodes"]


def test_plan_change_authority_is_role_gated():
    assert "plan_pending_changes" in tools_for("researcher")
    assert "plan_propose_change" in tools_for("researcher")
    assert "plan_review_change" not in tools_for("researcher")
    assert "plan_commit_change" not in tools_for("researcher")

    assert "plan_pending_changes" in tools_for("reviewer")
    assert "plan_review_change" in tools_for("reviewer")
    assert "plan_propose_change" not in tools_for("reviewer")
    assert "plan_commit_change" not in tools_for("reviewer")

    assert not any(name.startswith("plan_") for name in tools_for("experimenter"))

    assert "plan_pending_changes" in tools_for("main")
    assert "plan_commit_change" in tools_for("main")
    assert "plan_propose_change" not in tools_for("main")
    assert "plan_review_change" not in tools_for("main")
