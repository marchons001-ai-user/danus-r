"""Minimal read-only project picker for ``danus tui`` without a project."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Static

from danus.execution import layout as L


class ProjectSelector(App[str | None]):
    TITLE = "Danus-R"
    AUTO_FOCUS = "#projects"
    BINDINGS = [
        Binding("enter", "select", "Open"),
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", "Quit"),
    ]
    CSS = """
    #selector-title { height: 3; padding: 1 2; text-style: bold; }
    #projects { height: 1fr; border: round $primary; }
    #selector-help { height: 3; padding: 1 2; color: $text-muted; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.projects = L.list_projects()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Select a Danus-R project", id="selector-title")
        table = DataTable(id="projects")
        table.cursor_type = "row"
        table.add_columns("Project", "Workers")
        for project in self.projects:
            table.add_row(project, str(len(L.list_workers(project))))
        if not self.projects:
            table.add_row("—", "No projects found")
        yield table
        yield Static("↑/↓ select · Enter open · Esc/q quit", id="selector-help")
        yield Footer()

    def action_select(self) -> None:
        if not self.projects:
            return
        row = self.query_one("#projects", DataTable).cursor_row
        if 0 <= row < len(self.projects):
            self.exit(self.projects[row])

    def action_cancel(self) -> None:
        self.exit(None)
