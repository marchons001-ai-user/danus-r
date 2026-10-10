"""Read-only state aggregation for the Danus-R terminal UI."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.core.evidence import EvidenceStore
from danus.execution import layout as L
from danus.orchestration import cli as C
from danus.orchestration import quota as Q

_ACTIVITY_LIMIT = 80
_LOG_TAIL_BYTES = 256 * 1024
_TOOL_ITEM_TYPES = {"command_execution", "mcp_tool_call", "web_search"}


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _tail_lines(path: Path, max_bytes: int = _LOG_TAIL_BYTES) -> List[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            start = max(0, size - max_bytes)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start and lines:
        lines = lines[1:]
    return lines


def _tail_jsonl(path: Path, max_bytes: int = _LOG_TAIL_BYTES) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in _tail_lines(path, max_bytes=max_bytes):
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _epoch(value: Any, fallback: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _compact(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    text = text.replace("DANUS_OUTCOME: COMPLETE", "").replace(
        "DANUS_OUTCOME: BLOCKED", ""
    ).replace("DANUS_OUTCOME: NEEDS_REVIEW", "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _add_activity(
    rows: List[Dict[str, Any]],
    *,
    at: Any,
    role: str,
    source: str,
    message: str,
    fallback: float = 0.0,
) -> None:
    text = _compact(message)
    if not text:
        return
    stamp = _epoch(at, fallback)
    if stamp <= 0:
        return
    rows.append(
        {
            "at": at if isinstance(at, str) and at else _iso_from_epoch(stamp),
            "epoch": stamp,
            "role": str(role or "system").lower(),
            "source": source,
            "message": text,
        }
    )


def _pending_change(project_dir: Path, plan_version: int) -> bool:
    changes = project_dir / "plan" / "changes"
    if not changes.is_dir():
        return False
    for path in changes.glob("*.json"):
        record = _read_json(path)
        if (
            record is not None
            and record.get("status") in {"PROPOSED", "APPROVED"}
            and record.get("base_plan_version") == plan_version
        ):
            return True
    return False


def _effective_completed(plan: Dict[str, Any], completed: set[str]) -> set[str]:
    nodes = plan["nodes"]

    def active_route(node_id: str) -> bool:
        current = node_id
        seen: set[str] = set()
        while current:
            if current in seen:
                return False
            seen.add(current)
            node = nodes.get(current)
            if node is None or node.get("state") != "ACTIVE":
                return False
            current = node.get("parent")
        return True

    children = {node_id: [] for node_id in nodes}
    for node_id, node in nodes.items():
        parent = node.get("parent")
        if parent in children and active_route(node_id):
            children[parent].append(node_id)

    effective = set(completed)
    changed = True
    while changed:
        changed = False
        for node_id in list(effective):
            if (
                not active_route(node_id)
                or any(child not in effective for child in children[node_id])
            ):
                effective.remove(node_id)
                changed = True
    return effective


def _worker_row(project: str, worker: str, ledger: ProgressLedger) -> Dict[str, Any]:
    wl = L.WorkerLayout(L.worker_dir(project, worker))
    status = C._read_status(wl)
    progress = ledger.current(worker, refresh_stale=False)
    paused = C._pause_reason(wl)
    alive = C._worker_alive(wl)
    if paused is not None:
        runtime = "paused"
    elif alive:
        runtime = "working"
    else:
        runtime = str(status.get("state") or "stopped")
    round_started = status.get("round_started_at")
    age = (
        time.time() - round_started
        if alive and status.get("state") == "running" and isinstance(round_started, (int, float))
        else None
    )
    return {
        "worker": worker,
        "role": C._read_worker_role(wl),
        "runtime": runtime,
        "node_id": progress.get("node_id") if progress else None,
        "progress_state": progress.get("state") if progress else None,
        "round": int(status.get("round") or 0),
        "age_s": round(age, 1) if age is not None else None,
    }


def _blocker_rows(ledger: ProgressLedger) -> List[Dict[str, Any]]:
    """Read-only synthesis of blocked assignments and unresolved Main escalations."""
    escalations = [
        h for h in HandoffRequests(ledger).all()
        if h.get("kind") == "ESCALATE" and h.get("status") == "OPEN"
    ]
    blockers = []
    for assignment in ledger.assignments():
        if assignment.get("state") != "BLOCKED" or ledger._is_plan_stale(assignment):
            continue
        node_id = str(assignment.get("node_id") or "")
        assignment_id = str(assignment.get("assignment_id") or "")
        escalation = next((
            h for h in reversed(escalations)
            if h.get("subject_id") == assignment_id
            or (h.get("node_id") == node_id and h.get("target_role") == "main")
        ), None)
        reason = str(assignment.get("result") or "").strip()
        request = str(escalation.get("reason") or "").strip() if escalation else ""
        both = (reason + " " + request).lower()
        if "numpy" in both and ("autodiff" in both or "torch" in both):
            next_step = (
                "Provision a separate pinned numerical Python environment with the "
                "approved libraries; verify versions/resources and revalidate the "
                "contract before one governed retry. Do not mutate a shared Python "
                "venv backing frozen protocols."
            )
        else:
            next_step = (
                "Resolve the actual prerequisite through the authorized control "
                "path, then use governed recovery. Do not repeatedly dispatch an "
                "unchanged blocked task."
            )
        blockers.append({
            "node_id": node_id,
            "worker": str(assignment.get("worker") or ""),
            "role": str(assignment.get("role") or ""),
            "assignment_id": assignment_id,
            "reason": reason or "Blocked without a recorded result.",
            "escalation_id": str(escalation.get("handoff_id") or "") if escalation else "",
            "escalation_status": str(escalation.get("status") or "") if escalation else "",
            "escalation_target": str(escalation.get("target_role") or "") if escalation else "",
            "request": request,
            "next_step": next_step,
        })
    return blockers


def _protocol_total(protocol: Optional[Dict[str, Any]]) -> int:
    matrix = protocol.get("matrix") if protocol else None
    units = matrix.get("units") if isinstance(matrix, dict) else None
    return len(units) if isinstance(units, list) else 0


def _experiment_rows(project_dir: Path) -> List[Dict[str, Any]]:
    runtime_root = project_dir / "experiment_runtime"
    if not runtime_root.is_dir():
        return []
    store = EvidenceStore(project_dir)
    rows: List[Dict[str, Any]] = []
    for runtime_dir in sorted(path for path in runtime_root.iterdir() if path.is_dir()):
        status = _read_json(runtime_dir / "status.json")
        checkpoint = _read_json(runtime_dir / "checkpoint.json")
        if status is None and checkpoint is None:
            continue
        protocol_id = runtime_dir.name
        protocol = store.get_protocol(protocol_id)
        completed_map = checkpoint.get("completed") if checkpoint else None
        if isinstance(completed_map, dict):
            completed = len(completed_map)
        else:
            completed = int(status.get("completed_units") or 0) if status else 0
        total = int(status.get("total_units") or 0) if status else 0
        if total <= 0:
            total = _protocol_total(protocol)
        if total < completed:
            total = completed
        evidence_id = checkpoint.get("evidence_id") if checkpoint else None
        if status is not None:
            state = str(status.get("state") or "unknown")
        elif evidence_id:
            state = "complete"
        else:
            state = "checkpoint"
        percent = round(100.0 * completed / total, 1) if total else 0.0
        question = str(protocol.get("question") or "") if protocol else ""
        rows.append({
            "protocol_id": protocol_id,
            "label": question or protocol_id,
            "state": state,
            "completed_units": completed,
            "total_units": total,
            "percent": percent,
            "current_unit": status.get("current_unit") if status else None,
            "attempt": status.get("attempt") if status else None,
            "evidence_id": status.get("evidence_id") if status else evidence_id,
        })
    return rows


def _tool_label(item: Dict[str, Any]) -> str:
    kind = str(item.get("type") or "tool")
    if kind == "command_execution":
        return f"command {_compact(item.get('command'), 140)}".strip()
    if kind == "web_search":
        return f"web_search {_compact(item.get('query') or item.get('text'), 140)}".strip()
    server = str(item.get("server") or item.get("server_name") or "").strip()
    name = str(item.get("tool") or item.get("name") or "mcp_tool").strip()
    return f"{server + '.' if server else ''}{name}"


def _worker_activity(project: str, rows: List[Dict[str, Any]]) -> None:
    for worker in L.list_workers(project):
        wl = L.WorkerLayout(L.worker_dir(project, worker))
        role = C._read_worker_role(wl)
        if role not in L.SCIENTIFIC_ROLES or not wl.logs.is_dir():
            continue
        logs = list(wl.logs.glob("round_*.log"))
        if not logs:
            continue
        log_path = max(logs, key=_mtime)
        fallback = _mtime(log_path)
        lines = _tail_lines(log_path)
        structured = False
        emitted = False
        for line in lines:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                continue
            structured = True
            etype = event["type"]
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            event_at = event.get("timestamp") or event.get("at") or event.get("created_at")
            if etype == "item.completed" and item_type == "agent_message":
                _add_activity(
                    rows,
                    at=event_at,
                    fallback=fallback,
                    role=role,
                    source="MODEL",
                    message=f"{worker}: {item.get('text') or ''}",
                )
                emitted = True
            elif etype in {"item.started", "item.completed"} and item_type in _TOOL_ITEM_TYPES:
                phase = "start" if etype == "item.started" else "done"
                _add_activity(
                    rows,
                    at=event_at,
                    fallback=fallback,
                    role=role,
                    source="TOOL",
                    message=f"{worker}: {phase} {_tool_label(item)}",
                )
                emitted = True
        if structured or emitted:
            continue

        # DSH headless writes its public final answer to stdout rather than Codex
        # JSONL. Ignore wrapper/runtime diagnostics and expose only the plain tail.
        plain = [
            line.strip()
            for line in lines
            if line.strip()
            and not line.startswith("[codex-dsh]")
            and not line.startswith("[worker_loop]")
        ]
        if plain:
            _add_activity(
                rows,
                at=None,
                fallback=fallback,
                role=role,
                source="MODEL",
                message=f"{worker}: {' '.join(plain[-8:])}",
            )


def _supervisor_activity(project_dir: Path, rows: List[Dict[str, Any]]) -> None:
    for event in _tail_jsonl(project_dir / "supervisor" / "events.jsonl"):
        at = event.get("at")
        for item in list(event.get("applied") or []) + list(event.get("autonomy_applied") or []):
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "ACTION")
            node = str(item.get("node_id") or "").strip()
            worker = str(item.get("worker") or "").strip()
            result = str(item.get("result") or "").strip()
            subject = "/".join(value for value in (node, worker) if value)
            suffix = f" → {result}" if result else ""
            _add_activity(
                rows,
                at=at,
                role="supervisor",
                source="SUPERVISOR",
                message=f"{action}{' ' + subject if subject else ''}{suffix}",
            )


def _plan_activity(project_dir: Path, rows: List[Dict[str, Any]]) -> None:
    changes = project_dir / "plan" / "changes"
    if not changes.is_dir():
        return
    for path in changes.glob("*.json"):
        record = _read_json(path)
        if record is None:
            continue
        cid = str(record.get("change_id") or path.stem)[:8]
        _add_activity(
            rows,
            at=record.get("created_at"),
            role=str(record.get("proposer") or "researcher"),
            source="PLAN",
            message=f"proposed {cid}: {record.get('reason') or 'plan change'}",
        )
        if record.get("reviewed_at"):
            _add_activity(
                rows,
                at=record.get("reviewed_at"),
                role=str(record.get("reviewer") or "reviewer"),
                source="PLAN",
                message=(
                    f"{str(record.get('status') or 'REVIEWED').upper()} {cid}: "
                    f"{record.get('review_rationale') or ''}"
                ),
            )
        if record.get("committed_at"):
            _add_activity(
                rows,
                at=record.get("committed_at"),
                role="supervisor",
                source="PLAN",
                message=f"committed {cid} → Plan v{record.get('committed_version')}",
            )


def _evidence_activity(project_dir: Path, rows: List[Dict[str, Any]]) -> None:
    store = EvidenceStore(project_dir)
    if store.protocols_dir.is_dir():
        for path in store.protocols_dir.glob("*.json"):
            record = _read_json(path)
            if record is None:
                continue
            _add_activity(
                rows,
                at=record.get("frozen_at"),
                role=str(record.get("author") or "experimenter"),
                source="EVIDENCE",
                message=f"froze protocol {path.stem[:8]}: {record.get('question') or ''}",
            )
    if store.records_dir.is_dir():
        for path in store.records_dir.glob("*.json"):
            record = _read_json(path)
            if record is None:
                continue
            _add_activity(
                rows,
                at=record.get("created_at"),
                role=str(record.get("author") or "experimenter"),
                source="EVIDENCE",
                message=(
                    f"recorded evidence {path.stem[:8]} for protocol "
                    f"{str(record.get('protocol_id') or '')[:8]}"
                ),
            )


def _experiment_activity(project_dir: Path, rows: List[Dict[str, Any]]) -> None:
    root = project_dir / "experiment_runtime"
    if not root.is_dir():
        return
    for runtime_dir in (path for path in root.iterdir() if path.is_dir()):
        events = _tail_jsonl(runtime_dir / "events.jsonl")
        if events:
            for event in events:
                kind = str(event.get("event") or "EXPERIMENT")
                unit = str(event.get("unit_id") or "").strip()
                attempt = event.get("attempt")
                progress = f"{event.get('completed_units', 0)}/{event.get('total_units', 0)}"
                if kind == "UNIT_STARTED":
                    text = f"{runtime_dir.name[:8]} {unit} started · attempt {attempt} · {progress}"
                elif kind == "UNIT_COMPLETED":
                    text = f"{runtime_dir.name[:8]} {unit} completed · {progress}"
                elif kind == "UNIT_FAILED":
                    text = (
                        f"{runtime_dir.name[:8]} {unit} failed rc={event.get('returncode')} "
                        f"· attempt {attempt}"
                    )
                elif kind == "COMPLETE":
                    text = (
                        f"{runtime_dir.name[:8]} complete · {progress} · evidence "
                        f"{str(event.get('evidence_id') or '')[:8]}"
                    )
                else:
                    text = f"{runtime_dir.name[:8]} {kind.lower()} · {progress}"
                _add_activity(
                    rows,
                    at=event.get("at"),
                    role="experimenter",
                    source="EXPERIMENT",
                    message=text,
                )
            continue

        status = _read_json(runtime_dir / "status.json")
        if status is not None:
            current = str(status.get("current_unit") or "").strip()
            suffix = f" · {current}" if current else ""
            _add_activity(
                rows,
                at=status.get("updated_at"),
                role="experimenter",
                source="EXPERIMENT",
                message=(
                    f"{runtime_dir.name[:8]} {str(status.get('state') or 'unknown').lower()} "
                    f"· {status.get('completed_units', 0)}/{status.get('total_units', 0)}{suffix}"
                ),
            )


def _activity_rows(project: str, project_dir: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    _worker_activity(project, rows)
    _supervisor_activity(project_dir, rows)
    _plan_activity(project_dir, rows)
    _evidence_activity(project_dir, rows)
    _experiment_activity(project_dir, rows)
    rows.sort(key=lambda row: (row["epoch"], row["source"], row["message"]), reverse=True)
    return rows[:_ACTIVITY_LIMIT]


def load_project_snapshot(project: str) -> Dict[str, Any]:
    """Read one display snapshot without running Supervisor or quota refresh."""
    project_dir = L.project_dir(project)
    plan = ResearchPlanGraph(project_dir).snapshot()
    ledger = ProgressLedger(project_dir)
    completed = _effective_completed(plan, set(ledger.completed_nodes()))
    pending = _pending_change(project_dir, int(plan["plan_version"]))
    if plan["root_id"] in completed and not pending:
        project_state = "COMPLETE"
    elif (project_dir / L.PAUSE_FILE).is_file():
        project_state = "PAUSED"
    else:
        project_state = "RUNNING"

    nodes: List[Dict[str, Any]] = []
    for node_id, node in plan["nodes"].items():
        plan_state = str(node.get("state") or "ACTIVE")
        nodes.append(
            {
                "node_id": node_id,
                "title": str(node.get("title") or node_id),
                "summary": str(node.get("summary") or ""),
                "parent": node.get("parent"),
                "depends_on": list(node.get("depends_on") or []),
                "state": "DONE" if node_id in completed else ("OPEN" if plan_state == "ACTIVE" else plan_state),
            }
        )

    workers = [
        _worker_row(project, worker, ledger)
        for worker in L.list_workers(project)
    ]
    # Official usage is display-only and never an execution gate.
    observed = Q.load_quota(project_dir) or {}
    weekly = (observed.get("windows") or {}).get("weekly") or {}
    quota = {
        "state": "OBSERVE_ONLY" if observed.get("available") else "UNAVAILABLE",
        "reason": "official_telemetry_only" if observed.get("available") else "telemetry_unavailable",
        "weekly_remaining_percent": weekly.get("remaining_percent"),
        "observed_at": observed.get("observed_at"),
    }
    return {
        "project": project,
        "project_state": project_state,
        "plan_version": int(plan["plan_version"]),
        "root_id": plan["root_id"],
        "nodes": nodes,
        "workers": workers,
        "blockers": _blocker_rows(ledger),
        "experiments": _experiment_rows(project_dir),
        "activity": _activity_rows(project, project_dir),
        "quota": quota,
    }