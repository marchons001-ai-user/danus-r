"""A2-8d human steering regressions. No LLM or network."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from danus.core import ResearchPlanGraph
from danus.execution import layout as L
from danus.orchestration import cli, steering


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


def _project(tmp_path: Path):
    ctx = _agents_root(tmp_path)
    ctx.__enter__()
    cli.do_new("P", roles="low:2")
    pdir = L.project_dir("P")
    ResearchPlanGraph.create(pdir, node_id="R1", title="Research")
    return ctx, pdir


def test_guide_is_latest_durable_state_without_task_or_ledger_mutation(tmp_path: Path):
    ctx, pdir = _project(tmp_path)
    try:
        cli.do_assign("P/low", "Do the bounded research task.\n", node_id="R1")
        task = L.WorkerLayout(L.worker_dir("P", "low")).task
        ledger = pdir / "progress" / "ledger.jsonl"
        before_task = task.read_bytes()
        before_ledger = ledger.read_bytes()

        first = steering.do_guide("P", "Focus on the smallest decisive argument.")
        second = steering.do_guide("P", "Prefer the direct parity argument.")

        assert first["mode"] == "guide"
        assert first["interrupted"] == []
        assert second["interrupted"] == []
        assert steering.guidance_path("P").read_text(encoding="utf-8") == (
            "# Current Human Guidance\n\nPrefer the direct parity argument.\n"
        )
        assert task.read_bytes() == before_task
        assert ledger.read_bytes() == before_ledger
    finally:
        ctx.__exit__(None, None, None)


def test_guide_does_not_disturb_project_pause(tmp_path: Path):
    ctx, pdir = _project(tmp_path)
    try:
        cli.do_pause("P")
        result = steering.do_guide("P", "Keep the current scope narrow.")
        assert result["interrupted"] == []
        assert (pdir / L.PAUSE_FILE).is_file()
        assert steering.guidance_path("P").is_file()
    finally:
        ctx.__exit__(None, None, None)


def test_project_resume_starts_only_active_assignments(tmp_path: Path, monkeypatch):
    ctx, pdir = _project(tmp_path)
    try:
        cli.do_assign("P/low", "Continue R1.\n", node_id="R1")
        (pdir / L.PAUSE_FILE).write_text("manual\n", encoding="utf-8")
        started = []

        def fake_start(target: str, stagger: float = 0.2):
            started.append((target, stagger))
            return [{"worker": target.rsplit("/", 1)[-1], "result": "started"}]

        monkeypatch.setattr(steering, "do_start", fake_start)
        result = {row["worker"]: row["result"] for row in steering.do_resume("P")}

        assert result == {"low": "started", "low2": "idle"}
        assert started == [("P/low", 0)]
        assert not (pdir / L.PAUSE_FILE).exists()
    finally:
        ctx.__exit__(None, None, None)


def test_interrupt_restarts_only_workers_that_were_live(tmp_path: Path, monkeypatch):
    ctx, _ = _project(tmp_path)
    try:
        monkeypatch.setattr(
            steering,
            "do_status",
            lambda project: [
                {"worker": "low", "alive": True},
                {"worker": "low2", "alive": False},
            ],
        )
        stopped = []
        started = []

        def fake_stop(target: str, force: bool = False):
            stopped.append((target, force))
            return [{"worker": target.rsplit("/", 1)[-1], "result": "killed"}]

        def fake_start(target: str, stagger: float = 0.2):
            started.append((target, stagger))
            return [{"worker": target.rsplit("/", 1)[-1], "result": "started"}]

        monkeypatch.setattr(steering, "do_stop", fake_stop)
        monkeypatch.setattr(steering, "do_start", fake_start)

        result = steering.do_guide("P", "Change direction now.", interrupt=True)

        assert result["mode"] == "interrupt"
        assert stopped == [("P/low", True)]
        assert started == [("P/low", 0)]
        assert result["interrupted"] == [
            {"worker": "low", "stop_result": "killed", "start_result": "started"}
        ]
    finally:
        ctx.__exit__(None, None, None)


def test_scientific_contract_reads_guidance_without_bypassing_governance():
    text = L.scientific_worker_md().read_text(encoding="utf-8")
    assert "../../HUMAN_GUIDANCE.md" in text
    assert "does **not** itself mutate the Research Plan" in text
    assert "Researcher proposes" in text
    assert "Reviewer reviews" in text
    assert "Supervisor commits" in text
