"""``danus`` — the main agent's control surface over codex workers.

    danus list   [--json]
    danus new    <project> [--roles high:3,xhigh:4] [--model M]
    danus assign <project>/<worker> (--task "…" | --file P | --stdin)
    danus finalize <project> [--paper <paper_id>] [<fact_id> ...]
    danus start  <project>[/<worker>]
    danus status <project>[/<worker>] [--json]
    danus supervise <project> [--complete-node N | --reopen-node N] [--apply | --watch]
    danus pause  <project>[/<worker>]
    danus resume <project>[/<worker>]
    danus stop   <project>[/<worker>] [--force]

This module is the verbs/UX only. The worker outer loop, the on-disk layout, and
the scaffolding they drive live in ``danus.execution`` (imported here as a
library). Reads/writes only files under the project dir — the loop is autonomous;
this CLI just assigns / starts / monitors / stops it.

Notes:
  - the layout + scaffolding + config template are imported from ``danus.execution``
    (no duplicated layout / config template);
  - the verbs are mode-agnostic and identical across deployments.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from danus.core import EvidenceStore, ProgressLedger, ResearchPlanGraph
from danus.core._util import append_jsonl, utc_now
from danus.execution import layout as L
from danus.execution.scaffold import atomic_write, do_new, spawn_loop

from . import quota as Q
from .control import quota_policy, reconcile, reviewer_cost_policy

_QUOTA_REFRESH_SECONDS = 5 * 60
_REVIEWER_TERRA_MODEL = "gpt-5.6-terra"
_REVIEWER_DSH_DEFAULT_MODEL = "deepseek-v4-flash"
_TOKEN_USAGE_SEMANTICS = "completed_turn_usage_not_context_window"

__all__ = [
    "do_new", "do_assign", "do_start", "do_status", "worker_status",
    "do_supervise", "watch_supervisor", "do_pause", "do_resume",
    "do_list", "do_stop", "do_finalize", "build_parser", "main",
]


# --------------------------------------------------------------------------- #
# read helpers                                                                 #
# --------------------------------------------------------------------------- #

def _read_pid(wl: L.WorkerLayout) -> Optional[int]:
    pf = wl.pid
    if not pf.exists():
        return None
    try:
        return int(pf.read_text().strip())
    except (ValueError, OSError):
        return None


def _alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours
    # The pid exists — but a zombie (killed, not yet reaped by its parent) is
    # effectively dead. Linux /proc tells us the process state.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        state = stat.rsplit(")", 1)[1].split()[0]  # field after "(comm)"
        return state != "Z"
    except (OSError, IndexError):
        return True


def _read_status(wl: L.WorkerLayout) -> Dict:
    sp = wl.status
    if not sp.exists():
        return {}
    try:
        return json.loads(sp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _role_value(wl: L.WorkerLayout, key: str) -> str:
    if not wl.role.is_file():
        return ""
    try:
        for line in wl.role.read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == key:
                return value.strip()
    except OSError:
        pass
    return ""


def _read_worker_role(wl: L.WorkerLayout) -> str:
    return (_role_value(wl, "ROLE") or "worker").lower()


def _auto_reviewer(wl: L.WorkerLayout) -> bool:
    return _read_worker_role(wl) == "reviewer" and _role_value(wl, "BACKEND").lower() == "auto"


def _selected_worker_backend(wl: L.WorkerLayout) -> str:
    backend = _role_value(wl, "BACKEND").lower()
    if backend == "dsh":
        return "dsh"
    if backend == "auto" and _read_worker_role(wl) == "reviewer":
        selected = str(_read_status(wl).get("reviewer_backend_selected") or "dsh").lower()
        return selected if selected in {"chatgpt", "dsh"} else "dsh"
    return "chatgpt"


def _uses_openai_codex(wl: L.WorkerLayout) -> bool:
    """Quota pressure never gates a worker currently bound to DSH."""
    return _selected_worker_backend(wl) != "dsh"


def _dsh_available() -> bool:
    path = os.environ.get("DANUS_DSH_BIN")
    return bool(path and Path(path).is_file())


def _current_assignment_id(wl: L.WorkerLayout) -> Optional[str]:
    path = wl.project_dir / "progress" / "ledger.jsonl"
    if not path.is_file():
        return None
    try:
        current = ProgressLedger(wl.project_dir).current(wl.name, refresh_stale=True)
    except (FileNotFoundError, ValueError):
        return None
    value = current.get("assignment_id") if current else None
    return str(value) if value else None


def _dsh_quota_for_assignment(wl: L.WorkerLayout, assignment_id: Optional[str]) -> bool:
    if not assignment_id:
        return False
    status = _read_status(wl)
    return bool(status.get("reviewer_dsh_quota_exhausted")) and str(
        status.get("reviewer_dsh_quota_assignment_id") or ""
    ) == assignment_id


def _reviewer_model(wl: L.WorkerLayout, backend: str) -> str:
    if backend == "chatgpt":
        return _REVIEWER_TERRA_MODEL
    model = _role_value(wl, "MODEL")
    if model.lower().startswith(("deepseek", "ds-", "dsh-")):
        return model
    return _REVIEWER_DSH_DEFAULT_MODEL


def _persist_reviewer_backend(
    wl: L.WorkerLayout,
    decision: Dict[str, Any],
    *,
    assignment_id: Optional[str],
) -> None:
    status = _read_status(wl)
    backend = decision["selected_backend"]
    status.update({
        "reviewer_backend_selected": backend,
        "reviewer_model_selected": _reviewer_model(wl, backend),
        "reviewer_backend_reason": decision["reason"],
        "reviewer_backend_policy": decision["policy"],
        "reviewer_backend_quota_state": decision["quota_state"],
        "reviewer_backend_selected_at": utc_now(),
    })
    if backend == "dsh":
        status.pop("reviewer_dsh_quota_exhausted", None)
        status.pop("reviewer_dsh_quota_assignment_id", None)
    atomic_write(wl.status, json.dumps(status, ensure_ascii=False, indent=2) + "\n")


def _reviewer_cost_rows(
    project: str,
    rows: List[Dict[str, Any]],
    quota: Dict[str, Any],
    *,
    apply: bool,
) -> List[Dict[str, Any]]:
    available = _dsh_available()
    out: List[Dict[str, Any]] = []
    for row in rows:
        wl = L.WorkerLayout(L.worker_dir(project, row["worker"]))
        if not _auto_reviewer(wl):
            continue
        if row.get("progress_state") in {"DONE", "BLOCKED", "STALE"}:
            continue
        assignment_id = str(row.get("assignment_id") or "") or None
        if not assignment_id:
            continue
        exhausted = _dsh_quota_for_assignment(wl, assignment_id)
        desired = reviewer_cost_policy(
            quota,
            dsh_available=available,
            dsh_quota_exhausted=exhausted,
        )
        desired_model = _reviewer_model(wl, desired["selected_backend"])
        current = _selected_worker_backend(wl)
        current_model = str(
            _read_status(wl).get("reviewer_model_selected") or _reviewer_model(wl, current)
        )
        selected = current if row.get("alive") else desired["selected_backend"]
        selected_model = current_model if row.get("alive") else desired_model
        if row.get("alive"):
            switching = current != desired["selected_backend"] or current_model != desired_model
            result = "switch-planned" if switching else "active-unchanged"
            if apply and switching:
                wl.stop.touch()
                result = "switch-at-round-boundary"
        else:
            result = "planned"
            if apply:
                _persist_reviewer_backend(wl, desired, assignment_id=assignment_id)
                result = "selected"
        out.append({
            "worker": row["worker"],
            **desired,
            "selected_backend": selected,
            "selected_model": selected_model,
            "desired_backend": desired["selected_backend"],
            "desired_model": desired_model,
            "assignment_id": assignment_id,
            "result": result,
        })
    return out


def _prepare_reviewer_backend(wl: L.WorkerLayout) -> None:
    if not _auto_reviewer(wl):
        return
    assignment_id = _current_assignment_id(wl)
    decision = reviewer_cost_policy(
        quota_policy(Q.load_quota(wl.project_dir)),
        dsh_available=_dsh_available(),
        dsh_quota_exhausted=_dsh_quota_for_assignment(wl, assignment_id),
    )
    _persist_reviewer_backend(wl, decision, assignment_id=assignment_id)


def _pause_reason(wl: L.WorkerLayout) -> Optional[str]:
    if not wl.pause.is_file():
        return None
    try:
        return wl.pause.read_text(encoding="utf-8").strip() or "manual"
    except OSError:
        return "manual"


# --------------------------------------------------------------------------- #
# assign                                                                       #
# --------------------------------------------------------------------------- #

def do_assign(target: str, task: str, node_id: Optional[str] = None) -> Dict:
    """Replace one worker TASK.md and, for planned projects, bind it to Plan."""
    project, worker = L.resolve_target(target)
    if not worker:
        raise SystemExit("assign needs a specific worker: <project>/<worker>")
    wl = L.WorkerLayout(L.worker_dir(project, worker))
    if not wl.dir.is_dir():
        raise SystemExit(f"no such worker: {project}/{worker}")
    if not task.strip():
        raise SystemExit("refusing to assign an empty task")

    text = task if task.endswith("\n") else task + "\n"
    pdir = wl.project_dir
    planned = (pdir / "plan" / "current.json").is_file()
    if not planned:
        if node_id:
            raise SystemExit("cannot use --node: project has no Research Plan")
        atomic_write(wl.task, text)
        return {"worker": f"{project}/{worker}", "task_file": str(wl.task)}
    if not node_id:
        raise SystemExit("assign on a planned project requires --node <plan_node_id>")

    try:
        plan_version = ResearchPlanGraph(pdir).snapshot()["plan_version"]
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(f"cannot read Research Plan: {exc}") from exc

    old_task = None
    if wl.task.exists():
        try:
            old_task = wl.task.read_text(encoding="utf-8")
        except OSError:
            old_task = None
    atomic_write(wl.task, text)
    try:
        assignment = ProgressLedger(pdir).assign(
            worker=worker,
            role=_read_worker_role(wl),
            node_id=node_id,
            plan_version=plan_version,
            task=text,
        )
    except ValueError as exc:
        if old_task is None:
            wl.task.unlink(missing_ok=True)
        else:
            atomic_write(wl.task, old_task)
        raise SystemExit(f"cannot bind assignment: {exc}") from exc

    return {
        "worker": f"{project}/{worker}",
        "task_file": str(wl.task),
        "assignment_id": assignment["assignment_id"],
        "node_id": assignment["node_id"],
        "plan_version": assignment["plan_version"],
    }


# --------------------------------------------------------------------------- #
# finalize                                                                     #
# --------------------------------------------------------------------------- #

def do_finalize(project: str, fact_ids: List[str],
                paper_id: Optional[str] = None) -> Dict:
    """Record the finalized target theorem(s) for a PAPER of a project in that
    paper's TARGET.md — the durable slot write-paper reads (never a guess). The
    default paper writes the LEGACY ``<project>/TARGET.md``; a non-default
    ``paper_id`` writes ``<project>/papers/<paper_id>/TARGET.md`` (its own
    workspace). One fact graph per project; per-paper targets.

    Resolves the project dir, VALIDATES every ``fact_id`` against that project's
    fact graph (refuses an id the graph does not have — you cannot record a
    phantom target), then writes the ids to the paper's TARGET.md.

    With NO ``fact_ids`` (suggestion mode): prints the candidate terminal facts
    (facts that are no other fact's predecessor — the ``assemble._terminal_facts``
    helper) as SUGGESTIONS and writes NOTHING (returns ``{"suggested": [...]}``).

    Rejections raise ``SystemExit`` (nonzero exit) with a clear message."""
    from danus.core import FactGraph
    from danus.write_paper import assemble

    pdir = L.project_dir(project)
    if not pdir.is_dir():
        raise SystemExit(f"no such project: {project}")
    fg = FactGraph(pdir)

    if not fact_ids:
        return {"project": project, "paper_id": paper_id,
                "suggested": assemble._terminal_facts(fg)}

    unknown = [fid for fid in fact_ids if not fg.exists(fid)]
    if unknown:
        raise SystemExit(
            f"cannot finalize: unknown fact id(s) in {project}: {', '.join(unknown)} "
            f"(a target must be a verified fact in the project's graph)"
        )
    try:
        if not assemble._is_default_paper(paper_id):
            assemble._validate_paper_id(paper_id)  # type: ignore[arg-type]
    except ValueError as e:
        raise SystemExit(f"cannot finalize: {e}")
    seen: set = set()
    ids: List[str] = []
    for fid in fact_ids:
        if fid not in seen:
            seen.add(fid)
            ids.append(fid)
    path = assemble.write_target_fact_ids(pdir, ids, paper_id)
    return {"project": project, "paper_id": paper_id,
            "target_file": str(path), "target_fact_ids": ids}


# --------------------------------------------------------------------------- #
# start                                                                        #
# --------------------------------------------------------------------------- #

def _start_one(wl: L.WorkerLayout) -> str:
    """Returns 'started' / 'already-running' / 'locked' / 'paused'. Idempotent via
    an flock on .pid.lock; clears a stale .stop before spawning."""
    wl.dir.mkdir(parents=True, exist_ok=True)
    wl.logs.mkdir(exist_ok=True)
    if wl.pause.exists():
        return "paused"
    lock = open(wl.lock, "w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "locked"
        if _alive(_read_pid(wl)):
            return "already-running"
        _prepare_reviewer_backend(wl)
        wl.stop.unlink(missing_ok=True)
        pid = spawn_loop(wl.dir)
        atomic_write(wl.pid, str(pid))
        return "started"
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def do_start(target: str, stagger: float = 0.2) -> List[Dict]:
    dirs = L.target_worker_dirs(target)
    if not dirs:
        raise SystemExit(f"no workers for target {target!r}")
    out = []
    for i, wdir in enumerate(dirs):
        if i and stagger:
            time.sleep(stagger)
        out.append({"worker": wdir.name, "result": _start_one(L.WorkerLayout(wdir))})
    return out


# --------------------------------------------------------------------------- #
# status                                                                       #
# --------------------------------------------------------------------------- #

def worker_status(wl: L.WorkerLayout) -> Dict:
    pid = _read_pid(wl)
    alive = _alive(pid)
    st = _read_status(wl)
    state = st.get("state", "—")
    now = time.time()
    last = st.get("last_round_at") or st.get("round_started_at") or st.get("updated_at")
    age = (now - last) if isinstance(last, (int, float)) else None
    pause_reason = _pause_reason(wl)

    if pause_reason is not None:
        label = "paused"
    elif alive:
        rs = st.get("round_started_at")
        hard = int(os.environ.get("DANUS_ROUND_HARD_TIMEOUT", "14400"))
        if state == "running" and isinstance(rs, (int, float)) and (now - rs) > hard * 1.5:
            label = "stuck?"
        else:
            label = "working"
    else:
        label = state if state in ("stopped", "deadline", "max_rounds", "error",
                                   "terminated", "created") else "dead"

    progress = None
    progress_error = None
    ledger_path = wl.project_dir / "progress" / "ledger.jsonl"
    if ledger_path.is_file():
        try:
            progress = ProgressLedger(wl.project_dir).current(wl.name, refresh_stale=True)
        except (FileNotFoundError, ValueError) as exc:
            progress_error = str(exc)

    # Codex exposes completed-turn usage; DSH headless currently does not expose a
    # stable usage stream. An auto Reviewer may also have mixed DSH/Terra history,
    # so its cumulative total is conservatively incomplete after it has run.
    backend = _selected_worker_backend(wl)
    round_no = int(st.get("round") or 0)
    usage_available = backend != "dsh" and any(
        key in st for key in ("context_tokens", "context_tokens_total", "context_tool_calls")
    )
    usage_complete = round_no == 0 or (
        backend != "dsh" and not _auto_reviewer(wl) and "context_tokens_total" in st
    )
    round_usage_tokens = (
        int(st.get("context_tokens") or 0)
        if backend != "dsh" and "context_tokens" in st
        else None
    )
    usage_tokens_total = (
        0 if round_no == 0
        else int(st.get("context_tokens_total") or 0) if usage_complete
        else None
    )
    usage_tool_calls = (
        int(st.get("context_tool_calls") or 0)
        if backend != "dsh" and "context_tool_calls" in st
        else None
    )
    usage_compactions = (
        int(st.get("context_compactions") or 0)
        if backend != "dsh" and "context_compactions" in st
        else None
    )

    return {
        "worker": wl.name, "pid": pid, "alive": alive, "state": state,
        "round": st.get("round", 0), "age_s": round(age, 1) if age is not None else None,
        "last_fact_id": st.get("last_fact_id"), "label": label,
        "paused": pause_reason is not None, "pause_reason": pause_reason,
        "assignment_id": progress.get("assignment_id") if progress else None,
        "plan_node_id": progress.get("node_id") if progress else None,
        "plan_version": progress.get("plan_version") if progress else None,
        "progress_state": progress.get("state") if progress else None,
        "progress_outcome": progress.get("outcome") if progress else None,
        "round_usage_tokens": round_usage_tokens,
        "usage_tokens_total": usage_tokens_total,
        "usage_tool_calls": usage_tool_calls,
        "usage_compactions": usage_compactions,
        "usage_available": usage_available,
        "usage_complete": usage_complete,
        "token_semantics": _TOKEN_USAGE_SEMANTICS,
        # Compatibility aliases. They intentionally become null when usage is
        # unknown rather than claiming that DSH used zero tokens/tool calls.
        "context_tokens": round_usage_tokens,
        "context_tokens_total": usage_tokens_total,
        "context_tool_calls": usage_tool_calls,
        "context_compactions": usage_compactions,
        "context_trigger": st.get("context_trigger"),
        "terminal_outcome": st.get("terminal_outcome"),
        "reviewer_backend_selected": st.get("reviewer_backend_selected"),
        "reviewer_model_selected": st.get("reviewer_model_selected"),
        "reviewer_backend_reason": st.get("reviewer_backend_reason"),
        "reviewer_dsh_quota_exhausted": st.get("reviewer_dsh_quota_exhausted"),
        "reviewer_dsh_quota_assignment_id": st.get("reviewer_dsh_quota_assignment_id"),
        "progress_error": progress_error,
    }


def do_status(target: str) -> List[Dict]:
    dirs = L.target_worker_dirs(target)
    if not dirs:
        raise SystemExit(f"no workers for target {target!r}")
    return [worker_status(L.WorkerLayout(d)) for d in dirs]


def _project_rows(project: str) -> List[Dict[str, Any]]:
    return [
        worker_status(L.WorkerLayout(L.worker_dir(project, worker)))
        for worker in L.list_workers(project)
    ]


def _resource_snapshot(pdir: Path, rows: List[Dict[str, Any]], token_budget: int) -> Dict[str, Any]:
    known_totals = [
        max(0, int(row["usage_tokens_total"]))
        for row in rows
        if row.get("usage_complete") and row.get("usage_tokens_total") is not None
    ]
    used_tokens = sum(known_totals)
    unknown_workers = sorted(
        str(row.get("worker") or "")
        for row in rows
        if not row.get("usage_complete")
    )
    limit = max(0, int(token_budget))
    if limit and used_tokens >= limit:
        token_exceeded: Optional[bool] = True
    elif limit and unknown_workers:
        token_exceeded = None
    else:
        token_exceeded = False

    deadline = None
    deadline_path = pdir / L.DEADLINE_FILE
    try:
        deadline = float(deadline_path.read_text().strip()) if deadline_path.is_file() else None
    except (OSError, ValueError):
        deadline = None
    now = time.time()

    experiments = []
    store = EvidenceStore(pdir)
    runtime_dir = pdir / "experiment_runtime"
    if runtime_dir.is_dir():
        for path in sorted(runtime_dir.glob("*/checkpoint.json")):
            try:
                checkpoint = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(checkpoint, dict):
                continue
            protocol_id = str(checkpoint.get("protocol_id") or path.parent.name)
            protocol = store.get_protocol(protocol_id) or {}
            matrix = protocol.get("matrix") if isinstance(protocol, dict) else None
            units = matrix.get("units") if isinstance(matrix, dict) else None
            planned = len(units) if isinstance(units, list) else None
            completed = checkpoint.get("completed")
            failures = checkpoint.get("failures")
            experiments.append({
                "protocol_id": protocol_id,
                "planned_units": planned,
                "completed_units": len(completed) if isinstance(completed, dict) else 0,
                "failures": len(failures) if isinstance(failures, list) else 0,
                "evidence_id": checkpoint.get("evidence_id"),
            })

    tokens = {"used": used_tokens, "limit": limit, "exceeded": token_exceeded}
    if unknown_workers:
        tokens.update({
            "complete": False,
            "used_is_lower_bound": True,
            "unknown_workers": unknown_workers,
            "semantics": _TOKEN_USAGE_SEMANTICS,
        })

    return {
        "tokens": tokens,
        "time": {
            "deadline": deadline,
            "remaining_s": None if deadline is None else round(deadline - now, 1),
            "exceeded": bool(deadline is not None and now >= deadline),
        },
        "context": [
            {
                "worker": row["worker"],
                "round_usage_tokens": row.get("round_usage_tokens"),
                "usage_tokens_total": row.get("usage_tokens_total"),
                "usage_available": row.get("usage_available", False),
                "usage_complete": row.get("usage_complete", False),
                "token_semantics": row.get("token_semantics", _TOKEN_USAGE_SEMANTICS),
                "tool_calls": row.get("usage_tool_calls"),
                "compactions": row.get("usage_compactions"),
                "rollover": row.get("context_trigger"),
                # Compatibility aliases for existing consumers.
                "tokens": row.get("round_usage_tokens"),
                "tokens_total": row.get("usage_tokens_total"),
            }
            for row in rows
        ],
        "experiments": experiments,
    }


def _apply_resource_budget(report: Dict[str, Any], rows: List[Dict[str, Any]], budget: Dict[str, Any]) -> None:
    reason = None
    if budget["time"]["exceeded"]:
        reason = "resource_time_budget"
    elif budget["tokens"]["exceeded"] is True:
        reason = "resource_token_budget"
    if not reason:
        return

    report["decisions"] = [
        {
            "action": "STOP",
            "worker": row["worker"],
            "node_id": row.get("plan_node_id") or "",
            "assignment_id": row.get("assignment_id"),
            "reason": reason,
        }
        for row in rows
        if row.get("alive") or row.get("progress_state") in {"ASSIGNED", "RUNNING", "BLOCKED"}
    ]
    report["budget_stop"] = reason


def _apply_quota_gate(project: str, rows: List[Dict[str, Any]], policy: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Persist/clear only quota-owned pauses; active rounds are never signalled."""
    block = bool(policy.get("block_new_codex_rounds"))
    state = str(policy.get("state") or "UNKNOWN").lower()
    reason = str(policy.get("reason") or "quota")
    applied: List[Dict[str, Any]] = []

    for row in rows:
        wl = L.WorkerLayout(L.worker_dir(project, row["worker"]))
        pause_reason = _pause_reason(wl)
        quota_owned = bool(pause_reason and pause_reason.startswith("quota:"))

        if block:
            if not _uses_openai_codex(wl):
                if quota_owned:
                    wl.pause.unlink(missing_ok=True)
                    applied.append({"worker": row["worker"], "result": "quota-cleared-non-codex"})
                continue
            if pause_reason and not quota_owned:
                continue
            if not quota_owned:
                atomic_write(wl.pause, f"quota:{state}:{reason}\n")
            # .stop is already the worker loop's safe round boundary: it is checked
            # before the next model round. Do not SIGTERM an active round for quota.
            if row.get("alive"):
                wl.stop.touch()
            applied.append({"worker": row["worker"], "result": "pause-at-round-boundary"})
            continue

        if quota_owned:
            wl.pause.unlink(missing_ok=True)
            result = "quota-cleared"
            if not row.get("alive") and row.get("progress_state") in {"ASSIGNED", "RUNNING"}:
                result = _start_one(wl)
            applied.append({"worker": row["worker"], "result": result})

    return applied


def _apply_supervisor_decisions(project: str, decisions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    pdir = L.project_dir(project)
    ledger = ProgressLedger(pdir)
    applied: List[Dict[str, Any]] = []
    for decision in decisions:
        action = decision["action"]
        worker = decision.get("worker")
        reason = str(decision.get("reason") or "")
        result = "no-op"

        if action == "CONTINUE":
            result = "continued"
        elif action == "REVIEW":
            if worker:
                current = ledger.current(worker, refresh_stale=True)
                if current and current["state"] != "DONE":
                    result = _pause_one(
                        L.WorkerLayout(L.worker_dir(project, worker)),
                        reason=f"review:{reason}",
                    )
                else:
                    result = "pending_review"
            else:
                result = "pending_review"
        elif action == "RETASK":
            if worker:
                result = _start_one(L.WorkerLayout(L.worker_dir(project, worker)))
            else:
                # A ready node has no task text or scientific role encoded in Plan.
                # Deterministic Supervisor must not invent either.
                result = "pending_assignment"
        elif action == "STOP":
            if worker:
                wl = L.WorkerLayout(L.worker_dir(project, worker))
                if reason.startswith("resource_"):
                    result = _pause_one(wl, reason=f"budget:{reason}")
                else:
                    result = _stop_one(wl, force=True)
            else:
                result = "no_worker"

        applied.append({**decision, "result": result})
    return applied


def _record_supervisor_event(pdir: Path, trigger: str, report: Dict[str, Any]) -> None:
    append_jsonl(
        pdir / "supervisor" / "events.jsonl",
        {
            "event": "RECONCILE",
            "trigger": trigger,
            "project_state": report.get("project_state"),
            "plan_version": report.get("plan_version"),
            "plan_changes": report.get("plan_changes", []),
            "decisions": report.get("decisions", []),
            "applied": report.get("applied", []),
            "budget": report.get("budget", {}),
            "quota_policy": report.get("quota_policy", {}),
            "quota_applied": report.get("quota_applied", []),
            "reviewer_cost_policy": report.get("reviewer_cost_policy", []),
            "reviewer_cost_applied": report.get("reviewer_cost_applied", []),
            "at": utc_now(),
        },
    )


def do_supervise(
    project: str,
    *,
    complete_node: Optional[str] = None,
    reopen_node: Optional[str] = None,
    apply: bool = False,
    token_budget: int = 0,
    trigger: str = "manual",
) -> Dict:
    """Run one deterministic Supervisor reconciliation pass.

    ``--complete-node`` / ``--reopen-node`` update only Progress Ledger node state;
    they never revise the Research Plan. With ``apply=True`` the existing
    CONTINUE/REVIEW/RETASK/STOP decisions are executed as process-control actions.
    """
    pdir = L.project_dir(project)
    if not pdir.is_dir():
        raise SystemExit(f"no such project: {project}")
    if not (pdir / "plan" / "current.json").is_file():
        raise SystemExit(f"project {project} has no Research Plan")
    if complete_node and reopen_node:
        raise SystemExit("choose only one of --complete-node or --reopen-node")
    if token_budget < 0:
        raise SystemExit("token budget must be >= 0")

    ledger = ProgressLedger(pdir)
    try:
        if complete_node:
            ledger.set_node_state(complete_node, state="DONE", actor="supervisor")
        elif reopen_node:
            ledger.set_node_state(reopen_node, state="OPEN", actor="supervisor")
        rows = _project_rows(project)
        report = reconcile(pdir, rows)
        complete = report.get("project_state") == "COMPLETE"
        budget = _resource_snapshot(pdir, rows, token_budget)
        report["budget"] = budget
        if not complete:
            _apply_resource_budget(report, rows, budget)
        policy = quota_policy(Q.load_quota(pdir))
        report["quota_policy"] = policy
        report["reviewer_cost_policy"] = (
            [] if complete else _reviewer_cost_rows(project, rows, policy, apply=False)
        )
        if apply:
            report["reviewer_cost_applied"] = (
                [] if complete else _reviewer_cost_rows(project, rows, policy, apply=True)
            )
            report["quota_applied"] = [] if complete else _apply_quota_gate(project, rows, policy)
            report["applied"] = _apply_supervisor_decisions(project, report["decisions"])
            _record_supervisor_event(pdir, trigger, report)
        return report
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(f"cannot reconcile project: {exc}") from exc


def _control_signature(project: str, rows: List[Dict[str, Any]], budget: Dict[str, Any]) -> str:
    """Small change signature; age_s is deliberately excluded so idle time is not an event."""
    pdir = L.project_dir(project)
    compact_rows = [
        {k: row.get(k) for k in (
            "worker", "pid", "alive", "state", "round", "label", "paused", "pause_reason",
            "assignment_id", "plan_node_id", "plan_version", "progress_state", "progress_outcome",
            "round_usage_tokens", "usage_tokens_total", "usage_available", "usage_complete",
            "usage_tool_calls", "usage_compactions", "context_trigger", "terminal_outcome",
            "reviewer_backend_selected", "reviewer_model_selected", "reviewer_backend_reason",
            "reviewer_dsh_quota_exhausted", "reviewer_dsh_quota_assignment_id",
        )}
        for row in rows
    ]

    stamps = []
    paths = [pdir / "plan" / "current.json", pdir / "progress" / "ledger.jsonl",
             pdir / L.DEADLINE_FILE, Q.quota_path(pdir)]
    paths.extend(sorted((pdir / "plan" / "changes").glob("*.json")))
    paths.extend(sorted((pdir / "experiment_runtime").glob("*/checkpoint.json")))
    for path in paths:
        try:
            stat = path.stat()
            stamps.append((str(path.relative_to(pdir)), stat.st_mtime_ns, stat.st_size))
        except OSError:
            continue
    return json.dumps(
        {
            "rows": compact_rows,
            "stamps": stamps,
            "experiments": budget.get("experiments", []),
            "budget_state": {
                "token_exceeded": budget.get("tokens", {}).get("exceeded"),
                "token_complete": bool(budget.get("tokens", {}).get("complete", True)),
                "time_exceeded": bool(budget.get("time", {}).get("exceeded")),
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def watch_supervisor(
    project: str,
    *,
    heartbeat: float = 1800.0,
    poll: float = 1.0,
    token_budget: int = 0,
) -> Iterator[Dict[str, Any]]:
    """Change-driven long-running Supervisor with quota refresh at safe boundaries."""
    if heartbeat <= 0:
        raise SystemExit("heartbeat must be > 0")
    if poll <= 0:
        raise SystemExit("poll must be > 0")

    pdir = L.project_dir(project)
    last_signature: Optional[str] = None
    last_reconcile = 0.0
    last_quota_refresh: Optional[float] = None
    while True:
        now = time.monotonic()
        quota_due = (
            last_quota_refresh is None
            or now - last_quota_refresh >= _QUOTA_REFRESH_SECONDS
        )
        if quota_due:
            Q.refresh_quota(pdir)
            last_quota_refresh = now

        rows = _project_rows(project)
        budget = _resource_snapshot(pdir, rows, token_budget)
        signature = _control_signature(project, rows, budget)
        trigger = None
        if signature != last_signature:
            trigger = "event"
        elif quota_due:
            trigger = "quota"
        elif now - last_reconcile >= heartbeat:
            trigger = "heartbeat"

        if trigger:
            report = do_supervise(
                project,
                apply=True,
                token_budget=token_budget,
                trigger=trigger,
            )
            last_reconcile = now
            # Actions themselves change pid/pause/progress/quota files; consume that
            # new state now so the next poll does not replay our own action.
            rows = _project_rows(project)
            budget = _resource_snapshot(pdir, rows, token_budget)
            last_signature = _control_signature(project, rows, budget)
            yield report
            if report.get("project_state") == "COMPLETE" and not report.get("decisions"):
                return
        time.sleep(poll)


# --------------------------------------------------------------------------- #
# list                                                                         #
# --------------------------------------------------------------------------- #

def do_list() -> List[Dict]:
    """One row per project: roster + how many workers are live + model."""
    out: List[Dict] = []
    for project in L.list_projects():
        meta = {}
        mp = L.project_dir(project) / "project.json"
        if mp.exists():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        workers = L.list_workers(project)
        live = sum(1 for w in workers
                   if _alive(_read_pid(L.WorkerLayout(L.worker_dir(project, w)))))
        out.append({"project": project, "workers": len(workers), "live": live,
                    "model": meta.get("model", "—")})
    return out


def _fmt_list(rows: List[Dict]) -> str:
    head = f"{'PROJECT':<24}{'WORKERS':>8}{'LIVE':>6}  {'MODEL':<12}"
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(f"{r['project']:<24}{r['workers']:>8}{r['live']:>6}  {str(r['model']):<12}")
    return "\n".join(lines) if rows else "(no projects under the agents root)"


def _fmt_status(rows: List[Dict]) -> str:
    head = f"{'WORKER':<14}{'LABEL':<12}{'STATE':<13}{'ROUND':>6}  {'AGE':>7}  {'LAST_FACT':<16}"
    lines = [head, "-" * len(head)]
    for r in rows:
        age = f"{r['age_s']:.0f}s" if r["age_s"] is not None else "—"
        lines.append(f"{r['worker']:<14}{r['label']:<12}{r['state']:<13}"
                     f"{r['round']:>6}  {age:>7}  {str(r['last_fact_id'] or '—'):<16}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# stop                                                                         #
# --------------------------------------------------------------------------- #

def _stop_one(wl: L.WorkerLayout, force: bool) -> str:
    pid = _read_pid(wl)
    if not force:
        if not _alive(pid):
            return "not-running"
        wl.stop.touch()
        return "stopping (graceful)"
    if not _alive(pid):
        wl.pid.unlink(missing_ok=True)
        return "not-running"
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.1)
    if _alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    wl.pid.unlink(missing_ok=True)
    return "killed"


def do_stop(target: str, force: bool = False) -> List[Dict]:
    dirs = L.target_worker_dirs(target)
    if not dirs:
        raise SystemExit(f"no workers for target {target!r}")
    return [{"worker": d.name, "result": _stop_one(L.WorkerLayout(d), force)} for d in dirs]


def _pause_one(wl: L.WorkerLayout, reason: str = "manual") -> str:
    atomic_write(wl.pause, (reason.strip() or "manual") + "\n")
    _stop_one(wl, force=True)
    return "paused"


def do_pause(target: str) -> List[Dict]:
    dirs = L.target_worker_dirs(target)
    if not dirs:
        raise SystemExit(f"no workers for target {target!r}")
    return [{"worker": d.name, "result": _pause_one(L.WorkerLayout(d))} for d in dirs]


def _resume_one(wl: L.WorkerLayout) -> str:
    wl.pause.unlink(missing_ok=True)
    return _start_one(wl)


def do_resume(target: str, stagger: float = 0.2) -> List[Dict]:
    dirs = L.target_worker_dirs(target)
    if not dirs:
        raise SystemExit(f"no workers for target {target!r}")
    out = []
    for i, wdir in enumerate(dirs):
        if i and stagger:
            time.sleep(stagger)
        out.append({"worker": wdir.name, "result": _resume_one(L.WorkerLayout(wdir))})
    return out


# --------------------------------------------------------------------------- #
# argparse                                                                      #
# --------------------------------------------------------------------------- #

def _task_from_args(args) -> str:
    import sys
    if args.task is not None:
        return args.task
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.stdin:
        return sys.stdin.read()
    raise SystemExit("assign needs one of --task, --file, or --stdin")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="danus", description="Control codex workers.")
    sub = p.add_subparsers(dest="cmd", required=True)

    li = sub.add_parser("list", help="list all projects + live worker counts")
    li.add_argument("--json", action="store_true")

    n = sub.add_parser("new", help="scaffold a project + worker dirs")
    n.add_argument("project")
    n.add_argument("--roles", default="high:3,xhigh:4", help="e.g. high:3,xhigh:4 (default)")
    n.add_argument("--model", default=None)

    a = sub.add_parser("assign", help="write a worker's per-round TASK.md")
    a.add_argument("target", help="<project>/<worker>")
    a.add_argument("--task", default=None)
    a.add_argument("--file", default=None)
    a.add_argument("--stdin", action="store_true")
    a.add_argument("--node", default=None, help="Research Plan node id; required when a plan exists")

    f = sub.add_parser("finalize", help="record the finalized target fact_id(s) in "
                                        "a paper's TARGET.md (write-paper reads this)")
    f.add_argument("project")
    f.add_argument("--paper", default=None,
                   help="the paper_id (multiple papers per project). Default / 'main' "
                        "→ legacy <project>/TARGET.md; else "
                        "<project>/papers/<paper_id>/TARGET.md")
    f.add_argument("fact_ids", nargs="*",
                   help="the target fact id(s); omit to print candidate terminal facts")

    s = sub.add_parser("start", help="launch worker loop(s)")
    s.add_argument("target", help="<project> or <project>/<worker>")

    st = sub.add_parser("status", help="liveness + progress")
    st.add_argument("target", help="<project> or <project>/<worker>")
    st.add_argument("--json", action="store_true")

    sv = sub.add_parser("supervise", help="reconcile Plan, progress, worker state, and budgets")
    sv.add_argument("project")
    group = sv.add_mutually_exclusive_group()
    group.add_argument("--complete-node", default=None,
                       help="record Supervisor-approved node completion in Progress Ledger")
    group.add_argument("--reopen-node", default=None,
                       help="reopen a node in Progress Ledger without revising Plan")
    sv.add_argument("--apply", action="store_true", help="execute this one reconcile pass")
    sv.add_argument("--watch", action="store_true", help="run continuously; implies --apply")
    sv.add_argument("--heartbeat", type=float, default=1800.0,
                    help="fallback reconcile interval in seconds (default 1800)")
    sv.add_argument("--poll", type=float, default=1.0,
                    help="control-file/liveness poll interval in seconds (default 1)")
    sv.add_argument("--token-budget", type=int, default=0,
                    help="project cumulative completed-session token budget; 0 disables")

    pa = sub.add_parser("pause", help="pause worker loop(s) and prevent automatic restart")
    pa.add_argument("target", help="<project> or <project>/<worker>")

    rs = sub.add_parser("resume", help="clear pause and restart worker loop(s)")
    rs.add_argument("target", help="<project> or <project>/<worker>")

    sp = sub.add_parser("stop", help="stop worker loop(s)")
    sp.add_argument("target", help="<project> or <project>/<worker>")
    sp.add_argument("--force", action="store_true", help="kill now (else finish current round)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "list":
        rows = do_list()
        print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else _fmt_list(rows))
    elif args.cmd == "new":
        r = do_new(args.project, roles=args.roles, model=args.model)
        print(f"created {args.project} with {len(r['workers'])} workers: "
              f"{', '.join(r['workers'])}\n  {r['project_dir']}")
    elif args.cmd == "assign":
        r = do_assign(args.target, _task_from_args(args), node_id=args.node)
        suffix = ""
        if r.get("assignment_id"):
            suffix = f" [node {r['node_id']} @ plan v{r['plan_version']}]"
        print(f"assigned {r['worker']} -> {r['task_file']}{suffix}")
    elif args.cmd == "finalize":
        r = do_finalize(args.project, args.fact_ids, paper_id=args.paper)
        paper_note = f" (paper {args.paper})" if args.paper else ""
        paper_flag = f" --paper {args.paper}" if args.paper else ""
        if "suggested" in r:
            sug = r["suggested"]
            if sug:
                print(f"no fact_id given — candidate target facts for {r['project']}{paper_note} "
                      f"(terminal facts; nothing depends on them):")
                for fid in sug:
                    print(f"  {fid}")
                print(f"\nrun: danus finalize {r['project']}{paper_flag} <fact_id> [<fact_id> ...] to record")
            else:
                print(f"no candidate terminal facts in {r['project']} "
                      f"(is the fact graph empty?); nothing recorded")
        else:
            print(f"finalized target for {r['project']}{paper_note}: {', '.join(r['target_fact_ids'])}\n"
                  f"  wrote {r['target_file']}")
    elif args.cmd == "start":
        for r in do_start(args.target):
            print(f"{r['worker']}: {r['result']}")
    elif args.cmd == "status":
        rows = do_status(args.target)
        print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else _fmt_status(rows))
    elif args.cmd == "supervise":
        if args.watch:
            if args.complete_node or args.reopen_node:
                raise SystemExit("--watch cannot be combined with node state mutation")
            for r in watch_supervisor(
                args.project,
                heartbeat=args.heartbeat,
                poll=args.poll,
                token_budget=args.token_budget,
            ):
                print(json.dumps(r, ensure_ascii=False, sort_keys=True), flush=True)
        else:
            r = do_supervise(
                args.project,
                complete_node=args.complete_node,
                reopen_node=args.reopen_node,
                apply=args.apply,
                token_budget=args.token_budget,
            )
            print(json.dumps(r, ensure_ascii=False, indent=2))
    elif args.cmd == "pause":
        for r in do_pause(args.target):
            print(f"{r['worker']}: {r['result']}")
    elif args.cmd == "resume":
        for r in do_resume(args.target):
            print(f"{r['worker']}: {r['result']}")
    elif args.cmd == "stop":
        for r in do_stop(args.target, force=args.force):
            print(f"{r['worker']}: {r['result']}")
    return 0
