"""Progressively expanded project research-plan graph.

The plan is intentionally coarse: nodes carry only a short title/summary,
hierarchical containment, and explicit cross-node dependencies. Runtime progress,
agent ownership, experiment details, approvals, and revision history belong to
later control-plane layers rather than this graph.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


class ResearchPlanGraph:
    """One project-shared, single-root research plan stored in ``plan/current.json``."""

    SCHEMA_VERSION = 1
    MAX_DEPTH = 5
    MAX_TITLE_CHARS = 35
    MAX_SUMMARY_CHARS = 100
    _NODE_FIELDS = {"title", "summary", "parent", "depends_on"}

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.dir = self.root / "plan"
        self.path = self.dir / "current.json"

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        node_id: str,
        title: str,
        summary: str = "",
    ) -> "ResearchPlanGraph":
        graph = cls(root)
        if graph.path.exists():
            raise FileExistsError(f"research plan already exists: {graph.path}")
        nid = graph._clean_id(node_id)
        payload = {
            "schema_version": cls.SCHEMA_VERSION,
            "root_id": nid,
            "nodes": {
                nid: {
                    "title": graph._clean_text(title, "title", cls.MAX_TITLE_CHARS, required=True),
                    "summary": graph._clean_text(summary, "summary", cls.MAX_SUMMARY_CHARS),
                    "parent": None,
                    "depends_on": [],
                }
            },
        }
        graph._validate(payload)
        graph._write(payload)
        return graph

    def snapshot(self) -> Dict[str, Any]:
        """Return the current validated plan snapshot."""
        payload = self._read()
        self._validate(payload)
        return payload

    def add_node(
        self,
        *,
        node_id: str,
        title: str,
        parent: str,
        summary: str = "",
        depends_on: Optional[List[str]] = None,
    ) -> str:
        """Expand one existing node with a new child; no complete upfront plan is required."""
        payload = self.snapshot()
        nodes = payload["nodes"]
        nid = self._clean_id(node_id)
        parent_id = self._clean_id(parent)
        if nid in nodes:
            raise ValueError(f"duplicate node_id: {nid}")
        if parent_id not in nodes:
            raise ValueError(f"unknown parent: {parent_id}")
        nodes[nid] = {
            "title": self._clean_text(title, "title", self.MAX_TITLE_CHARS, required=True),
            "summary": self._clean_text(summary, "summary", self.MAX_SUMMARY_CHARS),
            "parent": parent_id,
            "depends_on": self._clean_dependencies(depends_on or []),
        }
        self._validate(payload)
        self._write(payload)
        return nid

    def set_dependencies(self, node_id: str, depends_on: List[str]) -> None:
        """Replace a node's explicit dependencies after later research reveals a relation."""
        payload = self.snapshot()
        nid = self._clean_id(node_id)
        if nid not in payload["nodes"]:
            raise ValueError(f"unknown node_id: {nid}")
        payload["nodes"][nid]["depends_on"] = self._clean_dependencies(depends_on)
        self._validate(payload)
        self._write(payload)

    @staticmethod
    def _clean_id(value: str) -> str:
        value = str(value).strip()
        if not value:
            raise ValueError("node_id is required")
        return value

    @staticmethod
    def _clean_text(value: str, field: str, limit: int, *, required: bool = False) -> str:
        text = str(value).strip()
        if required and not text:
            raise ValueError(f"{field} is required")
        if len(text) > limit:
            raise ValueError(f"{field} exceeds {limit} characters")
        return text

    def _clean_dependencies(self, values: List[str]) -> List[str]:
        out: List[str] = []
        seen = set()
        for value in values:
            dep = self._clean_id(value)
            if dep not in seen:
                seen.add(dep)
                out.append(dep)
        return out

    def _read(self) -> Dict[str, Any]:
        if not self.path.is_file():
            raise FileNotFoundError(f"research plan does not exist: {self.path}")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid research plan JSON: {self.path}") from exc
        if not isinstance(payload, dict):
            raise ValueError("research plan root must be an object")
        return payload

    def _write(self, payload: Dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def _validate(self, payload: Dict[str, Any]) -> None:
        if payload.get("schema_version") != self.SCHEMA_VERSION:
            raise ValueError("unsupported research plan schema_version")
        root_id = payload.get("root_id")
        nodes = payload.get("nodes")
        if not isinstance(root_id, str) or not root_id.strip():
            raise ValueError("research plan requires root_id")
        if not isinstance(nodes, dict) or root_id not in nodes:
            raise ValueError("research plan requires a valid root node")

        for nid, node in nodes.items():
            if not isinstance(nid, str) or not nid.strip() or not isinstance(node, dict):
                raise ValueError("invalid research plan node")
            if set(node) != self._NODE_FIELDS:
                raise ValueError(f"unsupported fields on node {nid}")
            self._clean_text(node.get("title", ""), "title", self.MAX_TITLE_CHARS, required=True)
            self._clean_text(node.get("summary", ""), "summary", self.MAX_SUMMARY_CHARS)
            parent = node.get("parent")
            if nid == root_id:
                if parent is not None:
                    raise ValueError("root node cannot have a parent")
            elif not isinstance(parent, str) or parent not in nodes:
                raise ValueError(f"unknown parent for node {nid}: {parent}")

            deps = node.get("depends_on")
            if not isinstance(deps, list):
                raise ValueError(f"depends_on must be a list for node {nid}")
            if len(deps) != len(set(deps)):
                raise ValueError(f"duplicate dependencies for node {nid}")
            for dep in deps:
                if not isinstance(dep, str) or dep not in nodes:
                    raise ValueError(f"unknown dependency for node {nid}: {dep}")
                if dep == nid:
                    raise ValueError(f"node cannot depend on itself: {nid}")

        self._validate_depths(nodes, root_id)
        self._validate_dependency_dag(nodes)

    def _validate_depths(self, nodes: Dict[str, Dict[str, Any]], root_id: str) -> None:
        for nid in nodes:
            depth = 1
            current = nid
            seen = set()
            while current != root_id:
                if current in seen:
                    raise ValueError(f"parent cycle at node {nid}")
                seen.add(current)
                parent = nodes[current]["parent"]
                if not isinstance(parent, str) or parent not in nodes:
                    raise ValueError(f"invalid parent chain for node {nid}")
                current = parent
                depth += 1
                if depth > self.MAX_DEPTH:
                    raise ValueError(f"node {nid} exceeds maximum depth {self.MAX_DEPTH}")

    @staticmethod
    def _validate_dependency_dag(nodes: Dict[str, Dict[str, Any]]) -> None:
        state: Dict[str, int] = {}

        def visit(nid: str) -> None:
            mark = state.get(nid, 0)
            if mark == 1:
                raise ValueError(f"dependency cycle at node {nid}")
            if mark == 2:
                return
            state[nid] = 1
            for dep in nodes[nid]["depends_on"]:
                visit(dep)
            state[nid] = 2

        for nid in nodes:
            visit(nid)
