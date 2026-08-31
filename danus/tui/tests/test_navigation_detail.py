"""Keyboard navigation, detail, filtering, and history tests for A2-9b."""

from __future__ import annotations

import asyncio

from textual.widgets import DataTable, Static, Tree

from danus.tui.app import DanusTUI


def _snapshot():
    activity = []
    roles = ["researcher", "reviewer", "experimenter", "supervisor"]
    for index in range(32):
        role = roles[index % len(roles)]
        activity.append(
            {
                "at": f"2026-08-31T12:{59 - index:02d}:00Z",
                "epoch": float(1000 - index),
                "role": role,
                "source": "MODEL" if role != "supervisor" else "SUPERVISOR",
                "message": f"{role} activity {index}",
            }
        )
    return {
        "project": "P",
        "project_state": "RUNNING",
        "plan_version": 2,
        "root_id": "P0",
        "nodes": [
            {
                "node_id": "P0",
                "title": "Research Project",
                "summary": "Root summary",
                "parent": None,
                "depends_on": [],
                "state": "OPEN",
            },
            {
                "node_id": "R1",
                "title": "Analyze evidence",
                "summary": "Read the frozen evidence and interpret it.",
                "parent": "P0",
                "depends_on": ["E1"],
                "state": "OPEN",
            },
            {
                "node_id": "E1",
                "title": "Run experiment",
                "summary": "Execute the frozen protocol.",
                "parent": "P0",
                "depends_on": [],
                "state": "DONE",
            },
        ],
        "workers": [
            {
                "worker": "low",
                "role": "researcher",
                "runtime": "working",
                "node_id": "R1",
                "progress_state": "RUNNING",
                "round": 3,
                "age_s": 12.0,
            },
            {
                "worker": "low2",
                "role": "reviewer",
                "runtime": "stopped",
                "node_id": "R1",
                "progress_state": "DONE",
                "round": 2,
                "age_s": None,
            },
        ],
        "experiments": [
            {
                "protocol_id": "abcdef0123456789",
                "label": "Robustness sweep",
                "state": "running",
                "completed_units": 3,
                "total_units": 5,
                "percent": 60.0,
                "current_unit": "case-4",
                "attempt": 1,
                "evidence_id": None,
            }
        ],
        "activity": activity,
        "quota": {"state": "NORMAL", "reason": "ok", "weekly_remaining_percent": 50.0},
    }


def test_keyboard_navigation_detail_filter_and_history(monkeypatch):
    snapshot = _snapshot()
    monkeypatch.setattr("danus.tui.app.load_project_snapshot", lambda project: snapshot)

    async def check() -> None:
        app = DanusTUI("P")
        async with app.run_test(size=(160, 50)) as pilot:
            detail = app.query_one("#detail", Static)
            plan = app.query_one("#plan", Tree)
            workers = app.query_one("#workers", DataTable)
            experiments = app.query_one("#experiments", DataTable)
            activity = app.query_one("#activity", DataTable)

            assert "PLAN NODE  P0" in str(detail.render())

            plan.focus()
            await pilot.press("down")
            await pilot.pause()
            assert "PLAN NODE" in str(detail.render())

            await pilot.press("tab")
            assert app.focused is workers
            await pilot.press("down")
            await pilot.pause()
            assert "WORKER  low2" in str(detail.render())

            await pilot.press("tab")
            assert app.focused is experiments
            await pilot.press("down")
            await pilot.pause()
            assert "EXPERIMENT  abcdef012345" in str(detail.render())

            await pilot.press("tab")
            assert app.focused is activity
            await pilot.press("pagedown")
            await pilot.pause()
            assert activity.cursor_row > 0
            assert "ACTIVITY" in str(detail.render())

            await pilot.press("1")
            await pilot.pause()
            assert app.activity_role == "researcher"
            assert activity.row_count == 8
            assert "Researcher" in str(activity.border_title)

            await pilot.press("2")
            assert app.activity_role == "reviewer"
            assert activity.row_count == 8

            await pilot.press("3")
            assert app.activity_role == "experimenter"
            assert activity.row_count == 8

            await pilot.press("4")
            assert app.activity_role == "supervisor"
            assert activity.row_count == 8

            await pilot.press("0")
            assert app.activity_role == "all"
            assert activity.row_count == 32

    asyncio.run(check())
