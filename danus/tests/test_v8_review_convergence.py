"""Real PFL3 repeated Reviewer corrections: preserve scientific authority without issue inflation."""
import pytest

from danus import v8


def prior_issues():
    return {"review_issues": {
        "task-32/issue-2": {
            "id": "task-32/issue-2", "status": "OPEN",
            "problem": "Recovery flags missing",
            "required_action": "Store pulse-wise recovery flags",
            "target_role": "experimenter", "impact": "Recovery cannot be verified",
            "evidence": "convergence.csv",
        },
        "task-68/issue-1": {
            "id": "task-68/issue-1", "status": "OPEN",
            "problem": "task-32/issue-2 remains OPEN: recovery flags missing",
            "required_action": "Store pulse-wise recovery flags",
            "target_role": "experimenter", "impact": "Same missing evidence",
            "evidence": "convergence.csv",
        },
    }}


def issue(problem, action):
    return {"problem": problem, "impact": "Could change the narrow conclusion",
            "required_action": action, "target_role": "experimenter",
            "evidence": ["report.json", "run.py"]}


def test_revise_can_continue_existing_open_findings_without_making_new_issues():
    state = prior_issues()
    fb = v8._review_feedback("task-143", "REVISE",
        {"issues": [], "summary": "Both original correction requirements remain open"}, state)
    assert fb["issues"] == [] and fb["resolutions"] == []
    with pytest.raises(ValueError, match="existing unresolved"):
        v8._review_feedback("task-143", "REVISE", {}, {"review_issues": {}})
    with pytest.raises(ValueError, match="unresolved Reviewer issues"):
        v8._review_feedback("task-143", "INCONCLUSIVE", {}, state)


def test_history_duplicate_is_linked_to_open_canonical_and_replayed(tmp_path):
    store = v8.Store(tmp_path / "study")
    v8.initialize(store, "A negative experiment result is scientifically valid")
    origin = v8._review_feedback("task-32", "REVISE", {
        "issues": [issue("Recovery flags missing", "Store pulse-wise recovery flags")]
    }, store.state())
    old_id = "task-32/issue-1"
    store.append("TASK", id="task-32", role="reviewer", purpose="final_review",
                 instruction="Audit original", proposal_id="", request_id="", review_basis_seq=0)
    store.append("RESULT", task_id="task-32", status="COMPLETE",
                 summary="Missing flags", verdict="REVISE", review_feedback=origin)

    second = v8._review_feedback("task-68", "REVISE", {
        "issues": [issue("Second wording of the same absent flags",
                         "Store pulse-wise recovery flags")]
    }, store.state())
    new_id = "task-68/issue-1"
    store.append("TASK", id="task-68", role="reviewer", purpose="final_review",
                 instruction="Audit follow-up", proposal_id="", request_id="", review_basis_seq=0)
    store.append("RESULT", task_id="task-68", status="COMPLETE",
                 summary="Same failure", verdict="REVISE", review_feedback=second)

    resolved = v8._review_feedback("task-143", "REVISE", {
        "resolved_issues": [{
            "id": new_id, "status": "DUPLICATE", "canonical_id": old_id,
            "reason": "Both require the same missing pulse-wise flags",
            "evidence": ["convergence.csv", "run.py"],
        }],
    }, store.state())
    assert resolved["issues"] == []
    store.append("TASK", id="task-143", role="reviewer", purpose="final_review",
                 instruction="Reconcile corrections", proposal_id="", request_id="", review_basis_seq=0)
    store.append("RESULT", task_id="task-143", status="COMPLETE",
                 summary="Repeated finding deduplicated, original still required",
                 verdict="REVISE", review_feedback=resolved)
    state = store.state()
    assert state["review_issues"][old_id]["status"] == "OPEN"
    assert state["review_issues"][new_id]["status"] == "DUPLICATE"
    assert state["review_issues"][new_id]["canonical_id"] == old_id
    assert state["review_issues"][new_id]["evidence"] == "convergence.csv\nrun.py"
    with pytest.raises(ValueError, match="unresolved Reviewer issues"):
        v8._review_feedback("task-200", "ACCEPT", {}, state)

    accept = v8._review_feedback("task-200", "INCONCLUSIVE", {
        "resolved_issues": [{"id": old_id, "status": "RESOLVED",
                             "reason": "Verified measured pulse flags",
                             "evidence": "read-only checker output"}]
    }, state)
    assert accept["resolutions"][0]["status"] == "RESOLVED"


@pytest.mark.parametrize("canonical", [None, "task-68/issue-1", "unknown"])
def test_duplicate_needs_distinct_existing_open_canonical(canonical):
    with pytest.raises(ValueError, match="different existing OPEN"):
        v8._review_feedback("task-143", "REVISE", {
            "resolved_issues": [{
                "id": "task-68/issue-1", "status": "DUPLICATE",
                "canonical_id": canonical, "reason": "Same recovery requirement",
                "evidence": "Reviewed both old issue texts",
            }]
        }, prior_issues())


def test_duplicate_target_cannot_be_resolved_in_same_review():
    state = prior_issues()
    with pytest.raises(ValueError, match="must remain OPEN"):
        v8._review_feedback("task-143", "REVISE", {
            "resolved_issues": [
                {"id": "task-68/issue-1", "status": "DUPLICATE",
                 "canonical_id": "task-32/issue-2", "reason": "Same defect",
                 "evidence": "Both target the same absent flags"},
                {"id": "task-32/issue-2", "status": "RESOLVED",
                 "reason": "Flags now independently verified",
                 "evidence": "checker.log"},
            ]
        }, state)


def test_obvious_repeat_is_rejected_but_new_distinct_finding_is_allowed():
    state = prior_issues()
    for repeated in (
        issue("task-32/issue-2 remains OPEN: recovery flags missing", "Check output"),
        issue("Recovery flags missing", "Store pulse-wise recovery flags"),
    ):
        with pytest.raises(ValueError, match="restated as new"):
            v8._review_feedback("task-143", "REVISE", {"issues": [repeated]}, state)

    genuinely_new = issue("Actuator saturation sign reversed", "Check actual sign in run.py")
    fb = v8._review_feedback("task-143", "REVISE", {"issues": [genuinely_new]}, state)
    assert len(fb["issues"]) == 1 and fb["issues"][0]["id"] == "task-143/issue-1"
    with pytest.raises(ValueError, match="Identical issue restated"):
        v8._review_feedback("task-143", "REVISE",
                           {"issues": [genuinely_new, genuinely_new]}, state)


def _group_store(tmp_path):
    store = v8.Store(tmp_path / "group")
    v8.initialize(store, "Test a narrow scientific claim")
    research = v8.apply_decision(store, {
        "action": "dispatch", "role": "researcher", "purpose": "work",
        "task": "Analyze the available evidence", "reason": "Initial analysis",
    }, "low")
    store.append("RUNNING", task_id=research)
    v8._worker_result(store, research, {
        "action": "complete", "summary": "Two open evidence questions",
    })
    review = v8.apply_decision(store, {
        "action": "dispatch", "role": "reviewer", "purpose": "final_review",
        "task": "Independently assess evidence", "reason": "Scientific review",
    }, "low")
    store.append("RUNNING", task_id=review)
    v8._worker_result(store, review, {
        "action": "complete", "summary": "Two separate findings", "verdict": "REVISE",
        "issues": [
            issue("Recovery flags absent", "Recompute verified recovery records"),
            issue("Recovery timestamps absent", "Recompute verified recovery records"),
        ],
    })
    return store, list(store.state()["review_issues"])


def _research_complete(store, task_text, **result):
    task = v8.apply_decision(store, {
        "action": "dispatch", "role": "researcher", "purpose": "work",
        "task": task_text, "reason": "Address Reviewer findings",
    }, "low")
    store.append("RUNNING", task_id=task)
    v8._worker_result(store, task, {
        "action": "complete", "summary": "Evidence-based correction",
        **result,
    })
    return task


def test_researcher_group_requires_supervisor_approval_then_reviewer_approval(tmp_path):
    store, issue_ids = _group_store(tmp_path)
    task = _research_complete(store, "Propose shared repair", issue_merge={
        "issue_ids": issue_ids,
        "summary": "Recompute one pulse-level recovery table",
        "reason": "One independently reconstructed table supplies both missing fields",
    })
    merge_id = f"{task}/merge"
    assert store.state()["issue_merges"][merge_id]["status"] == "PENDING"
    assert v8._brief_state(store)["issue_merge_proposals"][0]["id"] == merge_id
    assert all(x["status"] == "OPEN" for x in store.state()["review_issues"].values())

    assert v8.apply_decision(store, {
        "action": "review_issue_merge", "merge_id": merge_id,
        "verdict": "APPROVE", "reason": "One table covers both independent requirements",
    }, "low") is None
    assert store.state()["issue_merges"][merge_id]["status"] == "APPROVE"
    assert all(x["status"] == "OPEN" for x in store.state()["review_issues"].values())

    _research_complete(store, "Write approved grouped experiment", issue_merge_id=merge_id,
                       proposal="Recompute pulse flags and timestamps with independent check",
                       required_checks=[{"id": "integrity", "kind": "asset_integrity"}])
    proposal_id = next(reversed(store.state()["proposals"]))
    assert store.state()["proposals"][proposal_id]["issue_merge_id"] == merge_id
    with pytest.raises(PermissionError, match="independent approved exact proposal"):
        v8.apply_decision(store, {
            "action": "dispatch", "role": "experimenter", "purpose": "experiment",
            "proposal_id": proposal_id, "reason": "Not independently approved",
            "task": "Run grouped repair",
        }, "low")
    approval = v8.apply_decision(store, {
        "action": "dispatch", "role": "reviewer", "purpose": "review_experiment",
        "proposal_id": proposal_id, "reason": "Independent approval",
        "task": "Review grouped corrective proposal",
    }, "low")
    store.append("RUNNING", task_id=approval)
    v8._worker_result(store, approval, {
        "action": "complete", "summary": "Adequate limited repair", "verdict": "APPROVE",
    })
    dispatched = v8.apply_decision(store, {
        "action": "dispatch", "role": "experimenter", "purpose": "experiment",
        "proposal_id": proposal_id, "reason": "Approved grouped repair",
        "task": "Run exact approved work",
    }, "low")
    assert store.state()["tasks"][dispatched]["purpose"] == "experiment"
    with pytest.raises(PermissionError, match="independent final review required|REVIEW_DEBT"):
        v8.apply_decision(store, {
            "action": "finish", "reason": "Grouping is not resolution",
        }, "low")


def test_grouping_rejected_or_unapproved_cannot_enter_corrective_proposal(tmp_path):
    store, issue_ids = _group_store(tmp_path)
    with pytest.raises(ValueError, match="distinct existing OPEN"):
        v8._issue_merge("task-x", {"issue_ids": [issue_ids[0], issue_ids[0]],
                                   "summary": "Same", "reason": "Same"}, store.state())
    task = _research_complete(store, "Request grouping", issue_merge={
        "issue_ids": issue_ids, "summary": "Combine", "reason": "One repair",
    })
    merge_id = f"{task}/merge"
    with pytest.raises(ValueError, match="same active"):
        v8._issue_merge("task-z", {
            "issue_ids": list(reversed(issue_ids)), "summary": "Repeat",
            "reason": "Same request",
        }, store.state())
    with pytest.raises(ValueError, match="APPROVE or REJECT"):
        v8.apply_decision(store, {
            "action": "review_issue_merge", "merge_id": merge_id,
            "verdict": "WAIVED", "reason": "Cannot waive review",
        }, "low")
    v8.apply_decision(store, {
        "action": "review_issue_merge", "merge_id": merge_id,
        "verdict": "REJECT", "reason": "Different evidence requirements",
    }, "low")
    assert store.state()["issue_merges"][merge_id]["status"] == "REJECT"
    with pytest.raises(PermissionError, match="unapproved or stale merge"):
        _research_complete(store, "Attempt rejected grouped plan",
                           issue_merge_id=merge_id, proposal="Unauthorized grouped work",
                           required_checks=[{"id": "integrity", "kind": "asset_integrity"}])
    assert not store.state()["proposals"]
