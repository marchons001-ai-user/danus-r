"""Offline A2-9a-1 TUI entry/skeleton tests."""

from __future__ import annotations

import asyncio
import runpy
import sys

import pytest
from textual.widgets import Static

from danus import tui
from danus.tui.app import DanusTUI


def test_tui_main_opens_existing_project(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    (root / "P" / "workers").mkdir(parents=True)
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    seen = []
    monkeypatch.setattr(DanusTUI, "run", lambda self: seen.append(self.project_name))

    assert tui.main(["P"]) == 0
    assert seen == ["P"]


def test_tui_main_rejects_unknown_project(tmp_path, monkeypatch):
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        tui.main(["missing"])
    assert exc.value.code == 2


def test_textual_skeleton_runs_headless():
    async def check() -> None:
        app = DanusTUI("P")
        async with app.run_test(size=(80, 24)):
            assert app.project_name == "P"
            assert app.query_one("#project", Static)
            assert app.query_one("#stage", Static)

    asyncio.run(check())


def test_orchestration_dispatches_tui(monkeypatch):
    calls = []
    monkeypatch.setattr(tui, "main", lambda argv: calls.append(list(argv)) or 0)
    monkeypatch.setattr(sys, "argv", ["danus.orchestration", "tui", "P"])

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("danus.orchestration", run_name="__main__")

    assert exc.value.code == 0
    assert calls == [["P"]]
