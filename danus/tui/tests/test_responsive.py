"""Responsive-layout regression for the A2-9 TUI."""

from __future__ import annotations

import asyncio

from textual.widgets import DataTable, Static, Tree

from danus.core import ResearchPlanGraph
from danus.tui.app import DanusTUI


def test_tui_resizes_wide_medium_and_compact(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    project = root / "P"
    (project / "workers").mkdir(parents=True)
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    ResearchPlanGraph.create(project, node_id="P0", title="Research Project")

    async def check() -> None:
        app = DanusTUI("P")
        async with app.run_test(size=(160, 50)) as pilot:
            plan = app.query_one("#plan", Tree)
            workers = app.query_one("#workers", DataTable)
            experiments = app.query_one("#experiments", DataTable)
            summary = app.query_one("#summary", Static)

            assert app.screen.has_class("-wide")
            assert app.screen.has_class("-tall")
            assert plan.region.y == workers.region.y
            assert experiments.region.height == 9
            assert summary.region.height == 3

            await pilot.resize_terminal(120, 36)
            await pilot.pause()
            assert app.screen.has_class("-medium")
            assert app.screen.has_class("-medium-height")
            assert plan.region.y == workers.region.y
            assert experiments.region.height == 7
            assert summary.region.height == 1

            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert app.screen.has_class("-compact")
            assert app.screen.has_class("-short")
            assert plan.region.x == workers.region.x
            assert plan.region.y < workers.region.y
            assert experiments.region.height == 5
            assert summary.region.height == 1

    asyncio.run(check())
