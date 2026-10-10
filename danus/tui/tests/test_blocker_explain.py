"""Read-only blocker explanation, escalation visibility, and operator UX tests."""
from __future__ import annotations

import asyncio
from textual.widgets import Static

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.tui.app import BlockerScreen, DanusTUI
from danus.tui.state import load_project_snapshot


def _blocked_project(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    project = root / "P"
    worker = project / "workers" / "low3"
    worker.mkdir(parents=True)
    (worker / ".role").write_text("ROLE=experimenter\n", encoding="utf-8")
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    ResearchPlanGraph.create(project, node_id="P0", title="Scientific Pilot")
    ledger = ProgressLedger(project)
    assignment = ledger.assign(
        worker="low3", role="experimenter", node_id="P0",
        plan_version=1, task="Frozen execution pilot",
    )
    ledger.update(
        worker="low3", state="BLOCKED", actor="runtime", outcome="BLOCKED",
        result="Approved runtime gate blocked: numpy and torch are unavailable.",
    )
    handoff = HandoffRequests(ledger).open(
        kind="ESCALATE", plan_version=1, node_id="P0",
        subject_type="ASSIGNMENT", subject_id=assignment["assignment_id"],
        requested_by="low3", source_role="experimenter",
        request="Provision a frozen environment with NumPy and PyTorch.",
        reason="Please provision a compatible isolated runtime; no experiment ran.",
        source_assignment_id=assignment["assignment_id"],
    )
    return project, ledger, handoff


def test_snapshot_exposes_original_blocker_and_main_escalation_read_only(tmp_path, monkeypatch):
    project, ledger, handoff = _blocked_project(tmp_path, monkeypatch)
    before = ledger.path.read_bytes()
    snapshot = load_project_snapshot("P")
    assert ledger.path.read_bytes() == before
    assert snapshot["project_state"] == "RUNNING"
    assert len(snapshot["blockers"]) == 1
    blocker = snapshot["blockers"][0]
    assert blocker["node_id"] == "P0"
    assert blocker["worker"] == "low3"
    assert "numpy and torch" in blocker["reason"]
    assert blocker["escalation_id"] == handoff["handoff_id"]
    assert blocker["escalation_status"] == "OPEN"
    assert blocker["escalation_target"] == "main"
    assert "compatible isolated runtime" in blocker["request"]
    assert "separate pinned numerical Python environment" in blocker["next_step"]


def test_tui_shows_blocker_banner_and_full_b_key_details(tmp_path, monkeypatch):
    project, ledger, handoff = _blocked_project(tmp_path, monkeypatch)
    before = ledger.path.read_bytes()

    async def check():
        app = DanusTUI("P")
        async with app.run_test(size=(120, 40)) as pilot:
            banner = app.query_one("#blockers", Static)
            assert banner.display is True
            assert "BLOCKED P0 / low3" in str(banner.render())
            await pilot.press("b")
            await pilot.pause()
            assert isinstance(app.screen, BlockerScreen)
            assert handoff["handoff_id"] in app.screen.content
            assert "Please provision a compatible isolated runtime" in app.screen.content
            assert "Do not mutate a shared Python venv" in app.screen.content
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, BlockerScreen)
            await pilot.press("b")
            await pilot.pause()
            assert isinstance(app.screen, BlockerScreen)
            await pilot.press("b")
            await pilot.pause()
            assert not isinstance(app.screen, BlockerScreen)
            await pilot.press("b")
            await pilot.pause()
            assert isinstance(app.screen, BlockerScreen)
            app.screen.query_one("#blocker-close").press()
            await pilot.pause()
            assert not isinstance(app.screen, BlockerScreen)
    asyncio.run(check())
    assert ledger.path.read_bytes() == before
