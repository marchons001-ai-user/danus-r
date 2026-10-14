"""Focused offline checks for Reviewer-owned scientific repair, no Codex calls."""
import json

import pytest

from danus import v8
from danus.tui.v8_state import load_v8_project_snapshot


def init(tmp_path):
    store = v8.Store(tmp_path / "runtime" / "v8" / "review-loop")
    v8.initialize(store, "Compare two independently reproducible observations")
    return store


def dispatch(store, role, purpose="work", task="Exact scientific work", **kwargs):
    return v8.apply_decision(store, {"action": "dispatch", "role": role,
        "purpose": purpose, "task": task, "reason": "Evidence-based next action",
        **kwargs}, "low")


def complete(store, task_id, **result):
    store.append("RUNNING", task_id=task_id)
    v8._worker_result(store, task_id, {
        "action": "complete", "summary": "Independently documented", **result})


def approved(store, label):
    researcher = dispatch(store, "researcher", task="Specify protocol " + label)
    complete(store, researcher, proposal="Fixed study " + label,
             required_checks=[{"id": "hashes", "kind": "asset_integrity"}])
    pid = next(reversed(store.state()["proposals"]))
    reviewer = dispatch(store, "reviewer", "review_experiment", proposal_id=pid)
    complete(store, reviewer, verdict="APPROVE")
    return pid


def perform(store, proposal, filename):
    (store.root / "workspace" / filename).write_text("recorded observation\n")
    experimenter = dispatch(store, "experimenter", "experiment", proposal_id=proposal)
    complete(store, experimenter, artifacts=[filename])
    assert store.state()["latest_validation"]["status"] == "PASSED"
    researcher = dispatch(store, "researcher", "interpret",
                          task="Interpret evidence " + filename)
    complete(store, researcher)
    return dispatch(store, "reviewer", "final_review")


def test_final_reviewer_receives_exact_proposal_and_structured_repair_loop(tmp_path, monkeypatch):
    store = init(tmp_path)
    proposal = approved(store, "initial")
    reviewer = perform(store, proposal, "initial.csv")
    prompts = []
    def reviewer_call(model, effort, prompt, workdir, log_path, *, writable=False, timeout=1200):
        payload = json.loads(prompt.split("Current assignment:\n", 1)[1])
        prompts.append(payload)
        return {"action": "complete", "summary": "Directed connection check missing",
                "verdict": "REVISE", "issues": [{
                    "problem": "Directed edge orientation not verified",
                    "impact": "Could reverse reachability",
                    "required_action": "Independently reconstruct directed pairs",
                    "target_role": "experimenter",
                    "evidence": "Initial experiment has only a hash check",
                }]}
    monkeypatch.setattr(v8, "_call", reviewer_call)
    v8._worker(store, reviewer, timeout=40)
    assert prompts[0]["approved_experiment_proposal"]["id"] == proposal
    assert "Fixed study initial" in prompts[0]["approved_experiment_proposal"]["content"]
    issues = store.state()["review_issues"]
    issue_id = next(iter(issues))
    assert issues[issue_id]["status"] == "OPEN"
    assert issues[issue_id]["target_role"] == "experimenter"
    assert store.state()["final_review"]["verdict"] == "REVISE"
    with pytest.raises(PermissionError):
        v8.apply_decision(store, {"action": "finish", "reason": "Skip required fix"}, "low")

    # Reviewer feedback is delivered directly to a corrective Researcher,
    # not reconstructed from a truncated Supervisor summary.
    corrective = dispatch(store, "researcher", task="Design independently approved correction")
    followup = []
    def correction_call(model, effort, prompt, workdir, log_path, *, writable=False, timeout=1200):
        payload = json.loads(prompt.split("Current assignment:\n", 1)[1])
        followup.append(payload)
        return {"action": "complete", "summary": "Revised plan prepared",
                "proposal": "Fixed study corrected",
                "required_checks": [{"id": "hashes", "kind": "asset_integrity"}]}
    monkeypatch.setattr(v8, "_call", correction_call)
    v8._worker(store, corrective, timeout=40)
    assert followup[0]["open_review_issues"][0]["id"] == issue_id
    assert followup[0]["open_review_issues"][0]["required_action"] == (
        "Independently reconstruct directed pairs")

    new_pid = next(reversed(store.state()["proposals"]))
    approval = dispatch(store, "reviewer", "review_experiment", proposal_id=new_pid)
    complete(store, approval, verdict="APPROVE")
    latest = perform(store, new_pid, "corrected.csv")
    # A plain ACCEPT cannot silently drop a prior blocking finding.
    store.append("RUNNING", task_id=latest)
    with pytest.raises(ValueError, match="unresolved Reviewer issues"):
        v8._worker_result(store, latest, {
            "action": "complete", "summary": "Implicitly accept", "verdict": "ACCEPT"})
    v8._worker_result(store, latest, {
        "action": "complete", "summary": "Independently verified correction",
        "verdict": "ACCEPT", "resolved_issues": [{
        "id": issue_id, "status": "RESOLVED",
        "reason": "Corrected experiment independently checked direction",
        "evidence": "Corrected result verified against source pairs",
    }]})
    assert store.state()["review_issues"][issue_id]["status"] == "RESOLVED"
    v8.apply_decision(store, {"action": "finish", "reason": "Reviewed repair"}, "low")
    assert store.state()["done"]


def test_nonblocking_duplicate_test_can_be_reasonedly_waived(tmp_path, monkeypatch):
    store = init(tmp_path)
    proposal = approved(store, "bounded")
    review = perform(store, proposal, "bounded.csv")
    complete(store, review, verdict="ACCEPT", accepted_deviations=[{
        "item": "Second output re-read with the same parser",
        "reason": "Logical ablation: only a serialization mismatch is at risk; "
                  "a separate raw-output reconstruction already excluded that fault",
        "evidence": "Independent per-record read and comparison",
    }])
    assert store.state()["final_review"]["feedback"]["accepted_deviations"]
    monkeypatch.setenv("DANUS_RUNTIME", str(tmp_path / "runtime"))
    snapshot = load_v8_project_snapshot("review-loop")
    assert snapshot["open_review_issues"] == 0
    assert snapshot["review_verdict"] == "ACCEPT"
    v8.apply_decision(store, {"action": "finish", "reason": "Narrow valid conclusion"}, "low")
    assert store.state()["done"]


def test_valid_scientific_inconclusive_and_no_silent_waiver(tmp_path):
    store = init(tmp_path)
    pid = approved(store, "null")
    first = perform(store, pid, "null.csv")
    with pytest.raises(ValueError, match="actionable issues"):
        complete(store, first, verdict="REVISE")
    # Still running: bad review responses cannot be silently accepted.
    assert store.state()["tasks"][first]["status"] == "RUNNING"
    v8._worker_result(store, first, {
        "action": "complete", "summary": "Evidence cannot support a difference",
        "verdict": "INCONCLUSIVE"})
    v8.apply_decision(store, {"action": "finish", "reason": "Valid null finding"}, "low")
    assert store.state()["done"]
