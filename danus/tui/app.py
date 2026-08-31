"""Read-only Textual dashboard for Danus-R."""

from __future__ import annotations

from typing import Any, Dict

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import DataTable, Footer, Header, Static, Tree

from .state import load_project_snapshot


class DanusTUI(App[None]):
    TITLE = "Danus-R"
    AUTO_FOCUS = "#plan"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("0", "filter_all", "All activity", show=False),
        Binding("1", "filter_researcher", "Researcher activity", show=False),
        Binding("2", "filter_reviewer", "Reviewer activity", show=False),
        Binding("3", "filter_experimenter", "Experimenter activity", show=False),
        Binding("4", "filter_supervisor", "Supervisor activity", show=False),
    ]
    HORIZONTAL_BREAKPOINTS = [(0, "-compact"), (100, "-medium"), (140, "-wide")]
    VERTICAL_BREAKPOINTS = [(0, "-short"), (32, "-medium-height"), (44, "-tall")]
    ROLE_STYLES = {
        "researcher": "cyan",
        "reviewer": "yellow",
        "experimenter": "green",
        "supervisor": "magenta",
        "system": "dim",
    }
    CSS = """
    #summary, #quota { height: 3; padding: 1 1; }
    #body { height: 1fr; }
    #plan { width: 2fr; border: round $primary; padding: 0 1; }
    #right { width: 3fr; height: 1fr; }
    #workers { width: 100%; height: 1fr; border: round $secondary; }
    #detail { width: 100%; height: 2fr; border: round $success; padding: 1 2; }
    #experiments { height: 9; border: round $accent; }
    #activity { height: 9; border: round $warning; }

    Screen.-medium #plan { width: 1fr; }
    Screen.-medium #right { width: 2fr; }

    Screen.-compact #body { layout: vertical; }
    Screen.-compact #plan, Screen.-compact #right {
        width: 100%;
        height: 1fr;
    }
    Screen.-compact #workers, Screen.-compact #detail { height: 1fr; }

    Screen.-medium-height #summary, Screen.-medium-height #quota,
    Screen.-short #summary, Screen.-short #quota {
        height: 1;
        padding: 0 1;
    }
    Screen.-medium-height #experiments, Screen.-medium-height #activity { height: 7; }
    Screen.-short #experiments, Screen.-short #activity { height: 5; }
    """

    def __init__(self, project: str) -> None:
        super().__init__()
        self.project_name = project
        self.snapshot = load_project_snapshot(project)
        self.activity_role = "all"
        self._detail_kind = "plan"
        self._detail_key = str(self.snapshot["root_id"])

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
                child = parent.add(self._node_label(nodes[child_id]), data=child_id)
                add(child, child_id)
                child.expand()

        add(tree.root, root_id)
        tree.root.expand()

    def _plan_tree(self) -> Tree:
        nodes, _ = self._plan_nodes()
        root_id = self.snapshot["root_id"]
        tree = Tree(self._node_label(nodes[root_id]), data=root_id, id="plan")
        self._populate_plan(tree)
        return tree

    @staticmethod
    def _restore_cursor(table: DataTable, row: int | None) -> None:
        if row is not None and table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1), column=0)

    def _populate_workers(self, table: DataTable) -> None:
        selected = table.cursor_row if table.row_count else None
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
        self._restore_cursor(table, selected)

    def _workers_table(self) -> DataTable:
        table = DataTable(id="workers")
        table.cursor_type = "row"
        table.add_columns("Worker", "Role", "Runtime", "Node", "Progress", "Round", "Round age")
        self._populate_workers(table)
        return table

    @staticmethod
    def _progress_bar(percent: float, width: int = 18) -> str:
        filled = max(0, min(width, round(width * percent / 100.0)))
        return "█" * filled + "░" * (width - filled)

    def _populate_experiments(self, table: DataTable) -> None:
        selected = table.cursor_row if table.row_count else None
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
        self._restore_cursor(table, selected)

    def _experiments_table(self) -> DataTable:
        table = DataTable(id="experiments")
        table.cursor_type = "row"
        table.add_columns("Experiment", "Progress", "State", "Current unit")
        self._populate_experiments(table)
        return table

    @staticmethod
    def _activity_time(value: str) -> str:
        return value[11:19] if len(value) >= 19 and "T" in value else value

    def _visible_activity(self) -> list[Dict[str, Any]]:
        rows = self.snapshot["activity"]
        if self.activity_role == "all":
            return rows
        return [row for row in rows if row["role"] == self.activity_role]

    def _populate_activity(self, table: DataTable) -> None:
        selected = table.cursor_row if table.row_count else None
        table.clear()
        rows = self._visible_activity()
        for row in rows:
            style = self.ROLE_STYLES.get(row["role"], "")
            table.add_row(
                self._activity_time(str(row["at"])),
                Text(row["role"], style=style),
                row["source"],
                Text(row["message"], style=style),
            )
        if not rows:
            table.add_row("—", "—", "—", "No observable activity for this filter")
        label = "All" if self.activity_role == "all" else self.activity_role.capitalize()
        table.border_title = f"Live Activity · {label} · 0/1/2/3/4 filter"
        self._restore_cursor(table, selected)

    def _activity_table(self) -> DataTable:
        table = DataTable(id="activity")
        table.cursor_type = "row"
        table.add_columns("Time", "Role", "Source", "Activity")
        self._populate_activity(table)
        return table

    def _detail_widget(self) -> Static:
        detail = Static(id="detail")
        detail.border_title = "Detail"
        return detail

    def _set_detail(self, kind: str, key: str, lines: list[str]) -> None:
        self._detail_kind = kind
        self._detail_key = key
        try:
            detail = self.query_one("#detail", Static)
        except NoMatches:
            return
        detail.update(Text("\n".join(lines)))

    def _show_plan_detail(self, node_id: str) -> None:
        node = next((row for row in self.snapshot["nodes"] if row["node_id"] == node_id), None)
        if node is None:
            return
        depends = ", ".join(node["depends_on"]) or "—"
        summary = node.get("summary") or "—"
        self._set_detail(
            "plan",
            node_id,
            [
                f"PLAN NODE  {node['node_id']}",
                node["title"],
                "",
                f"State:      {node['state']}",
                f"Parent:     {node['parent'] or '—'}",
                f"Depends on: {depends}",
                "",
                f"Summary: {summary}",
            ],
        )

    def _show_worker_detail(self, index: int) -> None:
        if not 0 <= index < len(self.snapshot["workers"]):
            return
        row = self.snapshot["workers"][index]
        age = "—" if row["age_s"] is None else f"{row['age_s']:.0f}s"
        self._set_detail(
            "worker",
            row["worker"],
            [
                f"WORKER  {row['worker']}",
                "",
                f"Role:       {row['role']}",
                f"Runtime:    {row['runtime']}",
                f"Node:       {row['node_id'] or '—'}",
                f"Progress:   {row['progress_state'] or '—'}",
                f"Round:      {row['round']}",
                f"Round age:  {age}",
            ],
        )

    def _show_experiment_detail(self, index: int) -> None:
        if not 0 <= index < len(self.snapshot["experiments"]):
            return
        row = self.snapshot["experiments"][index]
        self._set_detail(
            "experiment",
            row["protocol_id"],
            [
                f"EXPERIMENT  {row['protocol_id'][:12]}",
                row["label"],
                "",
                f"State:       {row['state'].upper()}",
                f"Progress:    {row['percent']:g}%  {row['completed_units']}/{row['total_units']}",
                f"Current unit:{' ' + str(row['current_unit']) if row['current_unit'] else ' —'}",
                f"Attempt:     {row['attempt'] if row['attempt'] is not None else '—'}",
                f"Evidence:    {row['evidence_id'] or '—'}",
            ],
        )

    def _show_activity_detail(self, index: int) -> None:
        rows = self._visible_activity()
        if not 0 <= index < len(rows):
            return
        row = rows[index]
        key = f"{row['epoch']}|{row['role']}|{row['source']}|{row['message']}"
        self._set_detail(
            "activity",
            key,
            [
                f"ACTIVITY  {self._activity_time(str(row['at']))}",
                "",
                f"Role:    {row['role']}",
                f"Source:  {row['source']}",
                "",
                row["message"],
            ],
        )

    def _refresh_detail(self) -> None:
        if self._detail_kind == "plan":
            self._show_plan_detail(self._detail_key)
        elif self._detail_kind == "worker":
            for index, row in enumerate(self.snapshot["workers"]):
                if row["worker"] == self._detail_key:
                    self._show_worker_detail(index)
                    return
        elif self._detail_kind == "experiment":
            for index, row in enumerate(self.snapshot["experiments"]):
                if row["protocol_id"] == self._detail_key:
                    self._show_experiment_detail(index)
                    return
        elif self._detail_kind == "activity":
            for index, row in enumerate(self._visible_activity()):
                key = f"{row['epoch']}|{row['role']}|{row['source']}|{row['message']}"
                if key == self._detail_key:
                    self._show_activity_detail(index)
                    return

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

    def _set_activity_filter(self, role: str) -> None:
        self.activity_role = role
        table = self.query_one("#activity", DataTable)
        self._populate_activity(table)
        if self._visible_activity():
            self._show_activity_detail(min(table.cursor_row, len(self._visible_activity()) - 1))

    def action_filter_all(self) -> None:
        self._set_activity_filter("all")

    def action_filter_researcher(self) -> None:
        self._set_activity_filter("researcher")

    def action_filter_reviewer(self) -> None:
        self._set_activity_filter("reviewer")

    def action_filter_experimenter(self) -> None:
        self._set_activity_filter("experimenter")

    def action_filter_supervisor(self) -> None:
        self._set_activity_filter("supervisor")

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        tree = self.query_one("#plan", Tree)
        if tree.has_focus and event.node.data:
            self._show_plan_detail(str(event.node.data))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not event.data_table.has_focus:
            return
        table_id = event.data_table.id
        if table_id == "workers":
            self._show_worker_detail(event.cursor_row)
        elif table_id == "experiments":
            self._show_experiment_detail(event.cursor_row)
        elif table_id == "activity":
            self._show_activity_detail(event.cursor_row)

    def on_mount(self) -> None:
        self._show_plan_detail(str(self.snapshot["root_id"]))
        self.set_interval(2.0, self._refresh_snapshot)

    def _refresh_snapshot(self) -> None:
        old_nodes = self.snapshot["nodes"]
        self.snapshot = load_project_snapshot(self.project_name)
        self.query_one("#summary", Static).update(self._summary_text())
        self.query_one("#quota", Static).update(self._quota_text())
        if old_nodes != self.snapshot["nodes"]:
            self._populate_plan(self.query_one("#plan", Tree), reset=True)
        self._populate_workers(self.query_one("#workers", DataTable))
        self._populate_experiments(self.query_one("#experiments", DataTable))
        self._populate_activity(self.query_one("#activity", DataTable))
        self._refresh_detail()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._summary_text(), id="summary")
        with Horizontal(id="body"):
            yield self._plan_tree()
            with Vertical(id="right"):
                yield self._workers_table()
                yield self._detail_widget()
        yield self._experiments_table()
        yield self._activity_table()
        yield Static(self._quota_text(), id="quota")
        yield Footer()
