"""Three bounded offline checks for V8. No model calls or experiment training."""
import hashlib
import json

import pytest

from danus import v8


def new_store(tmp_path):
    store = v8.Store(tmp_path / "P")
    v8.initialize(store, "Determine whether ring connectivity improves noisy heading memory. "
                        "A negative result is acceptable.")
    return store


def dispatch(store, role, task, purpose="work", **kwargs):
    return v8.apply_decision(store, {"action": "dispatch", "role": role,
        "purpose": purpose, "task": task, "reason": "needed for present investigation",
        **kwargs}, "low")


def start(store, task):
    store.append("RUNNING", task_id=task)


def test_sequential_multirole_request_and_followups(tmp_path):
    s = new_store(tmp_path)
    r = dispatch(s, "researcher", "Analyze experiment E1")
    start(s, r)
    v8._worker_result(s, r, {"action": "request", "summary": "Awaiting E1 provenance",
        "to": "experimenter", "question": "Where is the metric file?",
        "reason": "It is needed to interpret E1", "refs": ["E1"]})
    req = next(iter(s.state()["requests"]))
    assert s.state()["tasks"][r]["status"] == "WAITING"
    with pytest.raises(ValueError, match="READY"):
        v8.apply_decision(s, {"action": "dispatch", "task_id": r,
                             "reason": "resume too early"}, "low")
    e = dispatch(s, "experimenter", "Find metric file", "answer", request_id=req)
    start(s, e)
    v8._worker_result(s, e, {"action": "complete", "summary": "E1 file is metrics.json",
                              "artifacts": []})
    assert s.state()["requests"][req]["status"] == "ANSWERED"
    assert s.state()["tasks"][r]["status"] == "READY"
    assert v8.apply_decision(s, {"action": "dispatch", "task_id": r,
                                 "reason": "use E's response"}, "low") == r
    start(s, r)
    v8._worker_result(s, r, {"action": "request", "summary": "Need independent view",
                              "to": "reviewer", "question": "Is result interpretable?",
                              "reason": "Prior data answer raised a new doubt"})
    req2 = list(s.state()["requests"])[-1]
    reviewer = dispatch(s, "reviewer", "Answer R's doubt", "answer", request_id=req2)
    start(s, reviewer)
    v8._worker_result(s, reviewer, {"action": "complete", "summary": "Interpret cautiously"})
    assert s.state()["tasks"][r]["status"] == "READY"
    assert len(s.state()["requests"]) == 2


def test_review_required_and_artifacts_immutable(tmp_path):
    s = new_store(tmp_path)
    r = dispatch(s, "researcher", "Propose minimal experiment")
    start(s, r)
    v8._worker_result(s, r, {"action": "complete",
        "summary": "Test memory on controlled synthetic headings",
        "proposal": "Use 8-unit recurrent and feedforward models; 1 seed, 2 minutes max."
                    " Compare heading MSE, including negative outcomes."})
    pid = next(iter(s.state()["proposals"]))
    with pytest.raises(PermissionError, match="approved"):
        dispatch(s, "experimenter", "Run experiment", "experiment", proposal_id=pid)
    v = dispatch(s, "reviewer", "Review the frozen proposal", "review_experiment",
                 proposal_id=pid)
    start(s, v)
    v8._worker_result(s, v, {"action": "complete", "summary": "Method adequately bounded",
                              "verdict": "APPROVE"})
    e = dispatch(s, "experimenter", "Execute exact approved proposal", "experiment",
                 proposal_id=pid)
    file = s.root / "workspace" / "result.json"
    file.write_text('{"mse":0.4}')
    start(s, e)
    v8._worker_result(s, e, {"action": "complete", "summary": "Measured mse=0.4",
                              "artifacts": ["result.json"]})
    assert s.state()["assets"]["result.json"]["sha256"] == hashlib.sha256(
        file.read_bytes()).hexdigest()
    with pytest.raises(PermissionError, match="independent final review"):
        v8.apply_decision(s, {"action": "finish", "reason": "looks done"}, "low")
    final = dispatch(s, "reviewer", "Independent final audit", "final_review")
    start(s, final)
    v8._worker_result(s, final, {"action": "complete",
                                 "summary": "Single run cannot establish generality",
                                 "verdict": "INCONCLUSIVE"})
    assert v8.apply_decision(s, {"action": "finish",
                                 "reason": "No general advantage established"}, "low") is None
    assert s.state()["done"] is True
    file.write_text('{"mse":0.01}')
    with pytest.raises(PermissionError, match="artifact modified"):
        v8.verify_assets(s)


def test_escalation_and_denied_actions_do_not_grant_permissions(tmp_path, monkeypatch):
    s = new_store(tmp_path)
    calls = []
    def mock_supervisor(store, level, error=""):
        calls.append(level)
        return ({"action": "escalate", "reason": "ambiguous source of Evidence"}
                if level == "low" else
                {"action": "dispatch", "role": "researcher", "purpose": "work",
                 "task": "Investigate recorded evidence locations without rerunning",
                 "reason": "Legal research question"})
    def mock_worker(store, task_id, *, timeout=1200):
        v8._worker_result(store, task_id, {"action": "complete",
                                          "summary": "No evidence located"})
    monkeypatch.setattr(v8, "_supervisor", mock_supervisor)
    def simulate_worker(store, task_id, *, timeout=1200):
        store.append("RUNNING", task_id=task_id)
        mock_worker(store, task_id)
    monkeypatch.setattr(v8, "_worker", simulate_worker)
    assert v8.step(s) == "task-3"
    assert calls == ["low", "high"]
    assert s.state()["tasks"]["task-3"]["status"] == "COMPLETE"
    with pytest.raises(PermissionError, match="only high"):
        v8.apply_decision(s, {"action": "block", "reason": "illegal request"}, "low")
    with pytest.raises(PermissionError, match="approved"):
        dispatch(s, "experimenter", "Unapproved experiment", "experiment",
                 proposal_id="fake")


def test_existing_tui_can_display_live_v8_project(tmp_path, monkeypatch):
    from danus.tui.v8_state import load_v8_project_snapshot

    monkeypatch.setenv("DANUS_RUNTIME", str(tmp_path))
    s = v8.Store(v8.project_root("tui-smoke"))
    v8.initialize(s, "Explore noisy directional memory without assuming improvement.")
    task = dispatch(s, "researcher", "Design a bounded comparison")
    start(s, task)
    snapshot = load_v8_project_snapshot("tui-smoke")
    assert snapshot["plan_version"] == 8
    assert snapshot["project_state"] == "RUNNING"
    assert next(r for r in snapshot["workers"] if r["role"] == "researcher")[
        "progress_state"] == "RUNNING"
    assert any(n["node_id"] == task for n in snapshot["nodes"])
    assert any(a["source"] == "RUNNING" for a in snapshot["activity"])
