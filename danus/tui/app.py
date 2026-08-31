"""Read-only Textual dashboard for Danus-R."""

from __future__ import annotations

from typing import Any, Dict

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Static, Tree

from .state import load_project_snapshot


class DanusTUI(App[None]):
    TITLE = "Danus-R"
    BINDINGS = [("q", "quit", "Quit")]
    CSS = """
    #summary, #quota { height: 3; padding: 1 1; }
    #body { height: 1fr; }
    #plan { width: 2fr; border: round $primary; padding: 0 1; }
    #workers { width: 3fr; border: round $secondary; }
    #experiments { height: 9; border: round $accent; }
    """

    def __init__(self, project: str) -> None:
        super().__init__()
        self.project_name = project
        self.snapshot = load_project_snapshot(project)

    @staticmethod
    def _node_label(node: Dict[str, Any]) -> str:
        marker = "✓" if node["state"] == "DONE" else "·" if node["state"] == "OPEN" else "×"
        return f"{marker} {node['node_id']}  {node['title']}"

    def _plan_tree(self) -> Tree:
        nodes = {node["node_id"]: node for node in self.snapshot["nodes"]}
        children: Dict[str, list[str]] = {node_id: [] for node_id in nodes}
        for node_id, node in nodes.items():
            parent = node.get("parent")
            if parent in children:
                children[parent].append(node_id)
        for values in children.values():
            values.sort()

        root_id = self.snapshot["root_id"]
        tree = Tree(self._node_label(nodes[root_id]), id="plan")

        def add(parent, node_id: str) -> None:
            for child_id in children[node_id]:
                child = parent.add(self._node_label(nodes[child_id]))
                add(child, child_id)
                child.expand()

        add(tree.root, root_id)
        tree.root.expand()
        return tree

    def _workers_table(self) -> DataTable:
        table = DataTable(id="workers")
        table.add_columns("Worker", "Role", "Runtime", "Node", "Progress", "Round", "Age")
        for row in self.snapshot["workers"]:
            age = "—" if row["age_s"] is None else f"{row['age_s']:.0f}s"
            table.add_row(
                row["worker"],
                row["role"],
                row["runtime"],
                row["node_id"] or "—",
                row["progress_state"] or "—",
                str(row["round"]),
                age,
            )
        return table

    @staticmethod
    def _progress_bar(percent: float, width: int = 18) -> str:
        filled = max(0, min(width, round(width * percent / 100.0)))
        return "█" * filled + "░" * (width - filled)

    def _experiments_table(self) -> DataTable:
        table = DataTable(id="experiments")
        table.add_columns("Experiment", "Progress", "State", "Current unit")
        for row in self.snapshot["experiments"]:
            progress = (
                f"{self._progress_bar(row['percent'])}  {row['percent']:g}%  "
                f"{row['completed_units']}/{row['total_units']}"
            )
            current = row["current_unit"] or "—"
            if row["attempt"] is not None:
                current = f"{current} · attempt {row['attempt']}"
            table.add_row(
                f"{row['protocol_id'][:8]}  {row['label']}",
                progress,
                row["state"].upper(),
                current,
            )
        if not self.snapshot["experiments"]:
            table.add_row("—", "—", "NONE", "No experiment runtime yet")
        return table

    def _quota_text(self) -> str:
        quota = self.snapshot["quota"]
        remaining = quota.get("weekly_remaining_percent")
        weekly = "" if remaining is None else f" · weekly {remaining:g}% remaining"
        return f"Quota: {quota['state']} · {quota['reason']}{weekly}"

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            f"Project: {self.project_name}    State: {self.snapshot['project_state']}    "
            f"Plan: v{self.snapshot['plan_version']}",
            id="summary",
        )
        with Horizontal(id="body"):
            yield self._plan_tree()
            yield self._workers_table()
        yield self._experiments_table()
        yield Static(self._quota_text(), id="quota")
        yield Footer()
