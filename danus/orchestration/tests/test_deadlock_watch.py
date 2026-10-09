"""Deterministic tests for journal dedupe, 180s idle detection and safe fallback."""
import json

from danus.execution import layout as L
from danus.orchestration import cli
from danus.orchestration import deadlock_watch as D


def _report(state="RUNNING"):
    return {
        "project_state": state, "plan_version": 2,
        "frontier": ["R1"] if state == "RUNNING" else [],
        "completed_nodes": [], "pending_handoffs": [{
            "handoff_id": "a" * 16, "kind": "ESCALATE", "status": "OPEN",
            "node_id": "R1", "target_role": "main",
        }] if state == "RUNNING" else [],
        "unresolved_implementation_contracts": [],
        "decisions": [{"action": "REVIEW", "node_id": "R1",
                       "assignment_id": "b" * 16, "reason": "assignment_needs_review"}],
        "budget": {"tokens": {"exceeded": False, "used": 200, "limit": 10000000},
                   "time": {"exceeded": False}},
    }


def _rows():
    return [
        {"worker": "low", "alive": False, "assignment_id": "b" * 16,
         "plan_node_id": "R1", "progress_state": "BLOCKED",
         "progress_outcome": "NEEDS_REVIEW",
         "pause_reason": "review:assignment_needs_review"},
        {"worker": "low2", "alive": False, "assignment_id": "c" * 16,
         "plan_node_id": "R1", "progress_state": "DONE",
         "progress_outcome": "COMPLETE", "pause_reason": None},
        {"worker": "low3", "alive": False, "assignment_id": None,
         "plan_node_id": None, "progress_state": None, "pause_reason": None},
    ]


def test_deadlock_opens_once_after_180_seconds_and_closes_on_progress(tmp_path, monkeypatch):
    root = tmp_path / "P"
    root.mkdir()
    report, rows = _report(), _rows()
    monkeypatch.setattr(L, "project_dir", lambda p: root)
    monkeypatch.setattr(cli, "_project_rows", lambda p: rows)
    monkeypatch.setattr(cli, "_do_supervise_unlocked", lambda p, **kw: report)
    calls = []
    monkeypatch.setattr(D, "_model_proposal", lambda packet: (
        calls.append(packet) or {
            "target_role": "reviewer", "node_id": "R1",
            "rationale": "Existing R1 needs a Reviewer diagnosis",
        }
    ))
    watch = D.DeadlockWatch("P")
    assert watch.observe(report, rows, now=0) is None
    assert watch.observe(report, rows, now=179) is None
    detected = watch.observe(report, rows, now=180)
    assert detected["event"] == "DEADLOCK_OPEN"
    assert detected["snapshot"]["pending_handoffs"][0]["kind"] == "ESCALATE"
    assert len(calls) == 1
    assert watch.observe(report, rows, now=250) is None
    records = D._history(root)
    assert [r["event"] for r in records] == [
        "DEADLOCK_OPEN", "DEADLOCK_SUGGESTION", "DEADLOCK_ACTION",
    ]
    assert records[1]["target_role"] == "reviewer"
    assert records[2]["result"] == "rejected"
    assert all(r.get("deadlock_id") == detected["deadlock_id"] for r in records)

    changed = _report("COMPLETE")
    assert watch.observe(changed, rows, now=251) is None
    records = D._history(root)
    assert records[-1]["event"] == "DEADLOCK_CLOSED"
    assert records[-1]["deadlock_id"] == detected["deadlock_id"]

    # Control-plane restarts cannot repeat or lose a manually captured incident.
    monkeypatch.setattr(cli, "_do_supervise_unlocked", lambda p, **kw: report)
    first = D.capture_current_deadlock("P", token_budget=10000000)
    second = D.capture_current_deadlock("P", token_budget=10000000)
    assert second["replayed"] is True
    assert second["deadlock_id"] == first["deadlock_id"]
    assert sum(r["event"] == "DEADLOCK_OPEN" for r in D._history(root)) == 2


def test_watchdog_recovery_calls_only_existing_validated_entrypoint(tmp_path, monkeypatch):
    root = tmp_path / "P"
    root.mkdir()
    report, rows = _report(), _rows()
    monkeypatch.setattr(L, "project_dir", lambda p: root)
    monkeypatch.setattr(cli, "_project_rows", lambda p: rows)
    monkeypatch.setattr(cli, "_do_supervise_unlocked", lambda p, **kw: report)
    monkeypatch.setattr(D, "_model_proposal", lambda packet: {
        "target_role": "experimenter", "node_id": "R1",
        "rationale": "Use approved scientific experiment evidence",
    })
    original_packet = D._packet
    monkeypatch.setattr(D, "_packet", lambda report, rows, *, root=None: {
        **original_packet(report, rows, root=root),
        "current_assignments": {
            "b" * 16: {"role": "researcher", "node_id": "R1",
                       "state": "BLOCKED", "outcome": "NEEDS_REVIEW"},
        },
    })
    from danus.orchestration import idle_recovery

    invoked = []
    monkeypatch.setattr(idle_recovery, "dispatch_role_choice",
                        lambda p, *, target_role, node_id, reason, token_budget: (
                            invoked.append((p, target_role, node_id, token_budget)) or {
                                "result": "assigned", "node_id": node_id,
                                "assignment_id": "e" * 16,
                            }
                        ))
    watch = D.DeadlockWatch("P")
    assert watch.observe(report, rows, now=0, token_budget=10000000) is None
    assert watch.observe(report, rows, now=181, token_budget=10000000)
    assert invoked == [("P", "experimenter", "R1", 10000000)]
    assert D._history(root)[-1]["event"] == "DEADLOCK_ACTION"
    assert D._history(root)[-1]["result"] == "assigned"
    assert D._history(root)[-1]["target_role"] == "experimenter"


def test_watchdog_excludes_manual_pauses_but_not_missing_token_telemetry(tmp_path, monkeypatch):
    root = tmp_path / "P"
    root.mkdir()
    report, rows = _report(), _rows()
    report["budget"]["tokens"]["exceeded"] = None
    monkeypatch.setattr(L, "project_dir", lambda p: root)
    assert D._eligible(root, report, rows) is True
    rows[0]["pause_reason"] = "manual"
    assert D._eligible(root, report, rows) is False
    rows[0]["pause_reason"] = None
    report["budget"]["tokens"]["exceeded"] = True
    assert D._eligible(root, report, rows) is False



def test_legacy_wait_for_operator_is_retriaged_once_by_new_role_policy(tmp_path, monkeypatch):
    """A prior suggestion is not a decision made under the new role router."""
    root = tmp_path / "P"
    root.mkdir()
    report, rows = _report(), _rows()
    monkeypatch.setattr(L, "project_dir", lambda p: root)
    monkeypatch.setattr(cli, "_project_rows", lambda p: rows)
    monkeypatch.setattr(cli, "_do_supervise_unlocked", lambda p, **kw: report)
    signature = D._fingerprint(report, rows)
    D._write(root, {
        "event": "DEADLOCK_OPEN", "deadlock_id": "f" * 16,
        "signature": signature, "idle_seconds": 180, "capture": "automatic",
        "snapshot": D._packet(report, rows),
    })
    D._write(root, {
        "event": "DEADLOCK_SUGGESTION", "deadlock_id": "f" * 16,
        "action": "WAIT_FOR_OPERATOR", "target_role": "", "node_id": "",
        "rationale": "legacy whitelist does not authorize this deadlock",
    })
    selected = []
    monkeypatch.setattr(D, "_model_proposal", lambda packet: (
        selected.append(packet) or {
            "target_role": "reviewer", "node_id": "R1",
            "rationale": "review the blocked source independently",
        }
    ))
    from danus.orchestration import idle_recovery

    monkeypatch.setattr(idle_recovery, "dispatch_role_choice",
                        lambda p, **kw: {
                            "result": "assigned", "node_id": kw["node_id"],
                            "assignment_id": "e" * 16,
                        })
    watch = D.DeadlockWatch("P")
    assert watch.observe(report, rows, now=0) is None
    assert watch.observe(report, rows, now=181)
    events = D._history(root)
    assert sum(e["event"] == "DEADLOCK_OPEN" for e in events) == 1
    suggestions = [e for e in events if e["event"] == "DEADLOCK_SUGGESTION"]
    assert len(suggestions) == 2
    assert suggestions[-1]["policy_version"] == D._DECISION_POLICY
    assert suggestions[-1]["target_role"] == "reviewer"
    assert events[-1]["event"] == "DEADLOCK_ACTION"
    assert events[-1]["result"] == "assigned"
    assert events[-1]["deadlock_id"] == "f" * 16
    assert watch.observe(report, rows, now=250) is None
    assert len(selected) == 1
    assert len(D._history(root)) == len(events)

