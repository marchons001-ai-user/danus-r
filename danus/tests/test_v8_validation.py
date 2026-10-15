"""Three focused V8 validation-gate regressions; no Codex calls."""
import json

import pytest

from danus import v8, v8_validation
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


def checker_check(name):
    return {"id": "checker_execution", "kind": "checker_execution",
            "path": name + "/run.py", "log_path": name + "/run_log.json"}


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
    "checker_exit, tool_exit, expected, nested",
    [(1, 0, "FAILED", False), (0, 1, "FAILED", False),
     (0, 0, "PASSED", False), (0, None, "INCOMPLETE", False),
     (1, 0, "FAILED", True), (0, 1, "FAILED", True),
     (0, 0, "PASSED", True), (0, None, "INCOMPLETE", True)],
)
def test_checker_receipt_uses_host_owned_tool_exit(
        tmp_path, checker_exit, tool_exit, expected, nested):
    s = new_store(tmp_path)
    name = "check_pilot"
    proposal = (f"Run from workspace: python -B {name}/run.py. "
                f"The parent runs --check and writes {name}/run_log.json.")
    research = dispatch(s, "researcher", "Propose check contract")
    complete(s, research, proposal=proposal,
             required_checks=checks(name) + [checker_check(name)])
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
    if nested:
        receipt = {"children": [
            {"argv": ["python", f"{name}/run.py", "--replay"], "exit_code": 0},
            {"argv": ["python", f"{name}/run.py", "--check"],
             "exit_code": checker_exit},
        ]}
    else:
        receipt = {"checker_exit_code": checker_exit, "parent_exit_code": 0}
    (out / "run_log.json").write_text(json.dumps(receipt))
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


def test_revalidate_latest_checker_incomplete_without_rerunning_experiment(tmp_path):
    s = new_store(tmp_path)
    name = "receipt"
    research = dispatch(s, "researcher", "Propose receipt")
    complete(s, research,
             proposal=f"Run python -B {name}/run.py with --check; write {name}/run_log.json",
             required_checks=checks(name) + [checker_check(name)])
    pid = list(s.state()["proposals"])[-1]
    reviewer = dispatch(s, "reviewer", "Approve receipt",
                        "review_experiment", proposal_id=pid)
    complete(s, reviewer, verdict="APPROVE")
    out = s.root / "workspace" / name
    out.mkdir()
    (out / "metrics.csv").write_text("mse\n1\n3\n")
    (out / "report.json").write_text('{"mse": 2}')
    (out / "run.py").write_text("print(0)\n")
    (out / "run_log.json").write_text(json.dumps({
        "children": [
            {"argv": ["python", f"{name}/run.py", "--replay"], "exit_code": 0},
            {"argv": ["python", f"{name}/run.py", "--check"], "exit_code": 0},
        ]
    }))
    task = dispatch(s, "experimenter", "Run receipt", "experiment",
                    proposal_id=pid)
    complete(s, task, artifacts=[
        f"{name}/metrics.csv", f"{name}/report.json",
        f"{name}/run.py", f"{name}/run_log.json",
    ])
    prior = s.state()["latest_validation"]
    assert prior["status"] == "INCOMPLETE"
    assert prior["checks"][-1]["id"] == "checker_execution"
    logs = s.root / "logs"
    logs.mkdir()
    (logs / f"{task}-real.jsonl").write_text(json.dumps({
        "type": "item.completed",
        "item": {"type": "command_execution",
                 "command": f"python -B {name}/run.py", "exit_code": 0},
    }) + "\n")
    result = v8.revalidate_latest(s, task)
    assert result["status"] == "PASSED"
    assert result["recheck_of_seq"] == prior["seq"]
    state = s.state()
    assert state["latest_validation"]["status"] == "PASSED"
    assert len([e for e in s.read() if e["kind"] == "VALIDATION"]) == 2
    with pytest.raises(ValueError, match="checker-receipt INCOMPLETE"):
        v8.revalidate_latest(s, task)


@pytest.mark.parametrize("command,expected", [
    ("python -B pilot/run.py", True),
    ("python3.12 -u ./pilot/run.py --check", True),
    ("/opt/venv/bin/python -B pilot/run.py", True),
    ("cd /tmp/ws && python -B pilot/run.py", True),
    ("echo 'python -B pilot/run.py'", False),
    ("printf '%s' 'python -B pilot/run.py'", False),
    ("python -c 'print(\\\"python -B pilot/run.py\\\")'", False),
    ("python -B pilot/other.py --note pilot/run.py", False),
    ("python -B pilot/run.py | cat", False),
    ("true && python -B pilot/run.py", False),
    ("cd /wrong && python -B pilot/run.py", False),
])
def test_only_direct_python_invocation_counts(tmp_path, command, expected):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert v8_validation._direct_python_entrypoint(
        command, "pilot/run.py", workspace) is expected


def test_new_checker_contract_is_typed_not_inferred_from_prose(tmp_path):
    s = new_store(tmp_path)
    name = "quoted_checker"
    research = dispatch(s, "researcher", "Mention checker only")
    complete(s, research,
             proposal=(f"Discuss --check and {name}/run_log.json "
                       f"but do not execute python -B {name}/run.py"),
             required_checks=checks(name))
    pid = next(iter(s.state()["proposals"]))
    contract = s.state()["proposals"][pid]
    assert contract["check_contract"] == "explicit_v1"
    reviewer = dispatch(s, "reviewer", "Review", "review_experiment", proposal_id=pid)
    complete(s, reviewer, verdict="APPROVE")
    task = experiment(s, pid, name, "mse\n1\n3\n")
    result = s.state()["validations"][task]
    assert result["status"] == "PASSED"
    assert not any(x["kind"] == "checker_execution" for x in result["checks"])


def test_legacy_checker_prose_still_detected_by_host(tmp_path):
    s = new_store(tmp_path)
    name = "legacy"
    root = s.root / "workspace" / name
    root.mkdir()
    (root / "run.py").write_text("print(0)\n")
    (root / "run_log.json").write_text(json.dumps(
        {"checker_exit_code": 0, "parent_exit_code": 0}))
    task_id = "task-legacy"
    logs = s.root / "logs"
    logs.mkdir()
    (logs / f"{task_id}-mock.jsonl").write_text(json.dumps({
        "type": "item.completed",
        "item": {"type": "command_execution",
                 "command": f"python -B {name}/run.py", "exit_code": 0}
    }) + "\n")
    paths = {f"{name}/run.py": root / "run.py",
             f"{name}/run_log.json": root / "run_log.json"}
    import hashlib
    owned = {
        key: {"task_id": task_id, "bytes": path.stat().st_size,
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for key, path in paths.items()
    }
    proposal = {
        "content": f"Run python -B {name}/run.py, then --check, save {name}/run_log.json",
        "required_checks": [],
        # Historical approved proposals have no check_contract version key.
    }
    receipt = v8_validation._checker_receipt(
        s, task_id, proposal, owned, lambda key: paths[key])
    assert receipt["status"] == "PASSED"


def test_checker_contract_rejects_ambiguous_specs():
    with pytest.raises(ValueError, match="id checker_execution"):
        v8_validation.normalize_checks([
            {"id": "something_else", "kind": "checker_execution",
             "path": "pilot/run.py", "log_path": "pilot/run_log.json"}])
    with pytest.raises(ValueError, match="only one checker_execution"):
        v8_validation.normalize_checks([
            checker_check("pilot"), checker_check("other")])
    with pytest.raises(ValueError, match="safe workspace-relative"):
        v8_validation.normalize_checks([{
            **checker_check("pilot"), "log_path": "../outside.json"}])
