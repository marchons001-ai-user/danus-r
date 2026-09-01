"""Final A2-9d selector, small-screen, and error-state regressions."""

from __future__ import annotations

import asyncio

import pytest
from textual.widgets import DataTable, Static

from danus import tui
from danus.core import ResearchPlanGraph
from danus.tui.app import DanusTUI
from danus.tui.selector import ProjectSelector


def _project(root, name: str):
    project = root / name
    (project / "workers").mkdir(parents=True)
    ResearchPlanGraph.create(project, node_id="P0", title="Research Project")
    return project


def test_tui_without_project_opens_selected_project(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    _project(root, "A")
    _project(root, "B")
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    monkeypatch.setattr(ProjectSelector, "run", lambda self: "B")
    opened = []
    monkeypatch.setattr(DanusTUI, "run", lambda self: opened.append(self.project_name))

    assert tui.main([]) == 0
    assert opened == ["B"]


def test_tui_without_projects_can_exit_cleanly(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    monkeypatch.setattr(ProjectSelector, "run", lambda self: None)

    assert tui.main([]) == 0


def test_tui_reports_corrupt_project_without_traceback(tmp_path, monkeypatch, capsys):
    root = tmp_path / "projects"
    (root / "broken" / "workers").mkdir(parents=True)
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))

    with pytest.raises(SystemExit) as exc:
        tui.main(["broken"])

    assert exc.value.code == 2
    assert "cannot open project broken" in capsys.readouterr().err


def test_project_selector_enter_opens_highlighted_project(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    _project(root, "A")
    _project(root, "B")
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))

    async def check() -> None:
        app = ProjectSelector()
        selected = []
        monkeypatch.setattr(app, "exit", lambda result=None, *args, **kwargs: selected.append(result))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert selected == ["B"]

    asyncio.run(check())


def test_project_selector_resizes_and_shows_empty_state(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))

    async def check() -> None:
        app = ProjectSelector()
        async with app.run_test(size=(80, 24)) as pilot:
            table = app.query_one("#projects", DataTable)
            title = app.query_one("#selector-title", Static)
            help_text = app.query_one("#selector-help", Static)
            assert app.projects == []
            assert table.row_count == 1
            assert title.region.height == 3
            assert help_text.region.height == 3
            assert table.region.height > 0

            await pilot.resize_terminal(120, 36)
            await pilot.pause()
            assert title.region.height == 3
            assert help_text.region.height == 3
            assert table.region.height > 0

            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert table.region.height > 0

    asyncio.run(check())
