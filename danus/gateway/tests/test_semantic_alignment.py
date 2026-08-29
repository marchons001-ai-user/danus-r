from __future__ import annotations

import os
from contextlib import contextmanager

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.gateway import server
from danus.gateway.roles import tools_for


@contextmanager
def _env(**values):
    old = {key: os.environ.get(key) for key in values}
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


def test_semantic_gate_authority_is_role_gated():
    assert "implementation_pending_contracts" in tools_for("researcher")
    assert "implementation_submit_contract" in tools_for("researcher")
    assert "implementation_review_contract" not in tools_for("researcher")

    assert "implementation_pending_contracts" in tools_for("reviewer")
    assert "implementation_review_contract" in tools_for("reviewer")
    assert "implementation_submit_contract" not in tools_for("reviewer")

    assert not any(name.startswith("implementation_") for name in tools_for("experimenter"))

    assert "implementation_pending_contracts" in tools_for("main")
    assert "implementation_submit_contract" not in tools_for("main")
    assert "implementation_review_contract" not in tools_for("main")


def test_semantic_gate_round_trip_is_offline(tmp_path):
    plan = ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")

    with _env(DANUS_PROJECT_DIR=tmp_path, DANUS_AUTHOR="researcher"):
        proposed = server.implementation_submit_contract(
            node_id="root",
            plan_version=1,
            goal="Keep DSH as the Reviewer primary backend.",
            must_preserve_invariants=["Reviewer remains the intentional heterogeneous role."],
            forbidden_behaviors=["Do not make Codex or Terra primary while DSH is usable."],
            acceptance_conditions=["Terra is used only after explicit DSH QUOTA."],
        )
        pending = server.implementation_pending_contracts()

    assert proposed["status"] == "PROPOSED"
    assert pending["count"] == 1
    assert pending["contracts"][0]["contract_id"] == proposed["contract_id"]

    with _env(DANUS_PROJECT_DIR=tmp_path, DANUS_AUTHOR="reviewer"):
        reviewed = server.implementation_review_contract(
            proposed["contract_id"],
            verdict="APPROVE",
            rationale="The implementation contract matches the intended heterogeneous policy.",
        )

    assert reviewed["status"] == "APPROVE"
    ledger = ProgressLedger(tmp_path)
    assigned = ledger.assign(
        worker="experimenter",
        role="experimenter",
        node_id="root",
        plan_version=plan.snapshot()["plan_version"],
        task=proposed["task"],
    )
    assert assigned["implementation_contract_id"] == proposed["contract_id"]
    assert ledger.pending_implementation_contracts() == []
