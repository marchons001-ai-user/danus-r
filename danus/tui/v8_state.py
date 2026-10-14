"""Read-only V8 adapter for the existing Textual TUI.

This translates V8's append-only events and Codex logs to the already used
display snapshot. No V7 Plan, worker, or Gateway state is touched.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any

from danus.v8 import Store, project_root


def _epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _tail_jsonl(path: Path, size=98304, limit=90) -> list[dict]:
    if not path.is_file():
        return []
    with path.open("rb") as stream:
        stream.seek(0, 2)
        length = stream.tell()
        stream.seek(max(0, length - size))
        data = stream.read().decode("utf-8", errors="replace")
    if length > size:
        data = data.partition("\n")[2]
    result = []
    for line in data.splitlines()[-limit:]:
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                result.append(item)
        except (ValueError, TypeError):
            continue
    return result


def _activity(rows: list[dict], at: str, role: str, source: str, message: str):
    rows.append({
        "at": at,
        "epoch": _epoch(at),
        "role": role,
        "source": source,
        "message": str(message)[:420],
    })


def _log_activity(project_dir: Path, tasks: dict, rows: list[dict]):
    logs = project_dir / "logs"
    if not logs.is_dir():
        return
    # Read only a small tail of recent logs, never full model contexts.
    for log in sorted(logs.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:8]:
        task_id = log.name.split("-", 2)
        is_supervisor = log.name.startswith("supervisor-")
        role = "supervisor" if is_supervisor else (
            tasks.get("-".join(task_id[:2]), {}).get("role", "system")
        )
        for entry in _tail_jsonl(log, limit=18):
            phase = str(entry.get("type") or "")
            item = entry.get("item") or {}
            if not isinstance(item, dict):
                item = {}
            item_kind = str(item.get("type") or "")
            if phase == "turn.started":
                description = "model turn started"
            elif phase == "turn.completed":
                usage = entry.get("usage") or {}
                description = ("model turn completed"
                               + (f"; output {usage.get('output_tokens')} tokens"
                                  if usage.get("output_tokens") is not None else ""))
            elif phase in ("item.started", "item.completed") and item_kind in (
                "command_execution", "mcp_tool_call", "tool_call", "agent_message"):
                if item_kind == "agent_message":
                    if phase != "item.completed":
                        continue
                    raw = str(item.get("text") or "")
                    description = "model reply: " + raw[:180]
                else:
                    description = (item_kind + " " + phase.split(".")[-1] + ": " +
                                   str(item.get("command") or item.get("name") or
                                       item.get("server") or "")[:170])
            else:
                continue
            modified = datetime.fromtimestamp(
                log.stat().st_mtime, tz=timezone.utc).isoformat()
            _activity(rows, modified, role, "MODEL", description)


def load_v8_project_snapshot(project: str) -> dict[str, Any]:
    root = project_root(project)
    store = Store(root)
    state = store.state()
    events = store.read()
    tasks = state["tasks"]
    running_at: dict[str, float] = {}
    for event in events:
        if event["kind"] == "RUNNING":
            running_at[event["task_id"]] = _epoch(event["at"])

    nodes = [{
        "node_id": "project", "title": "Research goal / adaptive tasks",
        "summary": state["goal"], "parent": None, "depends_on": [],
        "state": "DONE" if state["done"] else "OPEN",
    }]
    for task in tasks.values():
        status = task["status"]
        nodes.append({
            "node_id": task["id"],
            "title": f"{task['role']} · {task['purpose']}",
            "summary": task["instruction"] + ("\n\n" + task["summary"] if task["summary"] else ""),
            "parent": "project", "depends_on": [],
            "state": "DONE" if status == "COMPLETE" else
                     "OPEN" if status in {"READY", "RUNNING", "WAITING"} else status,
        })
    # Stable role rows preserve the existing Workers panel even when idle.
    workers = []
    for role in ("researcher", "reviewer", "experimenter"):
        own = [task for task in tasks.values() if task["role"] == role]
        current = own[-1] if own else None
        active = next((task for task in reversed(own)
                       if task["status"] in ("RUNNING", "READY", "WAITING")), None)
        current = active or current
        started = running_at.get(current["id"]) if current else None
        workers.append({
            "worker": role, "role": role,
            "runtime": ("working" if current and current["status"] == "RUNNING" else
                        "waiting" if current and current["status"] == "WAITING" else
                        "ready" if current and current["status"] == "READY" else "idle"),
            "node_id": current["id"] if current else None,
            "progress_state": current["status"] if current else None,
            "round": len(own),
            "age_s": max(0.0, time.time() - started) if started and
                     current["status"] == "RUNNING" else None,
        })

    experiments = []
    validations = state.get("validations", {})
    for task in tasks.values():
        if task["purpose"] != "experiment":
            continue
        proposal = state["proposals"].get(task["proposal_id"], {})
        status = task["status"]
        check = validations.get(task["id"])
        experiments.append({
            "protocol_id": task["id"], "label": "experiment / " +
                (check["status"] if check else "UNVERIFIED"),
            "state": status, "percent": 100 if status == "COMPLETE" else 0,
            "completed_units": 1 if status == "COMPLETE" else 0,
            "total_units": 1, "current_unit": task["id"],
            "attempt": None, "evidence_id": None,
            "proposal_status": proposal.get("status", ""),
        })

    activity = []
    for event in events[-110:]:
        kind = event["kind"]
        task = tasks.get(event.get("task_id") or event.get("from_task") or "", {})
        role = task.get("role", "supervisor" if kind in (
            "DECISION", "DENIED", "HALT", "UNHALT", "FINISH") else "system")
        message = {
            "INIT": lambda: "Project created with fixed research goal",
            "DECISION": lambda: f"{event.get('level')} {event.get('action')}: {event.get('reason', '')}",
            "TASK": lambda: f"{event['id']} assigned {event['role']} / {event['purpose']}",
            "RUNNING": lambda: f"{event['task_id']} model started",
            "REQUEST": lambda: f"{event['id']} asks {event['to']}: {event['question']}",
            "REPLY": lambda: f"{event['request_id']} reply: {event['message']}",
            "RESULT": lambda: f"{event['task_id']} {event['status']}: {event['summary']}",
            "ASSET": lambda: f"registered {event['path']}",
            "INPUT_STAGE": lambda: f"staged {len(event['files'])} immutable inputs "
                                   f"from {event['dataset_label']}",
            "VALIDATION": lambda: f"{event['task_id']} validation {event['status']}: "
                                  f"{event.get('reason') or 'approved checks executed'}",
            "DENIED": lambda: f"denied: {event.get('error', '')}",
            "HALT": lambda: f"halt: {event.get('reason', '')}",
            "UNHALT": lambda: f"unhalt: {event.get('reason', '')}",
            "FINISH": lambda: f"finished: {event.get('reason', '')}",
            "RESET": lambda: f"reset: {event['task_id']}",
        }.get(kind, lambda: kind)()
        _activity(activity, event["at"], role, kind, message)
    _log_activity(root, tasks, activity)
    activity.sort(key=lambda e: (e["epoch"], e["source"]), reverse=True)

    blockers = []
    if state["halt"]:
        blockers.append({
            "worker": "supervisor", "node_id": "project", "role": "supervisor",
            "assignment_id": "V8", "reason": state["halt"],
            "escalation_id": "", "escalation_target": "",
            "escalation_status": "",
            "request": "Inspect EVENTS.jsonl and latest model logs",
            "next_step": "Use V8 CLI to diagnose or explicitly unblock; not V7 controls",
        })
    # A blocked *last assigned* worker needs immediate attention. Older failed
    # attempts are history, not an active project halt (as in task-7/task-12).
    last_task = next(reversed(tasks.values())) if tasks else None
    latest_blocked = last_task if last_task and last_task["status"] == "BLOCKED" else None
    if latest_blocked:
        blockers.append({
            "worker": latest_blocked["role"], "node_id": latest_blocked["id"],
            "role": latest_blocked["role"], "assignment_id": latest_blocked["id"],
            "reason": latest_blocked["summary"], "escalation_id": "",
            "escalation_target": "", "escalation_status": "",
            "request": "The most recently assigned Worker failed; no accepted result",
            "next_step": "Inspect its saved Codex log and V8 task status. "
                         "Recover a valid completed result through V8 salvage "
                         "when supported; do not rerun the experiment blindly.",
        })
    historical_failures = sum(
        t["status"] == "BLOCKED" for t in tasks.values()
        if t is not latest_blocked
    )
    return {
        "project": project,
        "validation_status": (state.get("latest_validation") or {}).get("status", "NOT_RUN"),
        "input_count": len((state.get("inputs") or {}).get("files", [])),
        "failed_task_count": historical_failures,
        "project_state": "COMPLETE" if state["done"] else
                         "ACTION_REQUIRED" if state["halt"] else "RUNNING",
        "scientific_complete": state["done"],
        "pending_main_escalations": [],
        "plan_version": 8,
        "root_id": "project",
        "nodes": nodes,
        "workers": workers,
        "blockers": blockers,
        "experiments": experiments,
        "activity": activity[:120],
        "quota": {
            "state": "OBSERVE_ONLY",
            "reason": "V8 independent runner; model usage in logs",
            "weekly_remaining_percent": None,
        },
    }
