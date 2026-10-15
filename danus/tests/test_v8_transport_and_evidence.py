"""Targeted regressions from the actual PFL3 Reviewer and Supervisor failures.

No network, Codex, experimental execution or installed new dependencies.
"""
import errno
import json
import subprocess

import pytest

from danus import v8


EVIDENCE = [
    ["pfl3_steering_v1/run.py: episode recovery loop",
     "pfl3_steering_v1/metrics.csv", "pfl3_steering_v1/convergence.csv"],
    ["pfl3_steering_v1/run.py: episode, main and --check",
     "pfl3_steering_v1/report.json", "pfl3_steering_v1/run_log.json"],
    ["pfl3_steering_v1/run.py: xlsx_schema",
     "pfl3_steering_v1/workbook_schema.json", "input/source_data.xlsx"],
    ["pfl3_steering_v1/run.py: decision expression and report construction",
     "pfl3_steering_v1/report.json"],
    ["pfl3_steering_v1/run.py: main and entry point",
     "pfl3_steering_v1/run_log.json"],
]


def test_original_five_reviewer_evidence_lists_are_kept_verbatim_and_bounded():
    result = {"issues": [
        {"problem": f"Problem {i}", "impact": "Could change the conclusion",
         "required_action": "Verify evidence independently",
         "target_role": "experimenter", "evidence": refs}
        for i, refs in enumerate(EVIDENCE, 1)]}
    feedback = v8._review_feedback("task-28", "REVISE", result, {"review_issues": {}})
    assert len(feedback["issues"]) == 5
    for index, (issue, refs) in enumerate(zip(feedback["issues"], EVIDENCE), 1):
        assert issue["id"] == f"task-28/issue-{index}"
        assert issue["evidence"].splitlines() == refs

    open_issue = {"task-28/issue-1": {"id": "task-28/issue-1", "status": "OPEN"}}
    resolved = v8._review_feedback("task-40", "ACCEPT", {
        "resolved_issues": [{"id": "task-28/issue-1", "status": "RESOLVED",
                              "reason": "Independently checked",
                              "evidence": EVIDENCE[0]}],
        "accepted_deviations": [{"item": "Duplicate local check",
                                  "reason": "Independent reconstruction already checked it",
                                  "evidence": EVIDENCE[1]}],
    }, {"review_issues": open_issue})
    assert resolved["resolutions"][0]["evidence"].splitlines() == EVIDENCE[0]
    assert resolved["accepted_deviations"][0]["evidence"].splitlines() == EVIDENCE[1]


@pytest.mark.parametrize("invalid, reason", [
    ([], "must list 1-8"),
    ([""], "whitespace-only"),
    ([{"path": "run.py"}], "non-string"),
    (["a"] * 9, "must list 1-8"),
    (["a" * 800, "b" * 800], "actual length 1601"),
])
def test_evidence_lists_still_reject_invalid_or_oversized_evidence(invalid, reason):
    with pytest.raises(ValueError, match=reason):
        v8._review_evidence(invalid, "issue evidence")


def test_200kb_codex_prompt_is_delivered_by_stdin_not_as_linux_argv(tmp_path, monkeypatch):
    sent = {}
    class Completed:
        returncode = 0
        def communicate(self, input=None, timeout=None):
            sent["prompt"] = input
            sent["timeout"] = timeout
            sent["log"].write(json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '{"action":"complete","summary":"ok"}'},
            }) + "\n" + json.dumps({"type": "turn.completed"}) + "\n")
            sent["log"].flush()

    def launch(cmd, **kw):
        sent["argv"] = cmd
        sent["log"] = kw["stdout"]
        assert cmd[-1] == "-"
        assert kw["stdin"] == subprocess.PIPE
        assert kw["text"] is True and kw["encoding"] == "utf-8"
        return Completed()

    monkeypatch.setattr(v8.codex, "resolve_bin", lambda: "/fake/codex")
    monkeypatch.setattr(v8.codex, "subprocess_env", lambda binary: {})
    monkeypatch.setattr(v8.subprocess, "Popen", launch)
    prompt = "Research context " + "X" * 200000
    result = v8._call("gpt-test", "low", prompt, tmp_path,
                      tmp_path / "logs" / "fake.jsonl", timeout=17)
    assert result["summary"] == "ok"
    assert sent["prompt"] == prompt
    assert len(" ".join(sent["argv"]).encode()) < 4096
    assert sent["timeout"] == 17


def test_long_project_routing_context_keeps_open_reviewer_issues_not_full_history():
    tasks = {f"task-{i}": {
        "id": f"task-{i}", "role": "researcher", "purpose": "work",
        "status": "COMPLETE", "proposal_id": "", "request_id": "",
        "instruction": "investigate", "summary": "X" * 3000,
    } for i in range(30)}
    proposals = {f"p{i}": {
        "id": f"p{i}", "status": "APPROVE", "by": "task-2",
        "content": "A" * 17000, "required_checks": [{"id": "files"}],
    } for i in range(8)}
    issues = {f"task-32/issue-{i}": {
        "id": f"task-32/issue-{i}", "status": "OPEN",
        "target_role": "experimenter",
        "problem": "missing check " * 80, "impact": "may reverse result",
        "required_action": "independently repeat observed comparison",
        "evidence": "report.json",
    } for i in range(5)}
    events = [{"seq": i, "kind": "RESULT", "summary": "E" * 18000,
               "review_feedback": {"issues": [{"evidence": "F" * 3000}]}}
              for i in range(50)]
    state = {
        "goal": "G" * 7240, "reference": "", "tasks": tasks,
        "requests": {}, "proposals": proposals,
        "assets": {}, "review_issues": issues,
        "final_review": {"verdict": "REVISE", "task_id": "task-32",
                         "basis_seq": 35, "summary": "R" * 9000},
        "latest_validation": {"status": "PASSED", "task_id": "task-11", "reason": "ok"},
        "inputs": {"dataset_label": "hemibrain:v1.1", "manifest_path": "input/PROVENANCE.json"},
    }
    class FakeStore:
        def state(self):
            return state
        def read(self):
            return events

    brief = v8._brief_state(FakeStore())
    assert len(brief["tasks"]) == 10
    assert brief["proposal_count"] == 8
    assert brief["open_review_issue_count"] == 5
    assert {i["id"] for i in brief["review_issues"]} == set(issues)
    assert brief["latest_validation"]["status"] == "PASSED"
    assert brief["final_review"]["verdict"] == "REVISE"
    assert len(json.dumps(brief).encode("utf-8")) < 32000


def test_os_supervisor_launch_failure_is_not_retried_as_scientific_decision(tmp_path, monkeypatch):
    store = v8.Store(tmp_path / "runtime" / "v8" / "long-argv")
    v8.initialize(store, "Test execution transport rather than a scientific claim.")
    calls = []
    def fail_to_start(*args, **kwargs):
        calls.append(1)
        raise OSError(errno.E2BIG, "Argument list too long")
    monkeypatch.setattr(v8, "_supervisor", fail_to_start)
    assert v8.step(store) == "HALT"
    assert len(calls) == 1
    assert store.state()["halt"].startswith("SUPERVISOR_LAUNCH_ERROR:")
    assert not store.state()["done"]
