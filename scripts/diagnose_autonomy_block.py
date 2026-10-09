#!/usr/bin/env python3
"""Read-only, bounded diagnostic summary for a Danus-R autonomy pilot.

Usage: python scripts/diagnose_autonomy_block.py PROJECT
Does not issue, update, resume, or terminate any Assignment or Handoff.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.execution import layout as L


def _worker_log_events(logs: Path, cap: int = 25) -> dict:
    paths = sorted(logs.glob("round_*.log"), key=lambda p: (
        int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else -1
    ))
    if not paths:
        return {"last_log": None, "tools": [], "unanswered_calls": []}
    path = paths[-1]
    events = []
    open_calls = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            if not raw.startswith("{"):
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            etype = str(event.get("type") or "")
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "mcp_tool_call":
                continue
            item_id = str(item.get("id") or "")
            name = str(item.get("tool") or "")
            entry = {
                "event": etype,
                "id": item_id,
                "tool": name,
                "error": str(item.get("error") or "")[:400],
                "status": str(item.get("status") or ""),
            }
            events.append(entry)
            if etype == "item.started":
                open_calls[item_id] = entry
            elif etype in {"item.completed", "item.failed"}:
                open_calls.pop(item_id, None)
    return {
        "last_log": str(path),
        "tools": events[-cap:],
        "unanswered_calls": list(open_calls.values())[-cap:],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", help="Danus-R project name")
    args = parser.parse_args()
    project = L.project_dir(args.project)
    if not project.is_dir():
        parser.error("project does not exist")
    ledger = ProgressLedger(project)
    graph = ResearchPlanGraph(project).snapshot()
    handoffs = HandoffRequests(ledger).all()
    workers = {}
    for worker in L.list_workers(args.project):
        assignment = ledger.current(worker, refresh_stale=False)
        if assignment is None:
            workers[worker] = {"assignment": None}
            continue
        info = {
            key: assignment.get(key)
            for key in ("assignment_id", "node_id", "role", "state", "outcome", "result")
        }
        info["log"] = _worker_log_events(project / "workers" / worker / "logs")
        workers[worker] = info
    result = {
        "project": args.project,
        "plan_version": graph["plan_version"],
        "plan_nodes": {k: {"state": v.get("state"), "depends_on": v.get("depends_on")}
                       for k, v in graph["nodes"].items()},
        "workers": workers,
        "handoffs": [
            {k: item.get(k) for k in ("handoff_id", "kind", "subject_type", "subject_id",
                                      "status", "node_id", "reason")}
            for item in handoffs[-15:]
        ],
        "contracts": [
            {k: item.get(k) for k in ("contract_id", "node_id", "status", "gate_status",
                                      "proposer", "rationale")}
            for item in ledger.implementation_contracts()
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
