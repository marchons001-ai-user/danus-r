"""Final TUI selector, quota refresh, small-screen, and error-state regressions."""

from __future__ import annotations

import asyncio
import os

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


def test_tui_without_project_opens_selected_project_with_fresh_quota(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    _project(root, "A")
    selected = _project(root, "B")
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    monkeypatch.setattr(ProjectSelector, "run", lambda self: "B")
    refreshed = []
    monkeypatch.setattr(
        tui.Q,
        "refresh_quota",
        lambda project_dir, timeout=15.0: refreshed.append((project_dir, timeout)) or {},
    )
    opened = []
    monkeypatch.setattr(DanusTUI, "run", lambda self: opened.append(self.project_name))

    assert tui.main([]) == 0
    assert opened == ["B"]
    assert refreshed == [(selected, 30.0)]


def test_quota_refresh_due_uses_persisted_snapshot_age(tmp_path, monkeypatch):
    project = tmp_path / "P"
    path = tui.Q.quota_path(project)
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (1000.0, 1000.0))

    monkeypatch.setattr(tui.time, "time", lambda: 1000.0 + tui._QUOTA_REFRESH_SECONDS - 1)
    assert tui._quota_refresh_due(project) is False

    monkeypatch.setattr(tui.time, "time", lambda: 1000.0 + tui._QUOTA_REFRESH_SECONDS)
    assert tui._quota_refresh_due(project) is True


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
    monkeypatch.setattr(tui.Q, "refresh_quota", lambda *args, **kwargs: {})

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
