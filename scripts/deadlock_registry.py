#!/usr/bin/env python3
"""Summarize durable supervisor deadlock episodes across all local Danus-R projects.

Read-only. Usage: python scripts/deadlock_registry.py [--json]
"""
from __future__ import annotations

import argparse
import json

from danus.core._util import read_jsonl
from danus.execution import layout as L


def collect():
    result = []
    for project in L.list_projects():
        path = L.project_dir(project) / "supervisor" / "deadlocks.jsonl"
        if not path.is_file():
            continue
        by_id = {}
        for event in read_jsonl(path):
            incident_id = str(event.get("deadlock_id") or "")
            if not incident_id:
                continue
            record = by_id.setdefault(
                incident_id, {"project": project, "deadlock_id": incident_id,
                              "state": "OPEN", "action": "", "result": ""},
            )
            if event.get("event") == "DEADLOCK_OPEN":
                record["opened_at"] = event.get("at")
                record["plan_version"] = event.get("snapshot", {}).get("plan_version")
                record["frontier"] = event.get("snapshot", {}).get("frontier", [])
                record["capture"] = event.get("capture")
            elif event.get("event") == "DEADLOCK_SUGGESTION":
                record["action"] = event.get("action", "")
                record["rationale"] = event.get("rationale", "")
            elif event.get("event") == "DEADLOCK_ACTION":
                record["result"] = event.get("result", "")
                record["error"] = event.get("error", "")
            elif event.get("event") == "DEADLOCK_CLOSED":
                record["state"] = "CLOSED"
                record["closed_at"] = event.get("at")
                record["close_reason"] = event.get("reason")
        result.extend(by_id.values())
    return sorted(result, key=lambda e: (e.get("opened_at", ""), e["project"]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    incidents = collect()
    if args.json:
        print(json.dumps(incidents, ensure_ascii=False, indent=2))
        return
    if not incidents:
        print("No recorded global-idle deadlocks.")
        return
    for e in incidents:
        print(
            f"{e['opened_at']} {e['project']} {e['deadlock_id']} "
            f"{e['state']} plan=v{e.get('plan_version')} "
            f"frontier={','.join(e.get('frontier', []))} "
            f"action={e.get('action') or '-'} result={e.get('result') or '-'}"
        )


if __name__ == "__main__":
    main()
