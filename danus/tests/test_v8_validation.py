"""Three focused V8 validation-gate regressions; no Codex calls."""
import json

import pytest

from danus import v8
from danus.tui.v8_state import load_v8_project_snapshot


def new_store(tmp_path):
    s = v8.Store(tmp_path / "runtime" / "v8" / "pilot")
    v8.initialize(s, "Determine whether a causal estimator improves MSE, allowing null results")
    return s


def dispatch(s, role, task, purpose="work", **kwargs):
    return v8.apply_decision(s, {"action": "dispatch", "role": role,
        "task": task, "purpose": purpose, "reason": "Scientific validation required",
        **kwargs}, "low")


def complete(s, task, **result):
    s.append("RUNNING", task_id=task)
    v8._worker_result(s, task, {"action": "complete", "summary": "Measured and reported",
                                **result})


def approved(s, name, checks):
    r = dispatch(s, "researcher", "Research protocol " + name)
    complete(s, r, proposal="Bounded causal estimator protocol " + name,
             required_checks=checks)
    pid = list(s.state()["proposals"])[-1]
    reviewer = dispatch(s, "reviewer", "Review exact protocol " + name,
                        "review_experiment", proposal_id=pid)
    complete(s, reviewer, verdict="APPROVE")
    return pid


def experiment(s, pid, name, csv_content, reported_mean=2.0):
    root = s.root / "workspace" / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "metrics.csv").write_text(csv_content)
    (root / "report.json").write_text(json.dumps({"mse": reported_mean}))
    task = dispatch(s, "experimenter", "Execute " + name, "experiment", proposal_id=pid)
    complete(s, task, artifacts=[name + "/metrics.csv", name + "/report.json"])
    return task


def close_review(s):
    interp = dispatch(s, "researcher", "Interpret the latest validated results", "interpret")
    complete(s, interp)
    reviewer = dispatch(s, "reviewer", "Review the latest interpretation", "final_review")
    complete(s, reviewer, verdict="INCONCLUSIVE")


def checks(name, count=2):
    return [
        {"id": "integrity", "kind": "asset_integrity"},
        {"id": "rows", "kind": "csv_rows", "path": name + "/metrics.csv",
         "expected_rows": count},
        {"id": "mse_recompute", "kind": "csv_mean",
         "path": name + "/metrics.csv", "column": "mse",
         "report_path": name + "/report.json", "report_key": "mse",
         "abs_tolerance": 1e-12},
    ]


def test_missing_required_checks_are_debt_not_scientific_inconclusive(tmp_path):
    s = new_store(tmp_path)
    pid = approved(s, "no-checks", [])
    task = experiment(s, pid, "case1", "mse\n1\n3\n")
    assert s.state()["tasks"][task]["status"] == "COMPLETE"
    assert s.state()["latest_validation"]["status"] == "INCOMPLETE"
    close_review(s)
    with pytest.raises(PermissionError, match="VALIDATION_DEBT"):
        v8.apply_decision(s, {"action": "finish", "reason": "Mistake missing tests for inconclusive"}, "low")
    assert not s.state()["done"]


def test_independent_checks_pass_and_inconclusive_science_can_finish(tmp_path, monkeypatch):
    s = new_store(tmp_path)
    pid = approved(s, "validated", checks("case2"))
    task = experiment(s, pid, "case2", "mse\n1\n3\n")
    validation = s.state()["latest_validation"]
    assert validation["task_id"] == task
    assert validation["status"] == "PASSED"
    assert len(validation["checks"]) == 3
    assert all(v["status"] == "PASSED" for v in validation["checks"])
    monkeypatch.setenv("DANUS_RUNTIME", str(tmp_path / "runtime"))
    snap = load_v8_project_snapshot("pilot")
    assert snap["validation_status"] == "PASSED"
    assert any("PASSED" in item["label"] for item in snap["experiments"])
    close_review(s)
    assert v8.apply_decision(s, {"action": "finish", "reason": "Valid but inconclusive"}, "low") is None
    assert s.state()["done"]


def test_failed_check_requires_authorized_new_run_and_preserves_old_evidence(tmp_path):
    s = new_store(tmp_path)
    a = approved(s, "faulty", checks("case3", count=3))
    original = experiment(s, a, "case3", "mse\n1\n3\n")
    assert s.state()["latest_validation"]["status"] == "FAILED"
    assert any(r["id"] == "rows" and r["status"] == "FAILED"
               for r in s.state()["latest_validation"]["checks"])
    # Reviewer cannot rescue a failing check with prose verdicts.
    close_review(s)
    with pytest.raises(PermissionError, match="VALIDATION_DEBT"):
        v8.apply_decision(s, {"action": "finish", "reason": "Invalid run"}, "low")
    b = approved(s, "corrective", checks("case4"))
    replacement = experiment(s, b, "case4", "mse\n1\n3\n")
    assert s.state()["latest_validation"]["status"] == "PASSED"
    assert s.state()["latest_validation"]["task_id"] == replacement
    assert s.state()["validations"][original]["status"] == "FAILED"
    assert s.state()["assets"]["case3/metrics.csv"]["task_id"] == original
    assert s.state()["assets"]["case4/metrics.csv"]["task_id"] == replacement
    close_review(s)
    v8.apply_decision(s, {"action": "finish", "reason": "Only corrected run supports conclusion"}, "low")
    assert s.state()["done"]


def test_unexecutable_and_duplicated_check_specs_rejected_before_approval():
    from danus.v8_validation import normalize_checks
    with pytest.raises(ValueError, match="unsupported"):
        normalize_checks([{"id": "causality", "kind": "trust_my_checks_json"}])
    with pytest.raises(ValueError, match="duplicate"):
        normalize_checks([{"id": "a", "kind": "asset_integrity"},
                          {"id": "a", "kind": "asset_integrity"}])



def test_new_unverified_experiment_cannot_inherit_earlier_pass(tmp_path):
    s = new_store(tmp_path)
    pid = approved(s, "first", checks("first"))
    experiment(s, pid, "first", "mse\n1\n3\n")
    close_review(s)
    new_pid = approved(s, "second", checks("second"))
    newest = dispatch(s, "experimenter", "Run second", "experiment", proposal_id=new_pid)
    s.append("RUNNING", task_id=newest)
    s.append("RESULT", task_id=newest, status="BLOCKED",
             summary="execution interrupted", proposal=None, review=None, verdict="")
    assert s.state()["latest_validation"]["status"] == "INCOMPLETE"
    assert s.state()["latest_validation"]["task_id"] == newest
    with pytest.raises(PermissionError):
        v8.apply_decision(s, {"action": "finish", "reason": "Reuse first validation"}, "low")



def test_two_repeated_unresolved_check_sets_escalate_to_high(tmp_path, monkeypatch):
    s = new_store(tmp_path)
    for i in range(2):
        s.append("VALIDATION", task_id=f"task-{i}",
                 proposal_id=f"proposal-{i}", status="INCOMPLETE",
                 reason="same mandatory checker missing",
                 checks=[{"id": "causality", "status": "INCOMPLETE",
                          "kind": "csv_rows", "detail": "not executed"}])
    levels = []
    def supervisor(store, level, error=""):
        levels.append(level)
        return {"action": "block", "reason": "No lawful executable validation route"}
    monkeypatch.setattr(v8, "_supervisor", supervisor)
    assert v8.step(s) == "CONTROL"
    assert levels == ["high"]
    assert s.state()["halt"]


@pytest.mark.parametrize(
    "checker_exit, tool_exit, expected",
    [(1, 0, "FAILED"), (0, 1, "FAILED"),
     (0, 0, "PASSED"), (0, None, "INCOMPLETE")],
)
def test_checker_receipt_uses_host_owned_tool_exit(
        tmp_path, checker_exit, tool_exit, expected):
    s = new_store(tmp_path)
    name = "check_pilot"
    proposal = (f"Run from workspace: python -B {name}/run.py. "
                f"The parent runs --check and writes {name}/run_log.json.")
    research = dispatch(s, "researcher", "Propose check contract")
    complete(s, research, proposal=proposal, required_checks=checks(name))
    pid = list(s.state()["proposals"])[-1]
    reviewer = dispatch(s, "reviewer", "Review check contract",
                        "review_experiment", proposal_id=pid)
    complete(s, reviewer, verdict="APPROVE")
    task = dispatch(s, "experimenter", "Execute check contract",
                    "experiment", proposal_id=pid)
    out = s.root / "workspace" / name
    out.mkdir(parents=True)
    (out / "metrics.csv").write_text("mse\n1\n3\n")
    (out / "report.json").write_text('{"mse": 2}')
    (out / "run_log.json").write_text(
        json.dumps({"checker_exit_code": checker_exit, "parent_exit_code": 0}))
    (out / "run.py").write_text("print('check pilot')\n")
    s.append("RUNNING", task_id=task)
    if tool_exit is not None:
        logs = s.root / "logs"
        logs.mkdir()
        (logs / f"{task}-mock.jsonl").write_text(json.dumps({
            "type": "item.completed",
            "item": {"type": "command_execution",
                     "command": f"python -B {name}/run.py",
                     "exit_code": tool_exit},
        }) + "\n")
    v8._worker_result(s, task, {
        "action": "complete", "summary": "Checker executed or failed",
        "artifacts": [f"{name}/metrics.csv", f"{name}/report.json",
                      f"{name}/run_log.json", f"{name}/run.py"],
    })
    validation = s.state()["latest_validation"]
    assert validation["status"] == expected
    receipt = next(r for r in validation["checks"]
                   if r["id"] == "checker_execution")
    assert receipt["status"] == expected
    assert s.state()["tasks"][task]["status"] == "COMPLETE"
