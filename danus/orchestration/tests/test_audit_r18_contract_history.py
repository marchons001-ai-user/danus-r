from danus.core import ProgressLedger, ResearchPlanGraph
from danus.orchestration.control import build_supervisor_snapshot, reduce_supervisor_snapshot


def test_86_completed_contract_execution_survives_experimenter_rebind(tmp_path):
    root = tmp_path / "P"
    graph = ResearchPlanGraph.create(
        root,
        node_id="P0",
        title="Research Project",
        summary="bounded root",
    )
    ledger = ProgressLedger(root)
    contract = ledger.submit_implementation_contract(
        proposer="researcher",
        node_id="P0",
        plan_version=1,
        goal="run the reviewed bounded experiment",
        must_preserve_invariants=["exact reviewed execution"],
        forbidden_behaviors=["duplicate execution"],
        acceptance_conditions=["execution completes exactly once"],
    )
    ledger.review_implementation_contract(
        contract["contract_id"],
        reviewer="reviewer",
        verdict="APPROVE",
        rationale="bounded and exact",
    )
    first = ledger.assign(
        worker="experimenter",
        role="experimenter",
        node_id="P0",
        plan_version=1,
        task=contract["task"],
    )
    ledger.update(
        worker="experimenter",
        state="DONE",
        actor="runtime",
        outcome="COMPLETE",
        expected_assignment_id=first["assignment_id"],
    )

    change_id = graph.propose_change(
        base_plan_version=1,
        proposer="researcher",
        reason="add a separate follow-up route",
        operations=[
            {
                "op": "add_node",
                "node_id": "P1",
                "title": "Follow-up",
                "summary": "independent later work",
                "parent": "P0",
                "depends_on": [],
            }
        ],
    )
    graph.review_change(
        change_id,
        reviewer="reviewer",
        verdict="APPROVE",
        rationale="independent follow-up keeps P0 compatible",
    )
    assert graph.commit_change(change_id, committer="supervisor") == 2
    assert graph.node_compatible("P0", 1)

    second = ledger.assign(
        worker="experimenter",
        role="experimenter",
        node_id="P1",
        plan_version=2,
        task="run separate follow-up work\n",
    )
    assert second["assignment_id"] != first["assignment_id"]
    assert ledger.current("experimenter")["assignment_id"] == second["assignment_id"]
    historical = next(
        item for item in ledger.assignments()
        if item["assignment_id"] == first["assignment_id"]
    )
    assert historical["state"] == "DONE"
    assert historical["outcome"] == "COMPLETE"
    assert ledger.implementation_contract(contract["contract_id"])["status"] == "CONSUMED"

    assert ledger.pending_implementation_contracts() == []
    report = reduce_supervisor_snapshot(build_supervisor_snapshot(root, []))
    assert contract["contract_id"] not in {
        item["contract_id"] for item in report["unresolved_implementation_contracts"]
    }
    assert not any(
        action.get("kind") == "ASSIGN_CONTRACT"
        and action.get("contract_id") == contract["contract_id"]
        for action in report["actions"]
    )
