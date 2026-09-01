"""Offline A2-9c TUI steering tests. No LLM or network."""

from __future__ import annotations

import asyncio

from textual.widgets import Input

from danus.core import ResearchPlanGraph
from danus.execution import layout as L
from danus.tui.app import DanusTUI, GuidanceScreen, InterruptConfirmScreen


def _project(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    project = root / "P"
    (project / "workers").mkdir(parents=True)
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    ResearchPlanGraph.create(project, node_id="P0", title="Research Project")
    return project


def test_tui_pause_resume_uses_existing_control_apis(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    calls = []

    def fake_pause(target):
        calls.append(("pause", target))
        (project / L.PAUSE_FILE).write_text("manual\n", encoding="utf-8")
        return []

    def fake_resume(target):
        calls.append(("resume", target))
        (project / L.PAUSE_FILE).unlink(missing_ok=True)
        return []

    monkeypatch.setattr("danus.tui.app.C.do_pause", fake_pause)
    monkeypatch.setattr("danus.tui.app.S.do_resume", fake_resume)

    async def check() -> None:
        app = DanusTUI("P")
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("p")
            await pilot.pause()
            assert calls == [("pause", "P")]
            assert app.snapshot["project_state"] == "PAUSED"

            await pilot.press("p")
            await pilot.pause()
            assert calls == [("pause", "P"), ("resume", "P")]
            assert app.snapshot["project_state"] == "RUNNING"

    asyncio.run(check())


def test_tui_guidance_and_confirmed_interrupt_use_existing_steering_api(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    calls = []

    def fake_guide(project, text, *, interrupt=False):
        calls.append((project, text, interrupt))
        return {
            "project": project,
            "mode": "interrupt" if interrupt else "guide",
            "interrupted": [{"worker": "r1"}] if interrupt else [],
        }

    monkeypatch.setattr("danus.tui.app.S.do_guide", fake_guide)

    async def check() -> None:
        app = DanusTUI("P")
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("g")
            await pilot.pause()
            assert isinstance(app.screen, GuidanceScreen)
            app.screen.query_one("#guidance-input", Input).value = "Keep the scope narrow."
            await pilot.press("enter")
            await pilot.pause()
            assert calls == [("P", "Keep the scope narrow.", False)]

            # A real terminal encodes Shift+I as the printable character "I".
            await pilot.press("I")
            await pilot.pause()
            assert isinstance(app.screen, GuidanceScreen)
            app.screen.query_one("#guidance-input", Input).value = "Switch direction now."
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, InterruptConfirmScreen)
            assert calls == [("P", "Keep the scope narrow.", False)]

            await pilot.press("enter")
            await pilot.pause()
            assert calls == [
                ("P", "Keep the scope narrow.", False),
                ("P", "Switch direction now.", True),
            ]

    asyncio.run(check())
