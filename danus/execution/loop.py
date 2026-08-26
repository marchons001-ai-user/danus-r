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
from danus.core import ProgressLedger
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
    return (
        f"\n\nYour Danus-R scientific ROLE is '{role_name}'. The role contract and "
        "project Research Charter below are binding for this round and supplement "
        "the minimal common AGENTS.md scientific-worker spine.\n\n"
        f"<SCIENTIFIC_ROLE_CONTRACT>\n{role_text}\n</SCIENTIFIC_ROLE_CONTRACT>\n\n"
        f"<PROJECT_RESEARCH_CHARTER>\n{charter_text}\n</PROJECT_RESEARCH_CHARTER>"
    )


def kickoff(project: str, worker: str, role_name: str = "worker") -> str:
    role_key = (role_name or "worker").strip().lower()
    scientific = role_key in L.SCIENTIFIC_ROLES
    gateway_role = scaffold.gateway_role(role_key)
    if scientific:
        prompt = (
            f"You are worker '{worker}' on project '{project}'. Continue the current "
            "scientific task; this is a continuation round, not a fresh start.\n"
            "1. Read TASK.md.\n"
            "2. Follow AGENTS.md, your injected scientific role contract, and the project "
            "Research Charter.\n"
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
    """Atomic status write (so `danus status` never reads a half-written file)."""
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
    """Parse only the explicit machine marker; never infer terminal state from prose."""
    matches = _OUTCOME_RE.findall(str(telemetry.get("last_agent_message") or ""))
    return matches[0].upper() if len(matches) == 1 else None


def _progress_ledger(wl: L.WorkerLayout) -> Optional[ProgressLedger]:
    path = wl.project_dir / "progress" / "ledger.jsonl"
    return ProgressLedger(wl.project_dir) if path.is_file() else None


# --- one round ------------------------------------------------------------- #

class _Child:
    """Holds the running codex subprocess so the SIGTERM handler can kill it."""
    proc: "subprocess.Popen | None" = None


def _terminate_child() -> None:
    if _Child.proc is None:
        return
    _Child.proc.terminate()
    try:
        _Child.proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _Child.proc.kill()


def run_round(wl: L.WorkerLayout, role: dict, prompt: str, log_path: Path,
              hard_timeout: int, context_budget: bool = False) -> int:
    """Exec one session. 124=hard timeout, 125=context rollover, 127=missing."""
    wdir = wl.dir
    codex_bin = codex.resolve_bin()
    tail = []
    # Codex exposes documented JSONL event telemetry. DSH currently does not;
    # leave its historical stdout untouched and degrade to runtime-only limits.
    if context_budget and codex.backend() != "dsh":
        tail.append("--json")
    tail += [
        "-C", str(wdir),
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        prompt,
    ]
    cmd = codex.exec_cmd(codex_bin, role["MODEL"], role["REASONING_EFFORT"], *tail)
    started = time.monotonic()
    with open(log_path, "w", encoding="utf-8") as logf:
        try:
            _Child.proc = subprocess.Popen(
                cmd, stdout=logf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, cwd=str(wdir),
                env=codex.subprocess_env(codex_bin),
            )
        except FileNotFoundError:
            logf.write(f"[worker_loop] codex binary not found: {cmd[0]}\n")
            return 127
        try:
            if not context_budget:
                try:
                    return _Child.proc.wait(timeout=hard_timeout if hard_timeout > 0 else None)
                except subprocess.TimeoutExpired:
                    _terminate_child()
                    logf.write(f"\n[worker_loop] round hard-timeout after {hard_timeout}s\n")
                    return 124

            while True:
                try:
                    return _Child.proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    elapsed = time.monotonic() - started
                    if hard_timeout > 0 and elapsed >= hard_timeout:
                        _terminate_child()
                        logf.write(f"\n[worker_loop] round hard-timeout after {hard_timeout}s\n")
                        return 124
                    telemetry = C.inspect_log(log_path)
                    # Codex reports token usage at turn.completed. Killing on that
                    # final usage event can race a normally completed turn before the
                    # process exits. Defer token-only rollover to the outer loop; keep
                    # tool/compaction/time limits live.
                    live_telemetry = dict(telemetry)
                    live_telemetry["tokens"] = 0
                    reason = C.budget_reason(live_telemetry, elapsed)
                    if reason:
                        _terminate_child()
                        C.write_rollover_request(wdir, reason, telemetry)
                        logf.write(f"\n[worker_loop] context rollover: {reason}\n")
                        return 125
        finally:
            _Child.proc = None


def _run_context_migration(
    wl: L.WorkerLayout, role: dict, reason: str, prior_log: Path, hard_timeout: int,
) -> Optional[dict]:
    """One bounded housekeeping turn only when a scientific session rolls over."""
    out_path = C.migration_path(wl.dir)
    out_path.unlink(missing_ok=True)
    prompt = C.migration_prompt(wl.dir, reason, prior_log)
    codex_bin = codex.resolve_bin()
    cmd = codex.exec_cmd(
        codex_bin, role["MODEL"], "low",
        "-C", str(wl.dir),
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        prompt,
    )
    log_path = wl.logs / f"migration_{prior_log.stem.removeprefix('round_')}.log"
    timeout = min(hard_timeout, 300) if hard_timeout > 0 else 300
    with open(log_path, "w", encoding="utf-8") as logf:
        try:
            proc = subprocess.Popen(
                cmd, stdout=logf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, cwd=str(wl.dir),
                env=codex.subprocess_env(codex_bin),
            )
        except FileNotFoundError:
            return None
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            return None
    return C.load_migration(wl.dir)


# --- the loop -------------------------------------------------------------- #

def _cleanup_pid(wl: L.WorkerLayout) -> None:
    pf = wl.pid
    try:
        if pf.exists() and pf.read_text().strip() == str(os.getpid()):
            pf.unlink(missing_ok=True)
    except OSError:
        pass


def main(worker_dir: str) -> int:
    wdir = Path(worker_dir).resolve()
    if not wdir.is_dir():
        print(f"worker dir not found: {wdir}", file=sys.stderr)
        return 2
    wl = L.WorkerLayout(wdir)
    project_dir = wl.project_dir
    project = wl.project
    worker = wl.name
    role = _read_role(wl)
    role_name = role.get("ROLE", "worker")
    scientific = (role_name or "").strip().lower() in L.SCIENTIFIC_ROLES
    if role.get("BACKEND"):
        os.environ["CODEX_BACKEND"] = role["BACKEND"]

    # The role can change between starts. Rewire only Danus-managed harness links
    # and the matching gateway surface before Codex/DSH enters the worker home.
    scaffold.sync_worker_harness(wl, role_name)
    scaffold.write_codex_config(wl, role_name)

    beat = float(os.environ.get("DANUS_ROUND_BEAT", "5"))
    hard_timeout = int(os.environ.get("DANUS_ROUND_HARD_TIMEOUT", "14400"))
    max_rounds = int(os.environ.get("DANUS_MAX_ROUNDS", "0"))
    max_fail = int(os.environ.get("DANUS_MAX_CONSEC_FAILURES", "5"))
    wl.logs.mkdir(parents=True, exist_ok=True)

    prior_status = _read_status(wl)
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
        if _Child.proc is not None:
            _Child.proc.terminate()
        write_status(wl, state="terminated")
        _record_exit("terminated", signal=int(signum))
        _cleanup_pid(wl)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_term)

    write_status(wl, state="running", round=rnd, started_at=time.time(),
                 context_tokens_total=context_tokens_total)
    if ledger is not None:
        current = ledger.current(worker, refresh_stale=True)
        if current is not None:
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
            ledger.update(
                worker=worker,
                state="RUNNING",
                actor="runtime",
                note="worker process started",
            )

    consec_fail = 0
    try:
        while True:
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

            rnd += 1
            log_path = wl.logs / f"round_{rnd}.log"
            round_started = time.time()
            prompt = kickoff(project, worker, role_name)
            write_status(wl, state="running", round=rnd, round_started_at=round_started)
            if scientific:
                rc = run_round(
                    wl, role, prompt, log_path, hard_timeout, context_budget=True,
                )
            else:
                rc = run_round(wl, role, prompt, log_path, hard_timeout)

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
                    # Token usage is normally only final in Codex exec JSON, so
                    # this catches an expensive completed turn before the next one.
                    trigger = C.budget_reason(telemetry, elapsed)
                C.write_checkpoint(
                    wl.dir, role=role_name, round_no=rnd, telemetry=telemetry,
                    trigger=trigger,
                )

                # A completed assignment wins over context rollover: there is no
                # continuation session to migrate into. Also skip migration when an
                # explicit max-rounds cap says this process has no next round.
                if trigger and not outcome and (not round_limit or rnd < round_limit):
                    write_status(wl, state="migrating", context_trigger=trigger)
                    migration = _run_context_migration(
                        wl, role, trigger, log_path, hard_timeout,
                    )
                    migration_log = wl.logs / f"migration_{rnd}.log"
                    migration_tokens = int(C.inspect_log(migration_log).get("tokens") or 0)
                    context_tokens_total += max(0, migration_tokens)
                    C.write_checkpoint(
                        wl.dir, role=role_name, round_no=rnd, telemetry=telemetry,
                        trigger=trigger, migration=migration,
                    )

            if outcome and ledger is not None:
                current = ledger.current(worker, refresh_stale=True)
                if current is not None and current["state"] == "STALE":
                    write_status(
                        wl,
                        state="stopped",
                        progress_state="STALE",
                        ignored_terminal_outcome=outcome,
                    )
                    _record_exit("assignment_stale_after_round")
                    break
                if current is not None:
                    progress_state = "DONE" if outcome == "COMPLETE" else "BLOCKED"
                    ledger.update(
                        worker=worker,
                        state=progress_state,
                        actor="runtime",
                        note=f"agent declared {outcome}",
                        outcome=outcome,
                    )

            status_fields = {
                "state": "stopped" if outcome else "idle",
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
            write_status(wl, **status_fields)

            if outcome:
                _record_exit("terminal_outcome", outcome=outcome, returncode=rc)
                break
            if rc == 127:
                write_status(wl, state="error", error="codex binary not found")
                _record_exit("codex_binary_not_found", returncode=rc)
                return 127
            consec_fail = consec_fail + 1 if rc not in (0, 124, 125) else 0
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
