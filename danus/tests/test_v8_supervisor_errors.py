"""V8 Supervisor failure classification regressions; no model/API calls."""
import errno

import pytest

from danus import v8


def _decision(role, purpose, task, *, proposal_id=""):
    return {"action": "dispatch", "role": role, "purpose": purpose,
            "task": task, "proposal_id": proposal_id, "reason": "Required by the recorded evidence"}


def _complete(store, task_id, **result):
    store.append("RUNNING", task_id=task_id)
    v8._worker_result(store, task_id, {"action": "complete",
                                       "summary": "Recorded real evidence", **result})


def _after_experiment(tmp_path):
    """Make the state seen just after the real task-49 corrective experiment."""
    store = v8.Store(tmp_path / "P")
    v8.initialize(store, "Study a small evidence-constrained biological controller")

    researcher = v8.apply_decision(
        store, _decision("researcher", "work", "Specify an exact bounded study"), "low")
    _complete(store, researcher, proposal="Bounded frozen comparison",
              required_checks=[{"id": "assets", "kind": "asset_integrity"}])
    proposal = next(iter(store.state()["proposals"]))

    reviewer = v8.apply_decision(
        store, _decision("reviewer", "review_experiment", "Review exact protocol",
                         proposal_id=proposal), "low")
    _complete(store, reviewer, verdict="APPROVE")

    experiment = v8.apply_decision(
        store, _decision("experimenter", "experiment", "Run approved study",
                         proposal_id=proposal), "low")
    (store.root / "workspace" / "result.csv").write_text("condition,error\nA,0.42\n")
    _complete(store, experiment, artifacts=["result.csv"])
    assert store.state()["latest_validation"]["status"] == "PASSED"
    assert store.state()["last_experiment_seq"] > store.state()["last_interpret_seq"]
    return store


def test_premature_final_review_is_denied_then_interpretation_runs(tmp_path, monkeypatch):
    store = _after_experiment(tmp_path)
    assert v8._brief_state(store)["latest_experiment_needs_interpretation"] is True

    calls = []
    def supervisor(store, level, error=""):
        calls.append((level, error))
        if len(calls) == 1:
            return _decision("reviewer", "final_review", "Premature scientific verdict")
        assert "PermissionError: latest experiment needs completed Researcher interpretation" in error
        return _decision("researcher", "interpret", "Interpret latest experiment")

    def worker(store, task_id, *, timeout=1200):
        assert store.state()["tasks"][task_id]["purpose"] == "interpret"
        _complete(store, task_id)

    monkeypatch.setattr(v8, "_supervisor", supervisor)
    monkeypatch.setattr(v8, "_worker", worker)
    result = v8.step(store)
    assert result.startswith("task-")
    assert [x[0] for x in calls] == ["low", "low"]
    events = store.read()
    denied = [e for e in events if e["kind"] == "DENIED"]
    assert len(denied) == 1 and "PermissionError" in denied[0]["error"]
    assert not [e for e in events if e["kind"] == "HALT"]
    assert not [t for t in store.state()["tasks"].values()
                if t["purpose"] == "final_review"]
    assert store.state()["last_interpret_seq"] > store.state()["last_experiment_seq"]
    assert v8._brief_state(store)["latest_experiment_needs_interpretation"] is False


@pytest.mark.parametrize("error,prefix", [
    (PermissionError(errno.EACCES, "Codex executable not accessible"), "SUPERVISOR_LAUNCH_ERROR:"),
    (RuntimeError("model invocation timed out at 600s"), "SUPERVISOR_MODEL_ERROR:"),
])
def test_model_invocation_errors_do_not_masquerade_as_route_denials(
        tmp_path, monkeypatch, error, prefix):
    store = v8.Store(tmp_path / "P")
    v8.initialize(store, "Keep the original research task")
    calls = []
    def supervisor(*args, **kwargs):
        calls.append(True)
        raise error
    monkeypatch.setattr(v8, "_supervisor", supervisor)
    assert v8.step(store) == "HALT"
    assert len(calls) == 1
    assert store.state()["halt"].startswith(prefix)
    assert not any(event["kind"] == "DENIED" for event in store.read())


def test_decision_filesystem_oserror_is_not_called_codex_launch_error(tmp_path, monkeypatch):
    store = v8.Store(tmp_path / "P")
    v8.initialize(store, "Preserve fail-closed event recording")
    monkeypatch.setattr(v8, "_supervisor", lambda *a, **kw: _decision(
        "researcher", "work", "Read the experiment"))
    def fail_to_apply(*args, **kwargs):
        raise OSError(errno.EIO, "unable to read runtime event file")
    monkeypatch.setattr(v8, "apply_decision", fail_to_apply)
    assert v8.step(store) == "HALT"
    assert store.state()["halt"].startswith("SUPERVISOR_DECISION_IO_ERROR:")
    assert "SUPERVISOR_LAUNCH_ERROR" not in store.state()["halt"]
