"""S5-D adversarial recovery E2E: destroy process state, then recover from disk."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from danus.core import EvidenceStore, ProgressLedger, ResearchPlanGraph
from danus.core._util import read_jsonl
from danus.core.handoff import HandoffRequests
from danus.execution import layout as L
from danus.execution.tests import test_experiment_runtime as E
from danus.gateway import server
from danus.orchestration import cli, planning, steering
from danus.orchestration.tests import test_s5_governance_recovery as G
from danus.orchestration.tests import test_s5_resource_recovery as R


def _fresh(code: str, *, agents_root: Path | None = None, cwd: Path | None = None, check: bool = True):
    env = os.environ.copy()
    if agents_root is not None:
        env["DANUS_AGENTS_ROOT"] = str(agents_root)
    repo = Path(__file__).resolve().parents[3]
    env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd or repo,
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )


def _assert_control_invariants(pdir: Path) -> None:
    ledger = ProgressLedger(pdir)
    assignments = ledger.assignments()
    handoffs = HandoffRequests(ledger).all()

    logical = {}
    for handoff in handoffs:
        key = tuple(
            json.dumps(handoff.get(field), sort_keys=True)
            for field in (
                "kind", "plan_version", "node_id", "subject_type", "subject_id",
                "source_assignment_id", "parent_handoff_id", "requested_by",
                "source_role", "target_role", "request_sha256", "reason", "evidence_refs",
            )
        )
        logical.setdefault(key, set()).add(handoff["handoff_id"])
    assert all(len(ids) == 1 for ids in logical.values())

    for handoff in handoffs:
        active = [
            item for item in assignments
            if item.get("handoff_id") == handoff["handoff_id"]
            and item.get("state") not in {"DONE", "STALE"}
        ]
        assert len(active) <= 1
        if handoff.get("status") != "RESOLVED" or handoff.get("kind") != "REVIEW":
            continue
        if handoff.get("subject_type") == "IMPLEMENTATION_CONTRACT":
            contract = ledger.implementation_contract(str(handoff.get("subject_id") or ""))
            assert contract and contract.get("verdict") in ProgressLedger.IMPLEMENTATION_VERDICTS
        elif handoff.get("subject_type") == "PLAN_CHANGE":
            change = ResearchPlanGraph(pdir).get_change(str(handoff.get("subject_id") or ""))
            assert change.get("review_verdict") in ResearchPlanGraph.REVIEW_VERDICTS


def test_governance_missing_effect_recovers_across_cold_restarts(tmp_path: Path, monkeypatch):
    with G._root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        planning.initialize_research("P", brief="Study one bounded question.")
        pdir = L.project_dir("P")
        ledger = ProgressLedger(pdir)
        handoffs = HandoffRequests(ledger)
        G._finish("P", "low")
        contract = G._contract(ledger)
        G._apply("P")
        review = G._contract_review(handoffs, contract["contract_id"])
        handoff_id = review["handoff_id"]
        first_assignment = review["assigned_assignment_id"]

        G._finish("P", "low2")
        child = _fresh(
            "from danus.orchestration import cli; "
            "cli._start_one=lambda wl:'started'; "
            "r=cli.do_supervise('P', apply=True); "
            "print(r['convergence']['passes'])",
            agents_root=pdir.parent,
        )
        assert 1 <= int(child.stdout.strip()) <= 8

        retried = HandoffRequests(ProgressLedger(pdir)).get(handoff_id)
        assert retried and retried["handoff_id"] == handoff_id
        assert retried["status"] == "ASSIGNED" and retried["attempt"] == 2
        assert retried["assigned_assignment_id"] != first_assignment
        assert len([a for a in ProgressLedger(pdir).assignments() if a.get("handoff_id") == handoff_id]) == 2
        _assert_control_invariants(pdir)

        G._gateway_reviewer(monkeypatch, pdir)
        server.implementation_review_contract(contract["contract_id"], "APPROVE", "recovered exactly once")
        G._finish("P", "low2")
        _fresh(
            "from danus.orchestration import cli; "
            "cli._start_one=lambda wl:'started'; "
            "cli.do_supervise('P', apply=True)",
            agents_root=pdir.parent,
        )

        final = HandoffRequests(ProgressLedger(pdir)).get(handoff_id)
        assert final and final["status"] == "RESOLVED" and final["attempt"] == 2
        verdicts = [
            event for event in read_jsonl(ProgressLedger(pdir).path)
            if event.get("event") == "IMPLEMENTATION_REVIEW"
            and event.get("contract_id") == contract["contract_id"]
        ]
        assert len(verdicts) == 1
        _assert_control_invariants(pdir)


def test_plan_commit_crash_recovers_once_and_materializes_tasks(tmp_path: Path, monkeypatch):
    with G._root(tmp_path):
        pdir, captured, review = G._start_plan_review(monkeypatch)
        G._gateway_reviewer(monkeypatch, pdir)
        server.plan_review_change(captured["change_id"], rationale="bounded", verdict="APPROVE")
        G._finish("P", "low2")
        change_id = captured["change_id"]

        crash = _fresh(
            f"""
import os
from danus.core import ResearchPlanGraph
from danus.orchestration import cli, planning
cli._start_one=lambda wl:'started'
real=ResearchPlanGraph._write_json
def fail(self, path, payload):
    if path == self._change_path({change_id!r}) and payload.get('status') == 'COMMITTED':
        os._exit(91)
    return real(self, path, payload)
ResearchPlanGraph._write_json=fail
planning.planning_step('P')
""",
            agents_root=pdir.parent,
            check=False,
        )
        assert crash.returncode == 91
        graph = ResearchPlanGraph(pdir)
        assert graph.snapshot()["plan_version"] == 2
        assert graph.snapshot()["committed_change_id"] == change_id
        assert graph.get_change(change_id)["status"] == "APPROVED"
        assert not list((pdir / "tasks" / "v2").glob("**/*.md"))

        recovered = _fresh(
            "from danus.orchestration import cli, planning; "
            "cli._start_one=lambda wl:'started'; "
            "print(__import__('json').dumps(planning.planning_step('P')))",
            agents_root=pdir.parent,
        )
        actions = json.loads(recovered.stdout)
        graph = ResearchPlanGraph(pdir)
        assert graph.snapshot()["plan_version"] == 2
        assert graph.get_change(change_id)["status"] == "COMMITTED"
        tasks = list((pdir / "tasks" / "v2").glob("**/*.md"))
        assert len(tasks) == 1
        assert any(item.get("action") == "PLAN_TASK_RECOVERY" for item in actions)

        again = _fresh(
            "from danus.orchestration import cli, planning; "
            "cli._start_one=lambda wl:'started'; "
            "print(__import__('json').dumps(planning.planning_step('P')))",
            agents_root=pdir.parent,
        )
        assert not any(item.get("action") == "PLAN_TASK_RECOVERY" for item in json.loads(again.stdout))
        assert ResearchPlanGraph(pdir).snapshot()["plan_version"] == 2
        assert len(list((pdir / "tasks" / "v2").glob("**/*.md"))) == 1
        resolved = HandoffRequests(ProgressLedger(pdir)).get(review["handoff_id"])
        assert resolved and resolved["status"] == "RESOLVED"
        _assert_control_invariants(pdir)


def test_experiment_checkpoint_recovers_in_fresh_process_without_duplicate_evidence(tmp_path: Path):
    store, protocol_id = E._fixture(tmp_path)
    (tmp_path / "fail_u2").touch()
    failed = _fresh(
        f"from danus.execution.experiment import run_experiment; run_experiment({str(tmp_path)!r}, {protocol_id!r})",
        cwd=tmp_path,
        check=False,
    )
    assert failed.returncode != 0
    assert (tmp_path / "counts" / "u1.txt").read_text() == "1"

    (tmp_path / "fail_u2").unlink()
    recovered = _fresh(
        f"import json; from danus.execution.experiment import run_experiment; "
        f"print(json.dumps(run_experiment({str(tmp_path)!r}, {protocol_id!r})))",
        cwd=tmp_path,
    )
    first = json.loads(recovered.stdout)
    assert (tmp_path / "counts" / "u1.txt").read_text() == "1"
    assert (tmp_path / "counts" / "u2.txt").read_text() == "2"

    again = _fresh(
        f"import json; from danus.execution.experiment import run_experiment; "
        f"print(json.dumps(run_experiment({str(tmp_path)!r}, {protocol_id!r})))",
        cwd=tmp_path,
    )
    second = json.loads(again.stdout)
    assert second["evidence_id"] == first["evidence_id"]
    assert (tmp_path / "counts" / "u1.txt").read_text() == "1"
    assert (tmp_path / "counts" / "u2.txt").read_text() == "2"
    records = list((tmp_path / "evidence" / "records").glob("*.json"))
    assert len(records) == 1
    assert store.get_record(first["evidence_id"])


def test_resource_and_external_blocker_chain_survives_restarts(tmp_path: Path):
    with R._root(tmp_path):
        pdir, wl, ledger, assignment_id = R._setup()
        wl.status.write_text(
            json.dumps({"state": "stopped", "round": 1, "context_tokens_total": 120}),
            encoding="utf-8",
        )
        cli.do_supervise("P", apply=True, token_budget=100)
        assert R._pause_reason(wl) if hasattr(R, "_pause_reason") else wl.pause.read_text().strip() == "budget:resource_token_budget"
        assert wl.pause.read_text(encoding="utf-8").strip() == "budget:resource_token_budget"

        _fresh(
            "import os; from danus.orchestration import cli; "
            "cli.spawn_loop=lambda path: os.getpid(); "
            "cli.do_supervise('P', apply=True, token_budget=200)",
            agents_root=pdir.parent,
        )
        assert not wl.pause.exists()
        assert ProgressLedger(pdir).current("high", refresh_stale=False)["assignment_id"] == assignment_id

        wl.pause.write_text("quota:pressure:test\n", encoding="utf-8")
        _fresh(
            "import os; from danus.orchestration import cli; "
            "cli.spawn_loop=lambda path: os.getpid(); "
            "rows=cli._project_rows('P'); "
            "cli._apply_quota_gate('P', rows, {'state':'NORMAL','reason':'restored','block_new_codex_rounds':False})",
            agents_root=pdir.parent,
        )
        assert not wl.pause.exists()

        ledger = ProgressLedger(pdir)
        ledger.update(worker="high", state="BLOCKED", actor="runtime", outcome="BLOCKED")
        (pdir / L.PAUSE_FILE).write_text("manual\n", encoding="utf-8")
        project_resume = _fresh(
            "import json; from danus.orchestration import steering; "
            "print(json.dumps(steering.do_resume('P')))",
            agents_root=pdir.parent,
        )
        assert json.loads(project_resume.stdout) == [{"worker": "high", "result": "idle"}]
        blocked = ProgressLedger(pdir).current("high", refresh_stale=False)
        assert blocked["assignment_id"] == assignment_id
        assert blocked["state"] == "BLOCKED" and blocked["outcome"] == "BLOCKED"

        worker_resume = _fresh(
            "import json, os; from danus.orchestration import cli, steering; "
            "cli.spawn_loop=lambda path: os.getpid(); "
            "print(json.dumps(steering.do_resume('P/high')))",
            agents_root=pdir.parent,
        )
        assert json.loads(worker_resume.stdout) == [{"worker": "high", "result": "started"}]
        running = ProgressLedger(pdir).current("high", refresh_stale=False)
        assert running["assignment_id"] == assignment_id and running["state"] == "RUNNING"

        ProgressLedger(pdir).update(worker="high", state="BLOCKED", actor="runtime", outcome="NEEDS_REVIEW")
        review_resume = _fresh(
            "import json; from danus.orchestration import steering; "
            "print(json.dumps(steering.do_resume('P/high')))",
            agents_root=pdir.parent,
        )
        assert json.loads(review_resume.stdout) == [{"worker": "high", "result": "needs-review"}]
        final = ProgressLedger(pdir).current("high", refresh_stale=False)
        assert final["assignment_id"] == assignment_id
        assert final["state"] == "BLOCKED" and final["outcome"] == "NEEDS_REVIEW"
        _assert_control_invariants(pdir)
