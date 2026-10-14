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
