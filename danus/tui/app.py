"""Minimal read-only Textual shell for Danus-R."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Static


class DanusTUI(App[None]):
    TITLE = "Danus-R"
    SUB_TITLE = "A2-9a-1"
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, project: str) -> None:
        super().__init__()
        self.project_name = project

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Project: {self.project_name}", id="project")
        yield Static("Read-only TUI skeleton", id="stage")
        yield Footer()
