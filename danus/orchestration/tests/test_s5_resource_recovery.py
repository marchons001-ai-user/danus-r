"""S5-C resource, external blocker, and cold-start recovery."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from danus.core import EvidenceStore, ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.execution import layout as L
from danus.orchestration import cli, quota as Q, steering


@contextmanager
def _root(tmp_path: Path):
    old = os.environ.get("DANUS_AGENTS_ROOT")
    os.environ["DANUS_AGENTS_ROOT"] = str(tmp_path / "projects")
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("DANUS_AGENTS_ROOT", None)
        else:
            os.environ["DANUS_AGENTS_ROOT"] = old


def _setup() -> tuple[Path, L.WorkerLayout, ProgressLedger, str]:
    cli.do_new("P", roles="high:1")
    pdir = L.project_dir("P")
    plan = ResearchPlanGraph.create(pdir, node_id="root", title="Root")
    wl = L.WorkerLayout(L.worker_dir("P", "high"))
    wl.role.write_text("ROLE=researcher\nREASONING_EFFORT=low\n", encoding="utf-8")
    task = "work on root\n"
    wl.task.write_text(task, encoding="utf-8")
    ledger = ProgressLedger(pdir)
    assignment = ledger.assign(
        worker="high",
        role="researcher",
        node_id="root",
        plan_version=plan.snapshot()["plan_version"],
        task=task,
    )
    return pdir, wl, ledger, assignment["assignment_id"]


def _fake_spawn(monkeypatch):
    calls = []

    def spawn(path):
        calls.append(Path(path))
        return os.getpid()

    monkeypatch.setattr(cli, "spawn_loop", spawn)
    return calls


def test_token_budget_pause_recovers_same_assignment(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, wl, ledger, assignment_id = _setup()
        wl.status.write_text(
            json.dumps({"state": "stopped", "round": 1, "context_tokens_total": 120}),
            encoding="utf-8",
        )

        stopped = cli.do_supervise("P", apply=True, token_budget=100)
        assert stopped["budget_stop"] == "resource_token_budget"
        assert wl.pause.read_text(encoding="utf-8").strip() == "budget:resource_token_budget"

        calls = _fake_spawn(monkeypatch)
        resumed = cli.do_supervise("P", apply=True, token_budget=200)
        assert "budget_stop" not in resumed
        assert not wl.pause.exists()
        assert calls == [wl.dir]
        assert ledger.current("high", refresh_stale=False)["assignment_id"] == assignment_id


def test_time_budget_pause_recovers_when_deadline_moves_forward(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, wl, ledger, assignment_id = _setup()
        (pdir / L.DEADLINE_FILE).write_text(str(time.time() - 1), encoding="utf-8")

        stopped = cli.do_supervise("P", apply=True)
        assert stopped["budget_stop"] == "resource_time_budget"
        assert wl.pause.read_text(encoding="utf-8").strip() == "budget:resource_time_budget"

        (pdir / L.DEADLINE_FILE).write_text(str(time.time() + 3600), encoding="utf-8")
        calls = _fake_spawn(monkeypatch)
        resumed = cli.do_supervise("P", apply=True)
        assert "budget_stop" not in resumed
        assert not wl.pause.exists()
        assert calls == [wl.dir]
        assert ledger.current("high", refresh_stale=False)["assignment_id"] == assignment_id


@pytest.mark.parametrize("reason", ["manual", "review:scientific", "quota:pressure:test"])
def test_budget_recovery_never_clears_foreign_pause(tmp_path: Path, reason: str):
    with _root(tmp_path):
        pdir, wl, _, _ = _setup()
        wl.pause.write_text(reason + "\n", encoding="utf-8")
        rows = cli._project_rows("P")
        budget = cli._resource_snapshot(pdir, rows, 0)
        cli._clear_recovered_budget_pauses("P", rows, budget)
        assert wl.pause.read_text(encoding="utf-8").strip() == reason


def test_quota_pause_recovers_only_its_own_worker_pause(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        _, wl, ledger, assignment_id = _setup()
        wl.pause.write_text("quota:pressure:test\n", encoding="utf-8")
        rows = cli._project_rows("P")
        calls = _fake_spawn(monkeypatch)

        applied = cli._apply_quota_gate(
            "P",
            rows,
            {"state": "NORMAL", "reason": "headroom_restored", "block_new_codex_rounds": False},
        )
        assert applied == [{"worker": "high", "result": "started"}]
        assert not wl.pause.exists()
        assert calls == [wl.dir]
        assert ledger.current("high", refresh_stale=False)["assignment_id"] == assignment_id


@pytest.mark.parametrize("reason", ["manual", "review:scientific", "budget:resource_token_budget"])
def test_quota_recovery_never_clears_foreign_pause(tmp_path: Path, reason: str):
    with _root(tmp_path):
        _, wl, _, _ = _setup()
        wl.pause.write_text(reason + "\n", encoding="utf-8")
        rows = cli._project_rows("P")
        applied = cli._apply_quota_gate(
            "P",
            rows,
            {"state": "NORMAL", "reason": "headroom_restored", "block_new_codex_rounds": False},
        )
        assert applied == []
        assert wl.pause.read_text(encoding="utf-8").strip() == reason


def test_explicit_worker_resume_recovers_external_blocker_same_assignment(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        _, wl, ledger, assignment_id = _setup()
        ledger.update(worker="high", state="BLOCKED", actor="runtime", outcome="BLOCKED")
        wl.pause.write_text("review:assignment_blocked\n", encoding="utf-8")
        calls = _fake_spawn(monkeypatch)

        result = steering.do_resume("P/high")
        current = ledger.current("high", refresh_stale=False)
        assert result == [{"worker": "high", "result": "started"}]
        assert current["assignment_id"] == assignment_id
        assert current["state"] == "RUNNING"
        assert current.get("outcome") is None
        assert not wl.pause.exists()
        assert calls == [wl.dir]


def test_explicit_resume_does_not_clear_needs_review(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        _, wl, ledger, assignment_id = _setup()
        ledger.update(worker="high", state="BLOCKED", actor="runtime", outcome="NEEDS_REVIEW")
        wl.pause.write_text("review:assignment_needs_review\n", encoding="utf-8")
        calls = _fake_spawn(monkeypatch)

        result = steering.do_resume("P/high")
        current = ledger.current("high", refresh_stale=False)
        assert result == [{"worker": "high", "result": "needs-review"}]
        assert current["assignment_id"] == assignment_id
        assert current["state"] == "BLOCKED"
        assert current["outcome"] == "NEEDS_REVIEW"
        assert wl.pause.read_text(encoding="utf-8").strip() == "review:assignment_needs_review"
        assert calls == []


def test_project_resume_does_not_clear_external_blocker(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, _, ledger, assignment_id = _setup()
        ledger.update(worker="high", state="BLOCKED", actor="runtime", outcome="BLOCKED")
        (pdir / L.PAUSE_FILE).write_text("manual\n", encoding="utf-8")
        calls = _fake_spawn(monkeypatch)

        result = steering.do_resume("P")
        current = ledger.current("high", refresh_stale=False)
        assert result == [{"worker": "high", "result": "idle"}]
        assert current["assignment_id"] == assignment_id
        assert current["state"] == "BLOCKED"
        assert current["outcome"] == "BLOCKED"
        assert calls == []


def test_cold_start_reconstructs_durable_control_state(tmp_path: Path):
    with _root(tmp_path):
        pdir, wl, ledger, assignment_id = _setup()
        plan = ResearchPlanGraph(pdir)
        plan_version = plan.snapshot()["plan_version"]

        contract = ledger.submit_implementation_contract(
            proposer="high",
            node_id="root",
            plan_version=plan_version,
            goal="preserve the current scientific task",
            must_preserve_invariants=["same plan node"],
            forbidden_behaviors=["invent new work"],
            acceptance_conditions=["durable state reloads"],
        )
        handoff = HandoffRequests(ledger).open(
            kind="ESCALATE",
            plan_version=plan_version,
            node_id="root",
            subject_type="ASSIGNMENT",
            subject_id=assignment_id,
            requested_by="high",
            source_role="researcher",
            source_assignment_id=assignment_id,
            request="surface the durable blocker",
            reason="cold-start sentinel",
        )

        store = EvidenceStore(pdir)
        protocol_id = store.freeze_protocol(
            question="Can durable evidence survive process restart?",
            hypothesis="Disk state is sufficient.",
            matrix={"units": [{"id": "u1"}]},
            primary_metrics=["ok"],
            decision_rule="record one result",
            author="reviewer",
        )
        evidence_id = store.add_record(
            protocol_id=protocol_id,
            author="experimenter",
            matrix_coverage={"planned": 1, "completed": 1},
            primary_metrics={"ok": 1},
        )
        checkpoint = pdir / "experiment_runtime" / protocol_id / "checkpoint.json"
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_text(
            json.dumps({
                "protocol_id": protocol_id,
                "completed": {"u1": []},
                "failures": [],
                "evidence_id": evidence_id,
            }),
            encoding="utf-8",
        )
        wl.pause.write_text("budget:resource_token_budget\n", encoding="utf-8")
        wl.status.write_text(
            json.dumps({"state": "stopped", "round": 1, "context_tokens_total": 120}),
            encoding="utf-8",
        )
        quota_path = Q.quota_path(pdir)
        quota_path.parent.mkdir(parents=True, exist_ok=True)
        quota_path.write_text(
            json.dumps({"schema_version": 1, "available": False, "error": "cold-start-sentinel"}),
            encoding="utf-8",
        )

        repo_root = Path(__file__).resolve().parents[3]
        env = os.environ.copy()
        env["DANUS_AGENTS_ROOT"] = str(pdir.parent)
        env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
        code = f"""
import json
from danus.core import EvidenceStore, ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.execution import layout as L
from danus.orchestration import cli, quota as Q
pdir = L.project_dir('P')
ledger = ProgressLedger(pdir)
rows = cli.do_status('P')
budget = cli._resource_snapshot(pdir, rows, 0)
print(json.dumps({{
    'plan_version': ResearchPlanGraph(pdir).snapshot()['plan_version'],
    'assignment_id': ledger.current('high', refresh_stale=False)['assignment_id'],
    'contract_id': ledger.implementation_contract({contract['contract_id']!r})['contract_id'],
    'handoff_id': HandoffRequests(ledger).get({handoff['handoff_id']!r})['handoff_id'],
    'evidence_id': EvidenceStore(pdir).get_record({evidence_id!r})['evidence_id'],
    'pause_reason': rows[0]['pause_reason'],
    'quota_error': Q.load_quota(pdir)['error'],
    'checkpoint_protocol': budget['experiments'][0]['protocol_id'],
}}, sort_keys=True))
"""
        child = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        recovered = json.loads(child.stdout)
        assert recovered == {
            "assignment_id": assignment_id,
            "checkpoint_protocol": protocol_id,
            "contract_id": contract["contract_id"],
            "evidence_id": evidence_id,
            "handoff_id": handoff["handoff_id"],
            "pause_reason": "budget:resource_token_budget",
            "plan_version": plan_version,
            "quota_error": "cold-start-sentinel",
        }
