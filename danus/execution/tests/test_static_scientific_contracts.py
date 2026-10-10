"""Offline contract-drift checks for human-readable scientific role instructions.

These checks verify the *delivered* prompt, the tool surface and the existing
metric reducer API. They do not claim a model is guaranteed to make a good
scientific judgment.
"""
from __future__ import annotations

import inspect
import math
from pathlib import Path

import pytest

from danus.core import metrics
from danus.execution import loop
from danus.gateway import server
from danus.gateway.roles import tools_for
from danus.orchestration.autonomy import _implementation_review_task


def test_scientific_role_contracts_are_injected_for_every_new_round(tmp_path, monkeypatch):
    project = tmp_path / "P"
    project.mkdir()
    (project / "RESEARCH_CHARTER.md").write_text(
        "# Research Charter\nDo not change scientific scope\n", encoding="utf-8"
    )
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(tmp_path))
    researcher = loop._scientific_context("P", "researcher", "low")
    reviewer = loop._scientific_context("P", "reviewer", "low2")
    experimenter = loop._scientific_context("P", "experimenter", "low3")

    assert "Make the proposed Experimenter task scientifically executable" in researcher
    for field in ("goal", "must_preserve_invariants", "forbidden_behaviors", "acceptance_conditions"):
        assert field in researcher

    assert "Implementation Contract: mandatory execution-readiness review" in reviewer
    assert "PASS / MISSING / NOT APPLICABLE" in reviewer
    assert "REVISE" in reviewer
    assert "scientific task-readiness" in reviewer

    for needle in (
        "Fixed Experimenter engineering interface",
        "experiment_freeze_protocol(",
        "experiment_run(protocol_id",
        "def reduce_metrics(protocol: dict, artifacts: list[Path]) -> dict[str, float]:",
        'protocol["primary_metrics"]',
        "No Reviewer APPROVE",
        "Missing scientific input",
    ):
        assert needle in experimenter
    assert "Do not construct IDs" in experimenter
    assert "Do not install dependencies" in experimenter


def test_review_assignment_explicitly_demands_task_completeness():
    task = _implementation_review_task({
        "contract_id": "0123456789abcdef",
        "node_id": "E1",
        "plan_version": 3,
        "task": "# Implementation Contract\nGoal:\nPilot\n",
    })
    assert "without inventing decision-relevant" in task
    assert "finite runs/seeds/stopping/resource policy" in task
    assert "PASS/MISSING/NOT APPLICABLE" in task
    assert "REVISE" in task
    assert 'implementation_review_contract(contract_id="0123456789abcdef"' in task


def test_experimenter_guide_matches_real_gateway_tool_and_reducer_api():
    contract = Path(__file__).resolve().parents[3] / "agents" / "contracts" / "experimenter.md"
    text = contract.read_text(encoding="utf-8")
    params = inspect.signature(server.experiment_freeze_protocol).parameters
    for name in (
        "question", "hypothesis", "matrix", "primary_metrics", "decision_rule",
        "metric_reducer_path", "execution_files", "metric_reducer_function",
    ):
        assert name in params and name in text
    assert params["metric_reducer_function"].default == "reduce_metrics"
    assert list(inspect.signature(server.experiment_run).parameters) == ["protocol_id"]
    assert "experiment_freeze_protocol" in tools_for("experimenter")
    assert "experiment_freeze_protocol" not in tools_for("researcher")
    assert "experiment_freeze_protocol" not in tools_for("reviewer")

    # The sandboxed reducer runner always passes (protocol, list[Path]).
    runner = metrics._RUNNER_SOURCE
    assert "result = reducer(" in runner
    assert 'request["protocol"]' in runner
    assert '[Path(path) for path in request["artifacts"]]' in runner

    protocol = {"primary_metrics": ["mean_error"]}
    assert metrics._normalize_metrics(protocol, {"mean_error": 0.125}) == {"mean_error": 0.125}
    for value in (True, None, float("nan"), float("inf"), [0.125]):
        with pytest.raises(ValueError):
            metrics._normalize_metrics(protocol, {"mean_error": value})
    with pytest.raises(ValueError, match="keys"):
        metrics._normalize_metrics(protocol, {"wrong_metric": 0.125})
