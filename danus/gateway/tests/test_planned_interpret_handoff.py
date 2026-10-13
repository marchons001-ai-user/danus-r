"""A planned Researcher interpretation consumes one INTERPRET handoff, not two jobs."""
from __future__ import annotations

import json

import pytest

from danus.core import EvidenceStore, ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.gateway import capability, server
from danus.orchestration import autonomy, control


def _plan(root, *, extra_interpreter=False):
    graph = ResearchPlanGraph.create(root, node_id="P0", title="Synthetic acceptance")
    ops = [
        {"op": "add_node", "node_id": "E1", "title": "Experiment",
         "summary": "", "parent": "P0", "depends_on": []},
        {"op": "add_node", "node_id": "R2", "title": "Interpret",
         "summary": "", "parent": "P0", "depends_on": ["E1"]},
        {"op": "add_node", "node_id": "V2", "title": "Independent review",
         "summary": "", "parent": "P0", "depends_on": ["R2"]},
    ]
    if extra_interpreter:
        ops.append({"op": "add_node", "node_id": "R3", "title": "Second interpret",
                    "summary": "", "parent": "P0", "depends_on": ["E1"]})
    change = graph.propose_change(
        base_plan_version=1, proposer="low", reason="Single evidence interpretation",
        operations=ops,
    )
    graph.review_change(change, reviewer="low2", verdict="APPROVE", rationale="bounded")
    assert graph.commit_change(change, committer="supervisor") == 2
    task = "Interpret the immutable experiment using experiment_evidence_get; write one report.\n\n"
    for name in (["R2", "R3"] if extra_interpreter else ["R2"]):
        path = root / "tasks" / "v2" / "researcher" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(task, encoding="utf-8")
    path = root / "tasks" / "v2" / "reviewer" / "V2.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Independently review the interpretation report.\n", encoding="utf-8")
    return task


def _evidence(root, ledger, source):
    store = EvidenceStore(root)
    protocol = store.freeze_protocol(
        question="Does this synthetic experiment produce a bounded result?",
        hypothesis="A fixed single run reports a metric.",
        matrix={"units": [{"id": "u1", "argv": ["python", "pilot.py", "raw/pilot.json"],
                           "artifacts": ["raw/pilot.json"]}]},
        primary_metrics=["loss"], decision_rule="record loss",
        author="low3", implementation_contract_id="a" * 16,
        execution_assignment_id=source["assignment_id"],
    )
    raw = root / "experiment_runtime" / protocol / "artifacts" / "u1" / "raw" / "pilot.json"
    raw.parent.mkdir(parents=True)
    raw.write_text(json.dumps({"environment": {"device": "cpu"},
                               "metrics": {"loss": 0.7}}), encoding="utf-8")
    evidence = store.add_record(
        protocol_id=protocol, author="low3",
        matrix_coverage={"planned": 1, "completed": 1},
        primary_metrics={"loss": 0.7}, artifacts=[store.artifact_ref(raw)],
    )
    return protocol, evidence


def test_planned_interpret_is_one_authorized_r2_and_releases_v2(tmp_path, monkeypatch):
    root = tmp_path / "P"
    root.mkdir()
    for name in ("low", "low2", "low3"):
        (root / "workers" / name).mkdir(parents=True)
    task = _plan(root)
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(tmp_path))
    ledger = ProgressLedger(root)
    source = ledger.assign(
        worker="low3", role="experimenter", node_id="E1",
        plan_version=2, task="Execute the approved experiment.",
        write_task=True,
    )
    protocol, evidence = _evidence(root, ledger, source)
    monkeypatch.setattr(server, "_BOUND_WORKER",
                        capability.issue(root, "low3", "experimenter"))
    opened = server.handoff_request(
        "INTERPRET", reason="interpret the fixed result", evidence_refs=[evidence],
    )
    handoffs = HandoffRequests(ledger)
    handoff = handoffs.get(opened["handoff_id"])
    assert handoff["node_id"] == "R2"
    assert handoff["request"] == task
    assert handoff["evidence_refs"] == [evidence]

    ledger.update(worker="low3", state="DONE", actor="runtime", outcome="COMPLETE")
    ledger.set_node_state("E1", state="DONE", actor="supervisor",
                          expected_assignment_id=source["assignment_id"])
    roles = {"low": "researcher", "low2": "reviewer", "low3": "experimenter"}

    def assign(target, task, *, node_id, handoff_id=""):
        worker = target.rsplit("/", 1)[-1]
        return ledger.assign(worker=worker, role=roles[worker], node_id=node_id,
                             plan_version=2, task=task, handoff_id=handoff_id,
                             write_task=True)

    dispatched = autonomy._dispatch_candidates(
        "P", rows=[], assign=assign, start=lambda _wl: "started",
        role_of=lambda wl: roles[wl.name],
    )
    assert sum(item.get("result") == "assigned" and item.get("node_id") == "R2"
               for item in dispatched) == 1
    assert next(item for item in dispatched if item.get("result") == "assigned"
                and item.get("node_id") == "R2")["action"] == "INTERPRET"
    researcher = ledger.current("low")
    assert researcher["node_id"] == "R2"
    assert researcher["handoff_id"] == handoff["handoff_id"]
    assert len([a for a in ledger.assignments()
                if a["role"] == "researcher"]) == 1
    monkeypatch.setattr(server, "_BOUND_WORKER",
                        capability.issue(root, "low", "researcher"))
    assert server.experiment_evidence_get(evidence)["protocol_id"] == protocol
    assert server.experiment_evidence_get(
        evidence, artifact_index=0, json_pointer="/metrics/loss"
    )["selected_raw_json"]["value"] == 0.7
    assert server.experiment_protocol_get(protocol)["protocol"]["protocol_id"] == protocol
    with pytest.raises(PermissionError, match="not bound"):
        server.experiment_evidence_get("f" * 16)

    ledger.update(worker="low", state="DONE", actor="runtime", outcome="COMPLETE")
    done = autonomy._auto_complete(root, {
        "action": "REVIEW", "reason": "completion_not_recorded",
        "worker": "low", "node_id": "R2",
        "assignment_id": researcher["assignment_id"],
    })
    assert done["result"] == "completed"
    handoffs.resolve(handoff["handoff_id"], assignment_id=researcher["assignment_id"],
                     actor="supervisor", summary="planned interpretation completed")
    assert handoffs.get(handoff["handoff_id"])["status"] == "RESOLVED"
    reduced = control.reduce_supervisor_snapshot(
        control.build_supervisor_snapshot(root, [])
    )
    assert "V2" in reduced["frontier"]
    assert "R2" in reduced["completed_nodes"]


def test_ambiguous_interpret_target_falls_back_without_implicit_authority(tmp_path):
    root = tmp_path / "P"
    root.mkdir()
    _plan(root, extra_interpreter=True)
    with pytest.raises(ValueError, match="multiple planned"):
        autonomy._interpret_plan_target(root, "E1")
    assert autonomy._interpret_plan_target(root, "nonexistent") == ""
