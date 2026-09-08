"""The per-worker autonomous outer loop — the round driver.

Each round runs one model session and resumes from persisted project state. Legacy
mathematical workers keep the historical ``worker.md`` + mathematical skills.
Researcher/Reviewer/Experimenter rounds use the minimal scientific worker spine,
their role contract, project Research Charter, and role-specific gateway tools.
Researcher/Reviewer have no default skills; the Experimenter gets only Ponytail.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import context as C
from . import layout as L
from . import scaffold
from danus import codex
from danus.sandbox import _arm_parent_death
from . import worker as W
from danus.core import ProgressLedger
from danus.core._util import read_jsonl
from danus.core.handoff import HandoffRequests
from danus.gateway.roles import tools_for

_FACT_ID_RE = re.compile(r"(?i:fact(?:_|\s+)id)\b[\"'`\s]*[:=]?\s*[\"'`]?([0-9a-f]{16})[\"'`]?")
_OUTCOME_RE = re.compile(r"(?im)^DANUS_OUTCOME:\s*(COMPLETE|BLOCKED|NEEDS_REVIEW)\s*$")


# --- the per-round prompt -------------------------------------------------- #

def _scientific_context(project: str, role_name: str) -> str:
    role_path = L.scientific_role_md(role_name)
    if role_path is None:
        return ""
    role_text = role_path.read_text(encoding="utf-8")
    charter_path = L.research_charter(project)
    if charter_path.exists():
        charter_text = charter_path.read_text(encoding="utf-8")
    else:
        charter_text = (
            "# Research Charter\n\n"
            "No project RESEARCH_CHARTER.md exists. Do not invent project-specific "
            "constraints; surface the missing Charter before substantive scientific work.\n"
        )
    guidance = ""
    guidance_path = L.project_dir(project) / "HUMAN_GUIDANCE.md"
    if guidance_path.is_file():
        try:
            guidance = guidance_path.read_text(encoding="utf-8").strip()
        except OSError:
            guidance = ""
    guidance_block = (
        f"\n\n<CURRENT_HUMAN_GUIDANCE>\n{guidance}\n</CURRENT_HUMAN_GUIDANCE>"
        if guidance
        else ""
    )
    return (
        f"\n\nYour Danus-R scientific ROLE is '{role_name}'. The role contract and "
        "project Research Charter below are binding for this round and supplement "
        "the minimal common AGENTS.md scientific-worker spine.\n\n"
        f"<SCIENTIFIC_ROLE_CONTRACT>\n{role_text}\n</SCIENTIFIC_ROLE_CONTRACT>\n\n"
        f"<PROJECT_RESEARCH_CHARTER>\n{charter_text}\n</PROJECT_RESEARCH_CHARTER>"
        f"{guidance_block}"
    )


def _resolved_scientific_route_context(project: str, assignment_id: str) -> str:
    root = L.project_dir(project)
    ledger_path = root / "progress" / "ledger.jsonl"
    if not assignment_id or not ledger_path.is_file():
        return ""
    ledger = ProgressLedger(root)
    routes = [
        item
        for item in HandoffRequests(ledger).all()
        if item.get("kind") in {"INTERPRET", "RETHINK"}
        and item.get("subject_type") == "ASSIGNMENT"
        and item.get("subject_id") == assignment_id
        and item.get("status") == "RESOLVED"
    ]
    if not routes:
        return ""
    routes.sort(key=lambda item: (str(item.get("resolved_at") or ""), str(item.get("handoff_id") or "")))
    route = routes[-1]
    prefix = f"resumed after scientific route {route['handoff_id']}"
    for event in reversed(read_jsonl(ledger.path)):
        if (
            event.get("event") == "STATE"
            and event.get("assignment_id") == assignment_id
            and event.get("actor") == "supervisor"
            and str(event.get("note") or "").startswith(prefix)
        ):
            note = str(event.get("note") or "")
            result = note.split("\n\n", 1)[1] if "\n\n" in note else note
            return (
                "\n\nThe resolved scientific route below is durable reviewed context for this same "
                "assignment, not a replacement for TASK/Charter/Plan. Use it to continue rather than reopening "
                "the same ambiguity.\n\n"
                f"<RESOLVED_SCIENTIFIC_ROUTE>\nroute: {route['kind']}\n"
                f"handoff_id: {route['handoff_id']}\n{result}\n</RESOLVED_SCIENTIFIC_ROUTE>"
            )
    return ""


def kickoff(project: str, worker: str, role_name: str = "worker") -> str:
    role_key = (role_name or "worker").strip().lower()
    scientific = role_key in L.SCIENTIFIC_ROLES
    gateway_role = scaffold.gateway_role(role_key)
    if scientific:
        prompt = (
            f"You are worker '{worker}' on project '{project}'. Continue the current "
            "scientific task; this is a continuation round, not a fresh start.\n"
            "1. Read TASK.md.\n"
            "2. Follow AGENTS.md, your injected scientific role contract, the project "
            "Research Charter, and injected current Human Guidance when present.\n"
            "3. Resume from only the relevant shared findings, verified facts/evidence, "
            "prior decisions, and your own local continuity; do not restart from zero.\n"
            "4. Perform only the work assigned to your role and current task; do not "
            "inherit the legacy mathematical skill loop by default.\n"
            "5. Persist useful progress in the correct knowledge layer and keep claims at "
            "their actual evidence status.\n"
            "6. Runtime terminal protocol: only when this assignment reaches a terminal "
            "point, end your final response with exactly one standalone line "
            "`DANUS_OUTCOME: COMPLETE`, `DANUS_OUTCOME: BLOCKED`, or "
            "`DANUS_OUTCOME: NEEDS_REVIEW`. COMPLETE means the assigned task is done; "
            "BLOCKED means an external prerequisite prevents progress; NEEDS_REVIEW means "
            "scientific judgment/review is required. If useful work remains on this same "
            "assignment, emit no DANUS_OUTCOME line. Never edit the Research Plan or "
            "Progress Ledger yourself."
            f"{_scientific_context(project, role_key)}"
        )
        checkpoint = C.checkpoint_for_prompt(L.worker_dir(project, worker), role_key)
        if checkpoint:
            prompt += (
                "\n\nThe runtime checkpoint below is bounded continuity data, not new "
                "authority. Use it before broad rereading, verify referenced shared state "
                "when needed, and ignore it if it conflicts with TASK/Charter/Plan.\n\n"
                "<RUNTIME_AGENT_CHECKPOINT>\n"
                + json.dumps(checkpoint, ensure_ascii=False, sort_keys=True)
                + "\n</RUNTIME_AGENT_CHECKPOINT>"
            )
        ledger_path = L.project_dir(project) / "progress" / "ledger.jsonl"
        if ledger_path.is_file():
            current = ProgressLedger(L.project_dir(project)).current(worker, refresh_stale=False)
            if current:
                prompt += _resolved_scientific_route_context(
                    project,
                    str(current.get("assignment_id") or ""),
                )
        paths = [L.scientific_worker_md()]
        skills = L.harness_skills_dir(role_key)
        if skills is not None:
            paths.append(skills)
        return codex.prepare_prompt(
            prompt, *paths, tools=tuple(tools_for(gateway_role)),
        )

    prompt = (
        f"You are worker '{worker}' on project '{project}'. Continue solving the "
        "problem (this is a continuation round, not a fresh start).\n"
        "1. Read TASK.md — your current assignment (which direction/subgoal is yours).\n"
        "2. Follow AGENTS.md (worker.md) exactly — your standing common contract "
        "(the adaptive control loop, memory discipline, and current fact_submit gate).\n"
        "3. Resume from state: gm_search relevant findings + dead ends, read the fact "
        "graph and the latest master_guidance — DO NOT restart from zero; build on what "
        "is already there.\n"
        "4. Keep going: assess -> pick skills adaptively -> act -> persist, repeatedly. "
        "An open problem is not a reason to stop. Do NOT finalize prematurely.\n"
        "5. Persist as you go: rough progress to local memory; shareable findings via "
        "gm_add; use fact_submit only where the active contracts say the current legacy "
        "verification path is appropriate."
    )
    return codex.prepare_prompt(
        prompt, L.worker_md(), L.worker_skills_dir(),
        tools=tuple(tools_for("worker")),
    )


# --- config --------------------------------------------------------------- #

def _read_role(wl: L.WorkerLayout) -> dict:
    out = {"MODEL": codex.model(),
           "REASONING_EFFORT": "high", "ROLE": "worker", "DANUS_AUTHOR": wl.name}
    rp = wl.role
    if rp.exists():
        for line in rp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def write_status(wl: L.WorkerLayout, **fields) -> None:
    path = wl.status
    cur = {}
    if path.exists():
        try:
            cur = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cur = {}
    cur.update(fields)
    cur["worker"] = wl.name
    cur["pid"] = os.getpid()
    cur["updated_at"] = time.time()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_status(wl: L.WorkerLayout) -> dict:
    try:
        obj = json.loads(wl.status.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _deadline_passed(project_dir: Path) -> bool:
    f = project_dir / L.DEADLINE_FILE
    if not f.exists():
        return False
    try:
        return time.time() >= float(f.read_text().strip())
    except (ValueError, OSError):
        return False


def _parse_last_fact_id(log_path: Path) -> Optional[str]:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    ids = _FACT_ID_RE.findall(text)
    return ids[-1] if ids else None


def _terminal_outcome(telemetry: dict) -> Optional[str]:
    matches = _OUTCOME_RE.findall(str(telemetry.get("last_agent_message") or ""))
    return matches[0].upper() if len(matches) == 1 else None


def _dsh_quota_exhausted(log_path: Path) -> bool:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(line.startswith("dsh: QUOTA:") for line in text.splitlines())


def _progress_ledger(wl: L.WorkerLayout) -> Optional[ProgressLedger]:
    path = wl.project_dir / "progress" / "ledger.jsonl"
    return ProgressLedger(wl.project_dir) if path.is_file() else None


# --- one round ------------------------------------------------------------- #

class _Child:
    proc: "subprocess.Popen | None" = None


def _send_tree_signal(proc, sig: int) -> bool:
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int) and pid > 0:
        try:
            os.killpg(os.getpgid(pid), sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        if sig == signal.SIGTERM:
            proc.terminate()
        else:
            proc.kill()
        return True
    except (AttributeError, ProcessLookupError, OSError):
        return False


def _terminate_process_tree(proc, grace: float = 2.0) -> None:
    _send_tree_signal(proc, signal.SIGTERM)
    wait = getattr(proc, "wait", None)
    if not callable(wait):
        return
    try:
        wait(timeout=grace)
    except subprocess.TimeoutExpired:
        _send_tree_signal(proc, signal.SIGKILL)


def _terminate_child() -> None:
    if _Child.proc is not None:
        _terminate_process_tree(_Child.proc)


def run_round(wl: L.WorkerLayout, role: dict, prompt: str, log_path: Path,
              hard_timeout: int, context_budget: bool = False) -> int:
    """124=timeout, 125=context rollover, 126=authority failure, 127=missing engine."""
    started = time.monotonic()
    cursor = C.LogCursor()
    parent_pid = os.getpid()
    with open(log_path, "w", encoding="utf-8") as logf:
        try:
            with W.worker_session(wl, role) as session:
                cmd = W.command(session, role, prompt, json_output=True)
                _Child.proc = subprocess.Popen(
                    cmd, stdout=logf, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, cwd=str(session["cwd"]),
                    env=session["env"], pass_fds=(session["fd"],), start_new_session=True,
                    preexec_fn=lambda: _arm_parent_death(parent_pid),
                )
                try:
                    while True:
                        try:
                            return _Child.proc.wait(timeout=0.5)
                        except subprocess.TimeoutExpired:
                            try:
                                W.check_session(session)
                            except PermissionError:
                                C.inspect_log(log_path, cursor=cursor)
                                if not cursor.structured_usage:
                                    raise
                                with W.capability.scope(session["binding"]):
                                    pass
                                remaining = 15 if hard_timeout <= 0 else min(15, hard_timeout - (time.monotonic() - started))
                                if remaining <= 0:
                                    return 124
                                try:
                                    return _Child.proc.wait(timeout=remaining)
                                except subprocess.TimeoutExpired:
                                    return 124
                            elapsed = time.monotonic() - started
                            if hard_timeout > 0 and elapsed >= hard_timeout:
                                logf.write(f"\n[worker_loop] round hard-timeout after {hard_timeout}s\n")
                                return 124
                            if context_budget:
                                telemetry = C.inspect_log(log_path, cursor=cursor)
                                live_telemetry = dict(telemetry)
                                live_telemetry["tokens"] = 0
                                reason = C.budget_reason(live_telemetry, elapsed)
                                if reason:
                                    C.write_rollover_request(wl.dir, reason, telemetry)
                                    logf.write(f"\n[worker_loop] context rollover: {reason}\n")
                                    return 125
                finally:
                    _terminate_child()
                    _Child.proc = None
        except FileNotFoundError as exc:
            logf.write(f"[worker_loop] required sandbox/runtime file not found: {exc}\n")
            return 127
        except (OSError, ValueError, RuntimeError) as exc:
            logf.write(f"[worker_loop] worker isolation refused: {exc}\n")
            return 126


def _run_context_migration(
    wl: L.WorkerLayout, role: dict, reason: str, prior_log: Path, hard_timeout: int,
) -> Optional[dict]:
    prompt = C.migration_prompt(wl.dir, reason, prior_log)
    log_path = wl.logs / f"migration_{prior_log.stem.removeprefix('round_')}.log"
    timeout = min(hard_timeout, 300) if hard_timeout > 0 else 300
    cursor = C.LogCursor()
    parent_pid = os.getpid()
    with open(log_path, "w", encoding="utf-8") as logf:
        try:
            with W.worker_session(wl, role) as session:
                cmd = W.command(session, {**role, "REASONING_EFFORT": "low"}, prompt, json_output=True)
                proc = subprocess.Popen(
                    cmd, stdout=logf, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, cwd=str(session["cwd"]), env=session["env"],
                    pass_fds=(session["fd"],), start_new_session=True,
                    preexec_fn=lambda: _arm_parent_death(parent_pid),
                )
                started = time.monotonic()
                try:
                    while True:
                        try:
                            rc = proc.wait(timeout=0.5)
                            if rc != 0:
                                return None
                            with W.capability.scope(session["binding"]):
                                return C.load_migration(session["cwd"])
                        except subprocess.TimeoutExpired:
                            try:
                                W.check_session(session)
                            except PermissionError:
                                C.inspect_log(log_path, cursor=cursor)
                                if not cursor.structured_usage:
                                    raise
                                remaining = min(15, timeout - (time.monotonic() - started))
                                if remaining <= 0:
                                    return None
                                try:
                                    rc = proc.wait(timeout=remaining)
                                except subprocess.TimeoutExpired:
                                    return None
                                with W.capability.scope(session["binding"]):
                                    return C.load_migration(session["cwd"]) if rc == 0 else None
                            if time.monotonic() - started >= timeout:
                                return None
                finally:
                    _terminate_process_tree(proc)
        except (OSError, ValueError, RuntimeError):
            return None


# --- the loop -------------------------------------------------------------- #

def _cleanup_pid(wl: L.WorkerLayout) -> None:
    pf = wl.pid
    try:
        parts = pf.read_text().split() if pf.exists() else []
        if parts and parts[0] == str(os.getpid()):
            pf.unlink(missing_ok=True)
    except OSError:
        pass


def _assignment_task_matches(wl: L.WorkerLayout, assignment: dict) -> bool:
    expected = str(assignment.get("task_sha256") or "")
    if not expected:
        return False
    try:
        task = wl.task.read_text(encoding="utf-8")
    except OSError:
        return False
    return ProgressLedger.task_sha256(task) == expected


def main(worker_dir: str) -> int:
    wdir = Path(worker_dir).resolve()
    if not wdir.is_dir():
        print(f"worker dir not found: {wdir}", file=sys.stderr)
        return 2
    wl = L.WorkerLayout(wdir)
    scaffold.write_worker_pid(wl, os.getpid())
    project_dir = wl.project_dir
    project = wl.project
    worker = wl.name
    role = _read_role(wl)
    role_name = role.get("ROLE", "worker")
    role_key = str(role_name or "worker").strip().lower()
    scientific = role_key in L.SCIENTIFIC_ROLES
    prior_status = _read_status(wl)
    backend = str(role.get("BACKEND") or "").strip().lower()
    auto_reviewer = backend == "auto" and role_key == "reviewer"
    if backend:
        if auto_reviewer:
            selected = str(prior_status.get("reviewer_backend_selected") or "dsh").lower()
            os.environ["CODEX_BACKEND"] = selected if selected in {"chatgpt", "dsh"} else "dsh"
            selected_model = str(prior_status.get("reviewer_model_selected") or "").strip()
            if selected_model:
                role["MODEL"] = selected_model
        else:
            os.environ["CODEX_BACKEND"] = backend

    scaffold.sync_worker_harness(wl, role_name)
    scaffold.write_codex_config(wl, role_name)

    beat = float(os.environ.get("DANUS_ROUND_BEAT", "5"))
    hard_timeout = int(os.environ.get("DANUS_ROUND_HARD_TIMEOUT", "14400"))
    max_rounds = int(os.environ.get("DANUS_MAX_ROUNDS", "0"))
    max_fail = int(os.environ.get("DANUS_MAX_CONSEC_FAILURES", "5"))
    wl.logs.mkdir(parents=True, exist_ok=True)

    try:
        rnd = max(0, int(prior_status.get("round") or 0))
    except (TypeError, ValueError):
        rnd = 0
    round_limit = rnd + max_rounds if max_rounds > 0 else 0
    try:
        context_tokens_total = max(0, int(prior_status.get("context_tokens_total") or 0))
    except (TypeError, ValueError):
        context_tokens_total = 0

    ledger = _progress_ledger(wl)
    if ledger is not None:
        with ledger.assignment_lock():
            pass
    exit_recorded = False

    def _record_exit(reason: str, **details) -> None:
        nonlocal exit_recorded
        if ledger is None or exit_recorded:
            return
        ledger.runtime_event(
            worker=worker,
            kind="EXITED",
            note=reason,
            details=details or None,
        )
        exit_recorded = True

    def _on_term(signum, _frame):
        _terminate_child()
        write_status(wl, state="terminated")
        _record_exit("terminated", signal=int(signum))
        _cleanup_pid(wl)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    write_status(wl, state="running", round=rnd, started_at=time.time(),
                 context_tokens_total=context_tokens_total)
    if ledger is not None:
        current = ledger.current(worker, refresh_stale=True)
        if current is not None:
            assignment_id = str(current.get("assignment_id") or "")
            checkpoint = C.checkpoint_for_prompt(wl.dir, role_name, assignment=current)
            if (
                current["state"] in {"ASSIGNED", "RUNNING"}
                and _assignment_task_matches(wl, current)
                and checkpoint
                and checkpoint.get("returncode") in (0, 125)
                and checkpoint.get("terminal_outcome") in ProgressLedger.TERMINAL_OUTCOMES
                and checkpoint.get("checkpoint_id")
                and checkpoint["checkpoint_id"] != current.get("checkpoint_id")
            ):
                recovered_outcome = checkpoint["terminal_outcome"]
                current = ledger.update(
                    worker=worker,
                    state="DONE" if recovered_outcome == "COMPLETE" else "BLOCKED",
                    actor="runtime",
                    note="recovered checkpointed terminal outcome",
                    outcome=recovered_outcome,
                    expected_assignment_id=assignment_id,
                    result=str(checkpoint.get("last_agent_message") or ""),
                    checkpoint_id=checkpoint["checkpoint_id"],
                )
            ledger.runtime_event(
                worker=worker,
                kind="STARTED",
                details={"pid": os.getpid(), "role": role_name},
            )
            if current["state"] in {"DONE", "BLOCKED", "STALE"}:
                write_status(
                    wl,
                    state="stopped",
                    terminal_outcome=current.get("outcome"),
                    progress_state=current["state"],
                )
                _record_exit(f"assignment_{current['state'].lower()}")
                _cleanup_pid(wl)
                return 0
            assignment_role = str(current.get("role") or "").strip().lower()
            if assignment_role and assignment_role != role_key:
                ledger.update(
                    worker=worker,
                    state="BLOCKED",
                    actor="runtime",
                    note=f"worker role {role_key} does not match assignment role {assignment_role}",
                    outcome="NEEDS_REVIEW",
                    expected_assignment_id=assignment_id,
                )
                write_status(
                    wl,
                    state="stopped",
                    progress_state="BLOCKED",
                    terminal_outcome="NEEDS_REVIEW",
                    assignment_role=assignment_role,
                    worker_role=role_key,
                )
                _record_exit(
                    "assignment_role_mismatch",
                    assignment_role=assignment_role,
                    worker_role=role_key,
                )
                _cleanup_pid(wl)
                return 0
            ledger.update(
                worker=worker,
                state="RUNNING",
                actor="runtime",
                note="worker process started",
                expected_assignment_id=assignment_id,
            )

    consec_fail = 0
    migration_failures = 0
    try:
        while True:
            if wl.pause.exists() or (project_dir / L.PAUSE_FILE).exists():
                write_status(wl, state="paused")
                _record_exit("paused")
                break
            round_assignment = None
            if ledger is not None:
                current = ledger.current(worker, refresh_stale=True)
                if current is not None and current["state"] in {"DONE", "BLOCKED", "STALE"}:
                    write_status(
                        wl,
                        state="stopped",
                        terminal_outcome=current.get("outcome"),
                        progress_state=current["state"],
                    )
                    _record_exit(f"assignment_{current['state'].lower()}")
                    break
                round_assignment = current
            if wl.stop.exists():
                wl.stop.unlink(missing_ok=True)
                write_status(wl, state="stopped")
                _record_exit("stopped")
                break
            if _deadline_passed(project_dir):
                write_status(wl, state="deadline")
                _record_exit("deadline")
                break
            if round_limit and rnd >= round_limit:
                write_status(wl, state="max_rounds")
                _record_exit("max_rounds")
                break
            if round_assignment is not None and not _assignment_task_matches(wl, round_assignment):
                assignment_id = str(round_assignment.get("assignment_id") or "")
                try:
                    ledger.update(
                        worker=worker,
                        state="BLOCKED",
                        actor="runtime",
                        note="TASK.md does not match assignment task_sha256",
                        outcome="BLOCKED",
                        expected_assignment_id=assignment_id,
                    )
                except ValueError:
                    latest = ledger.current(worker, refresh_stale=True)
                    latest_id = str((latest or {}).get("assignment_id") or "")
                    if assignment_id and latest_id != assignment_id:
                        continue
                    raise
                write_status(
                    wl,
                    state="stopped",
                    progress_state="BLOCKED",
                    terminal_outcome="BLOCKED",
                    assignment_id=assignment_id,
                    error="assignment_task_mismatch",
                )
                _record_exit("assignment_task_mismatch", assignment_id=assignment_id)
                break

            rnd += 1
            log_path = wl.logs / f"round_{rnd}.log"
            round_started = time.time()
            prompt = kickoff(project, worker, role_name)
            if round_assignment is not None:
                prompt += f"\n\nCurrent Assignment ID: `{round_assignment['assignment_id']}`. Copy this ID into any Plan proposal's `assignment_id`.\n"
                scaffold.write_codex_config(wl, role_name, round_assignment)
            write_status(wl, state="running", round=rnd, round_started_at=round_started)
            if scientific:
                rc = run_round(
                    wl, role, prompt, log_path, hard_timeout, context_budget=True,
                )
            else:
                rc = run_round(wl, role, prompt, log_path, hard_timeout)

            dsh_quota = bool(
                auto_reviewer
                and os.environ.get("CODEX_BACKEND") == "dsh"
                and rc != 0
                and _dsh_quota_exhausted(log_path)
            )
            quota_assignment_id = (
                str(round_assignment.get("assignment_id") or "")
                if dsh_quota and round_assignment is not None
                else None
            )

            telemetry = C.inspect_log(log_path) if scientific else {}
            elapsed = time.time() - round_started
            outcome = (
                _terminal_outcome(telemetry)
                if scientific and rc in (0, 125)
                else None
            )
            trigger = None
            if scientific:
                round_tokens = int(telemetry.get("tokens") or 0)
                context_tokens_total += max(0, round_tokens)
                requested = C.pop_rollover_request(wl.dir)
                if requested:
                    trigger = str(requested.get("reason") or "context_budget")
                elif rc == 0:
                    trigger = C.budget_reason(telemetry, elapsed)
                C.write_checkpoint(
                    wl.dir, role=role_name, round_no=rnd, telemetry=telemetry,
                    trigger=trigger, assignment=round_assignment,
                    returncode=rc, terminal_outcome=outcome,
                )

                migration_attempted = False
                if (
                    trigger
                    and not outcome
                    and not dsh_quota
                    and not wl.stop.exists()
                    and not wl.pause.exists()
                    and not (project_dir / L.PAUSE_FILE).exists()
                    and (not round_limit or rnd < round_limit)
                ):
                    migration_attempted = True
                    write_status(wl, state="migrating", context_trigger=trigger)
                    migration = _run_context_migration(
                        wl, role, trigger, log_path, hard_timeout,
                    )
                    migration_log = wl.logs / f"migration_{rnd}.log"
                    migration_tokens = int(C.inspect_log(migration_log).get("tokens") or 0)
                    context_tokens_total += max(0, migration_tokens)
                    C.write_checkpoint(
                        wl.dir, role=role_name, round_no=rnd, telemetry=telemetry,
                        trigger=trigger, migration=migration, assignment=round_assignment,
                    )
                    if migration is None:
                        migration_failures += 1
                        if max_fail and migration_failures >= max_fail:
                            write_status(
                                wl,
                                state="error",
                                error=f"{migration_failures} consecutive context migration failures",
                                context_trigger=trigger,
                                context_tokens_total=context_tokens_total,
                            )
                            _record_exit(
                                "consecutive_context_migration_failures",
                                count=migration_failures,
                                trigger=trigger,
                            )
                            return 1
                    else:
                        migration_failures = 0
                if not migration_attempted:
                    migration_failures = 0

            if outcome and ledger is not None and round_assignment is not None:
                progress_state = "DONE" if outcome == "COMPLETE" else "BLOCKED"
                assignment_id = str(round_assignment.get("assignment_id") or "")
                checkpoint = C.checkpoint_for_prompt(wl.dir, role_name, assignment=round_assignment)
                try:
                    ledger.update(
                        worker=worker,
                        state=progress_state,
                        actor="runtime",
                        note=f"agent declared {outcome}",
                        outcome=outcome,
                        expected_assignment_id=assignment_id,
                        result=str(telemetry.get("last_agent_message") or ""),
                        checkpoint_id=str((checkpoint or {}).get("checkpoint_id") or ""),
                    )
                except ValueError:
                    latest = ledger.current(worker, refresh_stale=True)
                    latest_id = str((latest or {}).get("assignment_id") or "")
                    if assignment_id and latest_id != assignment_id:
                        write_status(
                            wl,
                            state="stopped",
                            progress_state=str((latest or {}).get("state") or "REBOUND"),
                            ignored_terminal_outcome=outcome,
                            rebound_from_assignment_id=assignment_id,
                            rebound_to_assignment_id=latest_id,
                        )
                        break
                    if latest is not None and latest.get("state") == "STALE":
                        write_status(
                            wl,
                            state="stopped",
                            progress_state="STALE",
                            ignored_terminal_outcome=outcome,
                        )
                        _record_exit("assignment_stale_after_round")
                        break
                    raise

            status_fields = {
                "state": "stopped" if outcome or dsh_quota else "idle",
                "round": rnd,
                "last_round_at": time.time(),
                "last_rc": rc,
                "last_fact_id": _parse_last_fact_id(log_path),
            }
            if scientific:
                status_fields.update({
                    "context_tokens": int(telemetry.get("tokens") or 0),
                    "context_tokens_total": context_tokens_total,
                    "context_tool_calls": int(telemetry.get("tool_calls") or 0),
                    "context_compactions": int(telemetry.get("compactions") or 0),
                    "context_trigger": trigger,
                    "context_checkpoint": str(C._path(wl.dir, C.CHECKPOINT_NAME)),
                    "terminal_outcome": outcome,
                })
            if dsh_quota:
                status_fields.update({
                    "reviewer_dsh_quota_exhausted": True,
                    "reviewer_dsh_quota_assignment_id": quota_assignment_id,
                })
            write_status(wl, **status_fields)

            if dsh_quota:
                _record_exit(
                    "dsh_quota_exhausted",
                    assignment_id=quota_assignment_id,
                    returncode=rc,
                )
                break
            if outcome:
                _record_exit("terminal_outcome", outcome=outcome, returncode=rc)
                break
            if rc == 127:
                write_status(wl, state="error", error="codex binary not found")
                _record_exit("codex_binary_not_found", returncode=rc)
                return 127
            consec_fail = (
                consec_fail + 1
                if rc not in (0, 125) and (scientific or rc != 124)
                else 0
            )
            if max_fail and consec_fail >= max_fail:
                write_status(wl, state="error", error=f"{consec_fail} consecutive failed rounds")
                _record_exit("consecutive_failed_rounds", returncode=rc, count=consec_fail)
                return 1

            if beat > 0:
                time.sleep(beat)
    except Exception as exc:
        write_status(wl, state="error", error=str(exc))
        _record_exit("runtime_exception", error=type(exc).__name__)
        raise
    finally:
        _cleanup_pid(wl)
    return 0
