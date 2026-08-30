"""A2-7q regressions reproduced by the pre-pilot soak. No LLM or network."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.execution import layout as L
from danus.orchestration import cli
from danus.orchestration.control import reconcile


@contextmanager
def _agents_root(tmp_path: Path):
    old = os.environ.get("DANUS_AGENTS_ROOT")
    os.environ["DANUS_AGENTS_ROOT"] = str(tmp_path / "projects")
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("DANUS_AGENTS_ROOT", None)
        else:
            os.environ["DANUS_AGENTS_ROOT"] = old


def test_dsh_usage_is_unknown_not_zero(tmp_path: Path):
    with _agents_root(tmp_path):
        cli.do_new("P", roles="low:1")
        wl = L.WorkerLayout(L.worker_dir("P", "low"))
        wl.role.write_text("ROLE=reviewer\nBACKEND=auto\n", encoding="utf-8")
        wl.status.write_text(
            json.dumps({
                "state": "stopped",
                "round": 1,
                "context_tokens": 0,
                "context_tokens_total": 0,
                "context_tool_calls": 0,
                "context_compactions": 0,
                "reviewer_backend_selected": "dsh",
            }),
            encoding="utf-8",
        )

        row = cli.worker_status(wl)
        assert row["round_usage_tokens"] is None
        assert row["usage_tokens_total"] is None
        assert row["usage_tool_calls"] is None
        assert row["usage_available"] is False
        assert row["usage_complete"] is False
        assert row["context_tokens"] is None


def test_project_token_total_marks_unknown_backend_as_lower_bound(tmp_path: Path):
    rows = [
        {
            "worker": "codex",
            "round_usage_tokens": 40,
            "usage_tokens_total": 100,
            "usage_tool_calls": 2,
            "usage_compactions": 0,
            "usage_available": True,
            "usage_complete": True,
            "context_trigger": None,
        },
        {
            "worker": "dsh",
            "round_usage_tokens": None,
            "usage_tokens_total": None,
            "usage_tool_calls": None,
            "usage_compactions": None,
            "usage_available": False,
            "usage_complete": False,
            "context_trigger": None,
        },
    ]
    snapshot = cli._resource_snapshot(tmp_path, rows, token_budget=150)
    assert snapshot["tokens"]["used"] == 100
    assert snapshot["tokens"]["complete"] is False
    assert snapshot["tokens"]["used_is_lower_bound"] is True
    assert snapshot["tokens"]["unknown_workers"] == ["dsh"]
    assert snapshot["tokens"]["exceeded"] is None


def test_completed_project_has_clean_terminal_state(tmp_path: Path):
    pdir = tmp_path / "P"
    plan = ResearchPlanGraph.create(pdir, node_id="root", title="Root")
    ledger = ProgressLedger(pdir)
    ledger.set_node_state("root", state="DONE", actor="supervisor")

    report = reconcile(pdir, [])
    assert plan.snapshot()["root_id"] == "root"
    assert report["project_state"] == "COMPLETE"
    assert report["frontier"] == []
    assert report["plan_changes"] == []
    assert report["decisions"] == []


def test_completed_reviewer_assignment_is_not_routed_again(tmp_path: Path):
    with _agents_root(tmp_path):
        cli.do_new("P", roles="low:1")
        wl = L.WorkerLayout(L.worker_dir("P", "low"))
        wl.role.write_text("ROLE=reviewer\nBACKEND=auto\n", encoding="utf-8")
        rows = [{
            "worker": "low",
            "alive": False,
            "assignment_id": "finished",
            "progress_state": "DONE",
        }]
        quota = {"state": "NORMAL", "block_new_codex_rounds": False}
        assert cli._reviewer_cost_rows("P", rows, quota, apply=False) == []
