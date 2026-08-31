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
    #activity { height: 9; border: round $warning; }
    """

    def __init__(self, project: str) -> None:
        super().__init__()
        self.project_name = project
        self.snapshot = load_project_snapshot(project)

    @staticmethod
    def _node_label(node: Dict[str, Any]) -> str:
        marker = "✓" if node["state"] == "DONE" else "·" if node["state"] == "OPEN" else "×"
        return f"{marker} {node['node_id']}  {node['title']}"

    def _plan_nodes(self) -> tuple[Dict[str, Dict[str, Any]], Dict[str, list[str]]]:
        nodes = {node["node_id"]: node for node in self.snapshot["nodes"]}
        children: Dict[str, list[str]] = {node_id: [] for node_id in nodes}
        for node_id, node in nodes.items():
            parent = node.get("parent")
            if parent in children:
                children[parent].append(node_id)
        for values in children.values():
            values.sort()
        return nodes, children

    def _populate_plan(self, tree: Tree, *, reset: bool = False) -> None:
        nodes, children = self._plan_nodes()
        root_id = self.snapshot["root_id"]
        if reset:
            tree.root.remove_children()
            tree.root.set_label(self._node_label(nodes[root_id]))

        def add(parent, node_id: str) -> None:
            for child_id in children[node_id]:
                child = parent.add(self._node_label(nodes[child_id]))
                add(child, child_id)
                child.expand()

        add(tree.root, root_id)
        tree.root.expand()

    def _plan_tree(self) -> Tree:
        nodes, _ = self._plan_nodes()
        tree = Tree(self._node_label(nodes[self.snapshot["root_id"]]), id="plan")
        self._populate_plan(tree)
        return tree

    def _populate_workers(self, table: DataTable) -> None:
        table.clear()
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

    def _workers_table(self) -> DataTable:
        table = DataTable(id="workers")
        table.add_columns("Worker", "Role", "Runtime", "Node", "Progress", "Round", "Round age")
        self._populate_workers(table)
        return table

    @staticmethod
    def _progress_bar(percent: float, width: int = 18) -> str:
        filled = max(0, min(width, round(width * percent / 100.0)))
        return "█" * filled + "░" * (width - filled)

    def _populate_experiments(self, table: DataTable) -> None:
        table.clear()
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

    def _experiments_table(self) -> DataTable:
        table = DataTable(id="experiments")
        table.add_columns("Experiment", "Progress", "State", "Current unit")
        self._populate_experiments(table)
        return table

    @staticmethod
    def _activity_time(value: str) -> str:
        return value[11:19] if len(value) >= 19 and "T" in value else value

    def _populate_activity(self, table: DataTable) -> None:
        table.clear()
        for row in self.snapshot["activity"]:
            table.add_row(
                self._activity_time(str(row["at"])),
                row["role"],
                row["source"],
                row["message"],
            )
        if not self.snapshot["activity"]:
            table.add_row("—", "—", "—", "No observable activity yet")

    def _activity_table(self) -> DataTable:
        table = DataTable(id="activity")
        table.add_columns("Time", "Role", "Source", "Activity")
        self._populate_activity(table)
        return table

    def _summary_text(self) -> str:
        return (
            f"Project: {self.project_name}    State: {self.snapshot['project_state']}    "
            f"Plan: v{self.snapshot['plan_version']}"
        )

    def _quota_text(self) -> str:
        quota = self.snapshot["quota"]
        remaining = quota.get("weekly_remaining_percent")
        weekly = "" if remaining is None else f" · weekly {remaining:g}% remaining"
        return f"Quota: {quota['state']} · {quota['reason']}{weekly}"

    def on_mount(self) -> None:
        self.set_interval(2.0, self._refresh_snapshot)

    def _refresh_snapshot(self) -> None:
        self.snapshot = load_project_snapshot(self.project_name)
        self.query_one("#summary", Static).update(self._summary_text())
        self.query_one("#quota", Static).update(self._quota_text())
        self._populate_plan(self.query_one("#plan", Tree), reset=True)
        self._populate_workers(self.query_one("#workers", DataTable))
        self._populate_experiments(self.query_one("#experiments", DataTable))
        self._populate_activity(self.query_one("#activity", DataTable))

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._summary_text(), id="summary")
        with Horizontal(id="body"):
            yield self._plan_tree()
            yield self._workers_table()
        yield self._experiments_table()
        yield self._activity_table()
        yield Static(self._quota_text(), id="quota")
        yield Footer()
