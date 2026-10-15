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
                    " Compare heading MSE, including negative outcomes.",
        "required_checks": [{"id": "assets", "kind": "asset_integrity"}]})
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
    with pytest.raises(PermissionError, match="interpretation"):
        dispatch(s, "reviewer", "Premature final audit", "final_review")
    early = dispatch(s, "reviewer", "Early evidence audit", "work")
    start(s, early)
    v8._worker_result(s, early, {"action": "complete", "summary": "Raw evidence checked"})
    interp = dispatch(s, "researcher", "Explain observed results", "interpret")
    start(s, interp)
    v8._worker_result(s, interp, {"action": "complete",
                                  "summary": "Evidence supports no reliable advantage"})
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


def test_long_completed_codex_json_is_not_truncated_and_can_be_salvaged(tmp_path):
    s = new_store(tmp_path)
    task = dispatch(s, "researcher", "Make a bounded scientific proposal")
    start(s, task)
    s.append("RESULT", task_id=task, status="BLOCKED",
             summary="execution/validation error: JSONDecodeError: Expecting value",
             proposal=None, review=None, verdict="")
    output = {"action": "complete", "summary": "Detailed outcome: " + "x" * 10000,
              "proposal": "One seed; 2 minutes; compare noisy angles to baseline."}
    log = s.root / "logs" / (task + "-4.jsonl")
    log.parent.mkdir(exist_ok=True)
    log.write_text("\n".join(json.dumps(x) for x in [
        {"type": "thread.started", "thread_id": "offline"},
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps(output)}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 1, "output_tokens": 1}},
    ]) + "\n")
    assert v8._completed_model_reply(log) == output
    assert v8.recover_model_result(s, task)["status"] == "COMPLETE"
    assert len(s.state()["tasks"]) == 1
    assert len(s.state()["proposals"]) == 1
    assert any(e["kind"] == "RECOVERY" for e in s.read())


def test_worker_failure_does_not_dispatch_the_role_again_in_same_step(tmp_path, monkeypatch):
    s = new_store(tmp_path)
    calls = []
    def model(store, level, error=""):
        calls.append(level)
        return {"action": "dispatch", "role": "researcher", "purpose": "work",
                "task": "Analyze the existing data", "reason": "Necessary"}
    def broken_worker(store, task_id, *, timeout=1200):
        store.append("RUNNING", task_id=task_id)
        store.append("RESULT", task_id=task_id, status="BLOCKED",
                     summary="execution/validation error: JSONDecodeError",
                     proposal=None, review=None, verdict="")
        raise json.JSONDecodeError("bad model result", "", 0)
    monkeypatch.setattr(v8, "_supervisor", model)
    monkeypatch.setattr(v8, "_worker", broken_worker)
    assert v8.step(s) == "WORKER_BLOCKED"
    assert calls == ["low"]
    assert len(s.state()["tasks"]) == 1



def test_failed_v8_worker_is_historical_not_a_project_halt(tmp_path, monkeypatch):
    from danus.tui.v8_state import load_v8_project_snapshot
    monkeypatch.setenv("DANUS_RUNTIME", str(tmp_path))
    s = v8.Store(v8.project_root("historical-failure"))
    v8.initialize(s, "Explore an uncertain research hypothesis.")
    old = dispatch(s, "researcher", "Old attempt")
    start(s, old)
    s.append("RESULT", task_id=old, status="BLOCKED", summary="JSONDecodeError",
             proposal=None, review=None, verdict="")
    new = dispatch(s, "researcher", "Recovered scientific work")
    start(s, new)
    v8._worker_result(s, new, {"action": "complete", "summary": "Verified negative result"})
    snap = load_v8_project_snapshot("historical-failure")
    assert snap["project_state"] == "RUNNING"
    assert snap["failed_task_count"] == 1
    assert snap["blockers"] == []
    assert any(n["node_id"] == old and n["state"] == "BLOCKED"
               for n in snap["nodes"])



def test_corrective_19689_character_proposal_recovers_from_existing_log(tmp_path):
    s = new_store(tmp_path)
    t = dispatch(s, "researcher", "Draft a revised experiment protocol")
    start(s, t)
    s.append("RESULT", task_id=t, status="BLOCKED",
             summary="execution/validation error: ValueError: proposal must be nonempty text <= 16000 characters",
             proposal=None, review=None, verdict="")
    output = {
        "action": "complete", "summary": "Prepared a scientifically justified correction",
        "proposal": "Fix targets and bootstrap intervals. " + "x" * 20000,
        "artifacts": [], "verdict": None,
    }
    assert len(output["proposal"]) > 19689
    log = s.root / "logs" / (t + "-4.jsonl")
    log.parent.mkdir(exist_ok=True)
    log.write_text("\n".join(json.dumps(event) for event in (
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps(output)}},
        {"type": "turn.completed", "usage": {"input_tokens": 1}},
    )) + "\n")
    assert v8.recover_model_result(s, t)["status"] == "COMPLETE"
    proposals = list(s.state()["proposals"].values())
    assert len(proposals) == 1
    assert proposals[0]["content"] == output["proposal"]
    assert proposals[0]["status"] == "PENDING"


def test_final_review_must_cover_latest_science_not_just_any_old_review(tmp_path):
    s = new_store(tmp_path)
    first = dispatch(s, "researcher", "Initial study")
    start(s, first)
    v8._worker_result(s, first, {"action": "complete", "summary": "Initial analysis"})
    review = dispatch(s, "reviewer", "Independent review", "final_review")
    start(s, review)
    v8._worker_result(s, review, {"action": "complete", "summary": "Initial review",
                                 "verdict": "INCONCLUSIVE"})
    second = dispatch(s, "researcher", "New interpretation")
    start(s, second)
    v8._worker_result(s, second, {"action": "complete", "summary": "New scientific finding"})
    with pytest.raises(PermissionError, match="stale"):
        v8.apply_decision(s, {"action": "finish", "reason": "Use the old review"}, "low")
    latest = dispatch(s, "reviewer", "Independent review", "final_review")
    start(s, latest)
    v8._worker_result(s, latest, {"action": "complete", "summary": "Reviewed new finding",
                                 "verdict": "INCONCLUSIVE"})
    v8.apply_decision(s, {"action": "finish", "reason": "Scoped inconclusive conclusion"}, "low")
    assert s.state()["done"]


def test_prompt_writing_targets_leave_room_for_valid_long_protocols():
    # Suggested length is an LLM writing target, not a change to recovery limits.
    assert "target at most 10,000 characters" in v8._WORKER_POLICY
    assert "target at most 5,000 characters" in v8._WORKER_POLICY
    assert "target at most 1,500 characters" in v8._WORKER_POLICY
    assert "target at most 800 characters" in v8._WORKER_POLICY
    assert "at most 4,000 characters" in v8._SUPERVISOR_POLICY
    assert "CHARACTERS, not words" in v8._WORKER_POLICY
    assert len(v8._proposal("x" * 19689)["content"]) == 19689
    with pytest.raises(ValueError, match="32768"):
        v8._proposal("x" * 32769)



def test_researcher_may_reference_registered_artifacts_without_reregistering(tmp_path):
    s = new_store(tmp_path)
    exp = dispatch(s, "researcher", "Propose an experiment")
    start(s, exp)
    v8._worker_result(s, exp, {"action": "complete", "summary": "Design",
                               "proposal": "One approved experiment, fixed condition"})
    pid = next(iter(s.state()["proposals"]))
    rv = dispatch(s, "reviewer", "Review experiment", "review_experiment", proposal_id=pid)
    start(s, rv)
    v8._worker_result(s, rv, {"action": "complete", "summary": "Approved", "verdict": "APPROVE"})
    run = dispatch(s, "experimenter", "Produce measurements", "experiment", proposal_id=pid)
    original = s.root / "workspace" / "output.csv"
    original.write_text("x,error\n1,0.4\n")
    start(s, run)
    v8._worker_result(s, run, {"action": "complete", "summary": "Measured",
                               "artifacts": ["output.csv"]})
    before = s.state()["assets"]["output.csv"].copy()
    interp = dispatch(s, "researcher", "Interpret output", "interpret")
    start(s, interp)
    v8._worker_result(s, interp, {
        "action": "complete", "summary": "The registered artifact supports this interpretation",
        "artifacts": ["output.csv"]
    })
    assert s.state()["tasks"][interp]["status"] == "COMPLETE"
    assert s.state()["assets"]["output.csv"] == before
    assert len([e for e in s.read() if e["kind"] == "ASSET"]) == 1
    assert s.state()["last_interpret_seq"] > s.state()["last_experiment_seq"]


def test_non_experiment_roles_cannot_register_unrecorded_files(tmp_path):
    s = new_store(tmp_path)
    task = dispatch(s, "researcher", "Examine an existing dataset")
    (s.root / "workspace" / "unregistered.txt").write_text("unverified")
    start(s, task)
    with pytest.raises(PermissionError, match="unregistered artifact"):
        v8._worker_result(s, task, {
            "action": "complete", "summary": "Invalid request",
            "artifacts": ["unregistered.txt"]})
    assert "unregistered.txt" not in s.state()["assets"]


def test_safely_salvage_role_artifact_reference_error(tmp_path):
    s = new_store(tmp_path)
    reference = s.root / "workspace" / "measurements.csv"
    reference.write_text("estimate\n0.4\n")
    original = {"path": "measurements.csv",
                "sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
                "bytes": reference.stat().st_size, "task_id": "task-source",
                "description": "Previously measured"}
    s.append("ASSET", **original)
    task = dispatch(s, "researcher", "Interpret experiment")
    start(s, task)
    s.append("RESULT", task_id=task, status="BLOCKED",
             summary="execution/validation error: PermissionError: only Experimenter may register new files as artifacts",
             proposal=None, review=None, verdict="")
    final = {"action": "complete", "summary": "Read the recorded measurements",
             "artifacts": ["measurements.csv"]}
    log = s.root / "logs" / (task + "-5.jsonl")
    log.parent.mkdir(exist_ok=True)
    log.write_text("\n".join(json.dumps(x) for x in (
        {"type": "item.completed", "item": {"type": "agent_message",
                                            "text": json.dumps(final)}},
        {"type": "turn.completed"},
    )) + "\n")
    assert v8.recover_model_result(s, task)["status"] == "COMPLETE"
    assert s.state()["assets"]["measurements.csv"] == original
    assert len([e for e in s.read() if e["kind"] == "ASSET"]) == 1
    assert any(e["kind"] == "RECOVERY" for e in s.read())


def test_tui_highlights_current_blocker_but_not_older_failures(tmp_path, monkeypatch):
    from danus.tui.v8_state import load_v8_project_snapshot
    monkeypatch.setenv("DANUS_RUNTIME", str(tmp_path))
    s = v8.Store(v8.project_root("blocker"))
    v8.initialize(s, "Investigate measurement noise.")
    old = dispatch(s, "researcher", "Old task")
    start(s, old)
    s.append("RESULT", task_id=old, status="BLOCKED", summary="old error",
             proposal=None, review=None, verdict="")
    now = dispatch(s, "researcher", "Current task")
    start(s, now)
    s.append("RESULT", task_id=now, status="BLOCKED", summary="current error",
             proposal=None, review=None, verdict="")
    snap = load_v8_project_snapshot("blocker")
    assert snap["project_state"] == "RUNNING"
    assert snap["failed_task_count"] == 1
    assert len(snap["blockers"]) == 1
    assert snap["blockers"][0]["assignment_id"] == now
    successor = dispatch(s, "reviewer", "New independent work")
    start(s, successor)
    v8._worker_result(s, successor, {"action": "complete", "summary": "Done"})
    snap = load_v8_project_snapshot("blocker")
    assert snap["blockers"] == []
    assert snap["failed_task_count"] == 2


def test_v8_default_model_assignments():
    assert (v8.LOW_MODEL, v8.LOW_EFFORT) == ("gpt-5.6-luna", "low")
    assert (v8.HIGH_MODEL, v8.HIGH_EFFORT) == ("gpt-6.1-sol", "medium")
    assert v8.DEFAULT_MODELS == {
        "researcher": ("gpt-6.1-sol", "high"),
        "reviewer": ("gpt-6.1-sol", "medium"),
        "experimenter": ("gpt-6.1-sol", "medium"),
    }


def test_experimenter_can_implement_approved_missing_entrypoint(tmp_path):
    s = new_store(tmp_path)
    researcher = dispatch(s, "researcher", "Propose one check")
    start(s, researcher)
    v8._worker_result(s, researcher, {
        "action": "complete", "summary": "New script required",
        "proposal": "Implement and run a NEW validation/run.py in the workspace",
        "required_checks": [{"id": "integrity", "kind": "asset_integrity"}],
    })
    proposal_id = next(iter(s.state()["proposals"]))
    with pytest.raises(PermissionError, match="independent approved"):
        dispatch(s, "experimenter", "Not approved", "experiment",
                 proposal_id=proposal_id)
    reviewer = dispatch(s, "reviewer", "Review the exact new script",
                        "review_experiment", proposal_id=proposal_id)
    start(s, reviewer)
    v8._worker_result(s, reviewer, {
        "action": "complete", "summary": "Approved", "verdict": "APPROVE",
    })
    experimenter = dispatch(s, "experimenter", "Run", "experiment",
                            proposal_id=proposal_id)
    task = s.state()["tasks"][experimenter]
    assert task["status"] == "READY"
    assert "Create missing NEW implementation scripts" in task["instruction"]
    assert not (s.root / "workspace" / "validation" / "run.py").exists()
    assert "planned script being absent is not a blocker" in v8._WORKER_POLICY


def test_referenced_long_summary_is_restored_exactly_for_cross_role_answer(
        tmp_path, monkeypatch):
    """Regression: task-11 must not reconstruct a missing tail of task-3."""
    s = new_store(tmp_path)
    original = "Feasibility and provenance: " + "c" * 4900 + " END_OF_SOURCE"
    unrelated = "Unrelated historical result " + "z" * 4900
    source = dispatch(s, "researcher", "Inspect frozen CX inputs")
    start(s, source)
    v8._worker_result(s, source, {
        "action": "complete", "summary": original})
    other = dispatch(s, "researcher", "Different historical question")
    start(s, other)
    v8._worker_result(s, other, {
        "action": "complete", "summary": unrelated})
    reviewer = dispatch(s, "reviewer", f"Independently audit {source} findings")
    reviewer_context, omitted = v8._referenced_completed_work(
        s.state(), reviewer)
    assert omitted == []
    assert [(x["task_id"], x["summary"]) for x in reviewer_context] == [
        (source, original)]
    start(s, reviewer)
    v8._worker_result(s, reviewer, {
        "action": "request", "summary": "Need cited source's full result",
        "to": "researcher",
        "question": f"Please supply the remainder of {source}'s summary",
        "reason": "Excerpt ended early"})
    req = next(iter(s.state()["requests"]))
    answer = dispatch(s, "researcher", "Answer the targeted request",
                      "answer", request_id=req)
    exact, omitted = v8._referenced_completed_work(s.state(), answer)
    assert omitted == []
    assert exact == [{"task_id": source, "role": "researcher",
                      "purpose": "work", "summary": original}]

    recorded = {}
    def fake_model(model, effort, prompt, workspace, log, **kwargs):
        recorded["payload"] = json.loads(
            prompt.split("\nCurrent assignment:\n", 1)[1])
        return {"action": "complete", "summary": "Cited source recovered exactly"}
    monkeypatch.setattr(v8, "_call", fake_model)
    v8._worker(s, answer)
    payload = recorded["payload"]
    assert payload["referenced_completed_work"] == exact
    assert payload["recent_completed_work"][0]["task_id"] == source
    assert payload["recent_completed_work"][0]["excerpt_truncated"] is True
    assert len(payload["recent_completed_work"][0]["summary_excerpt"]) == 3000
    assert unrelated not in json.dumps(payload["referenced_completed_work"])
    assert s.state()["tasks"][source]["summary"] == original
    assert s.state()["requests"][req]["status"] == "ANSWERED"
    revived, _ = v8._referenced_completed_work(s.state(), reviewer)
    assert revived[0]["summary"] == original


def test_unreferenced_work_stays_bounded_and_missing_tasks_are_not_invented(tmp_path):
    s = new_store(tmp_path)
    source = dispatch(s, "researcher", "Long analysis")
    start(s, source)
    v8._worker_result(s, source, {
        "action": "complete", "summary": "details " + "x" * 5200})
    new = dispatch(s, "researcher", "Assess an unrelated question")
    assert v8._referenced_completed_work(s.state(), new) == ([], [])
    reviewer = dispatch(s, "reviewer", "Independent feasibility audit")
    latest, missing = v8._referenced_completed_work(s.state(), reviewer)
    assert missing == []
    assert len(latest) == 1
    assert latest[0]["task_id"] == source
    assert latest[0]["summary"] == s.state()["tasks"][source]["summary"]
    named = dispatch(s, "reviewer", "Check task-9999 (not in event ledger)")
    assert v8._referenced_completed_work(s.state(), named) == ([], [])


def test_supervisor_reads_complete_cited_summary_not_truncated_routing_preview(
        tmp_path, monkeypatch):
    """R2 regression: a Supervisor reply must see the cited original result."""
    s = new_store(tmp_path)
    exact = "Completed input and anatomy audit: " + "x" * 4800 + " FINAL_CONCLUSION"
    first = dispatch(s, "researcher", "Audit frozen connection data")
    start(s, first)
    v8._worker_result(s, first, {"action": "complete", "summary": exact})
    second = dispatch(s, "researcher", "Prepare smallest protocol")
    start(s, second)
    v8._worker_result(s, second, {
        "action": "request", "summary": "Need the recorded conclusion",
        "to": "supervisor",
        "question": f"Supply the remainder of {first}'s scientific audit",
        "reason": "Only the short routing excerpt was visible"})
    status = v8._brief_state(s)
    assert status["tasks"][0]["summary"] == exact[:400]
    assert len(status["tasks"][0]["summary"]) < len(exact)
    source = status["supervisor_request_sources"][0]["completed_source"]
    assert source == {"task_id": first, "summary": exact}

    seen = {}
    def fake_model(model, effort, prompt, workspace, log, **kwargs):
        seen["state"] = json.loads(prompt.split("\nTrusted project status:\n", 1)[1])
        return {"action": "answer", "request_id": next(iter(s.state()["requests"])),
                "message": "Use the complete recorded source", "reason": "Cited result exists"}
    monkeypatch.setattr(v8, "_call", fake_model)
    assert v8._supervisor(s, "low")["action"] == "answer"
    assert seen["state"]["supervisor_request_sources"][0]["completed_source"] == source


def test_supervisor_request_sources_are_narrow_and_do_not_invent_missing_records(tmp_path):
    s = new_store(tmp_path)
    first = dispatch(s, "researcher", "Short earlier analysis")
    start(s, first)
    v8._worker_result(s, first, {"action": "complete", "summary": "recorded outcome"})
    assert v8._brief_state(s)["supervisor_request_sources"] == []

    second = dispatch(s, "researcher", "Next analysis")
    start(s, second)
    v8._worker_result(s, second, {
        "action": "request", "summary": "Missing result",
        "to": "supervisor", "question": "What did task-9999 establish?",
        "reason": "Need to know"})
    assert v8._brief_state(s)["supervisor_request_sources"][0]["completed_source"] is None


def test_corrective_researcher_and_supervisor_can_read_frozen_revised_proposal(
        tmp_path, monkeypatch):
    s = new_store(tmp_path)
    original = "Fixed study protocol: " + "x" * 12000 + " PRECISE_END"
    researcher = dispatch(s, "researcher", "Draft a bounded exact protocol")
    start(s, researcher)
    v8._worker_result(s, researcher, {
        "action": "complete", "summary": "Protocol submitted",
        "proposal": original,
        "required_checks": [{"id": "integrity", "kind": "asset_integrity"}],
    })
    pid = next(iter(s.state()["proposals"]))
    reviewer = dispatch(s, "reviewer", "Review protocol",
                        "review_experiment", proposal_id=pid)
    start(s, reviewer)
    v8._worker_result(s, reviewer, {
        "action": "complete", "summary": "One endpoint definition is missing",
        "verdict": "REVISE",
    })
    assert s.state()["proposals"][pid]["status"] == "REVISE"
    correction = dispatch(s, "researcher", "Correct the revised protocol")
    seen = {}
    def model(model_name, effort, prompt, workspace, log, **kwargs):
        seen["payload"] = json.loads(
            prompt.split("\nCurrent assignment:\n", 1)[1])
        return {"action": "request", "summary": "Need exact source",
                "to": "supervisor",
                "question": f"Provide proposal {pid} verbatim",
                "reason": "Avoid changing approved numeric conditions"}
    monkeypatch.setattr(v8, "_call", model)
    v8._worker(s, correction)
    assert seen["payload"]["cited_experiment_proposals"][0]["content"] == original
    status = v8._brief_state(s)
    source = status["supervisor_request_sources"][0]["cited_proposals"]
    assert len(source) == 1
    assert source[0]["id"] == pid
    assert source[0]["content"] == original
    assert s.state()["proposals"][pid]["content"] == original

    # The exact same event-backed proposal is available for a targeted answer.
    answer = dispatch(s, "researcher", "Answer proposal source request",
                      "answer", request_id=next(iter(s.state()["requests"])))
    payload = {}
    def answer_model(model_name, effort, prompt, workspace, log, **kwargs):
        payload.update(json.loads(prompt.split("\nCurrent assignment:\n", 1)[1]))
        return {"action": "complete", "summary": "Exact proposal read"}
    monkeypatch.setattr(v8, "_call", answer_model)
    v8._worker(s, answer)
    assert payload["cited_experiment_proposals"][0]["content"] == original
