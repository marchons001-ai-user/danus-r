"""V8 adaptive research orchestration, independent of the legacy Plan dispatcher.

One scientific worker runs at a time.  LLMs choose work and communication;
this module persists requests and enforces execution/approval boundaries.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

from danus import codex
from danus.execution import context as codex_context

ROLES = ("researcher", "reviewer", "experimenter")
MODES = ("work", "answer", "experiment", "review_experiment", "final_review")
PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
MAX_TEXT = 12000
LOW_MODEL = "gpt-5.6-luna"
HIGH_MODEL = "gpt-6.1-sol"
LOW_EFFORT = "low"
HIGH_EFFORT = "medium"
DEFAULT_MODELS = {
    "researcher": ("gpt-6.1-sol", "high"),
    "reviewer": ("gpt-6.1-sol", "medium"),
    "experimenter": ("gpt-5.6-luna", "medium"),
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8",
                                     delete=False) as out:
        out.write(content)
        out.flush()
        os.fsync(out.fileno())
        tmp = Path(out.name)
    tmp.replace(path)


def project_root(name: str) -> Path:
    if not PROJECT_ID.fullmatch(name):
        raise ValueError("invalid project name")
    root = Path(os.environ.get("DANUS_RUNTIME", "runtime")).resolve()
    return root / "v8" / name


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.events = self.root / "EVENTS.jsonl"

    @contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".v8.lock").open("a+") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    @contextmanager
    def runner_lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".runner.lock").open("a+") as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("another V8 Supervisor process is already running") from exc
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    def read(self):
        if not self.events.is_file():
            return []
        with self.events.open(encoding="utf-8") as f:
            events = [json.loads(line) for line in f if line.strip()]
        for i, event in enumerate(events):
            if event.get("seq") != i + 1:
                raise ValueError("V8 event log has a missing or reordered entry")
        return events

    def append(self, kind, **fields):
        if not self.events.is_file() and kind != "INIT":
            raise ValueError("project not initialized")
        with self.events.open("a", encoding="utf-8") as fd:
            event = {"seq": len(self.read()) + 1, "at": _now(),
                     "kind": kind, **fields}
            fd.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            fd.flush()
            os.fsync(fd.fileno())
        return event

    def state(self):
        events = self.read()
        if not events or events[0]["kind"] != "INIT":
            raise ValueError("V8 project not initialized")
        base = events[0]
        state = {"goal": base["goal"], "reference": base.get("reference", ""),
                 "tasks": {}, "requests": {}, "proposals": {},
                 "assets": {}, "last": events[-1], "done": False, "halt": "",
                 "final_review": None}
        for e in events[1:]:
            kind = e["kind"]
            if kind == "TASK":
                state["tasks"][e["id"]] = {
                    "id": e["id"], "role": e["role"], "purpose": e["purpose"],
                    "instruction": e["instruction"], "request_id": e.get("request_id", ""),
                    "proposal_id": e.get("proposal_id", ""),
                    "status": "READY", "summary": ""}
            elif kind == "RUNNING":
                state["tasks"][e["task_id"]]["status"] = "RUNNING"
            elif kind == "REQUEST":
                state["requests"][e["id"]] = {
                    "id": e["id"], "from_task": e["from_task"],
                    "from_role": e["from_role"], "to": e["to"],
                    "question": e["question"], "reason": e["reason"],
                    "refs": e.get("refs", []), "status": "OPEN", "answer": ""}
                state["tasks"][e["from_task"]]["status"] = "WAITING"
            elif kind == "REPLY":
                request = state["requests"][e["request_id"]]
                request["status"], request["answer"] = "ANSWERED", e["message"]
                origin = state["tasks"][request["from_task"]]
                if origin["status"] == "WAITING":
                    origin["status"] = "READY"
            elif kind == "RESULT":
                task = state["tasks"][e["task_id"]]
                if e.get("reply_to"):
                    req = state["requests"][e["reply_to"]]
                    req["status"], req["answer"] = "ANSWERED", e["summary"]
                    origin = state["tasks"][req["from_task"]]
                    if origin["status"] == "WAITING":
                        origin["status"] = "READY"
                task["status"] = e["status"]
                task["summary"] = e["summary"]
                if e.get("proposal"):
                    pid = e["proposal"]["id"]
                    state["proposals"][pid] = {"id": pid, "content": e["proposal"]["content"],
                                               "status": "PENDING", "by": e["task_id"]}
                if e.get("review"):
                    review = e["review"]
                    state["proposals"][review["proposal_id"]]["status"] = review["verdict"]
                if task["purpose"] == "final_review":
                    state["final_review"] = {"verdict": e.get("verdict", ""),
                                             "summary": e["summary"], "task_id": e["task_id"]}
            elif kind == "ASSET":
                state["assets"][e["path"]] = {k: e[k] for k in
                    ("path", "sha256", "bytes", "task_id", "description")}
            elif kind == "FINISH":
                state["done"] = True
            elif kind == "RESET":
                state["tasks"][e["task_id"]]["status"] = "READY"
            elif kind == "HALT":
                state["halt"] = e["reason"]
            elif kind == "UNHALT":
                state["halt"] = ""
        return state

    def publish(self):
        state = self.state()
        lines = ["# Project State (generated; EVENTS.jsonl is authoritative)", "",
                 "## Fixed research goal", state["goal"], "",
                 "## Tasks"]
        for t in state["tasks"].values():
            lines.append(f"- {t['id']} {t['role']} / {t['purpose']} / {t['status']}: "
                         f"{t['summary'][:280] or t['instruction'][:280]}")
        lines += ["", "## Requests"]
        for r in state["requests"].values():
            lines.append(f"- {r['id']} {r['from_role']} -> {r['to']} "
                         f"{r['status']}: {r['question'][:250]}")
        lines += ["", "## Experiment approvals"]
        for p in state["proposals"].values():
            lines.append(f"- {p['id']}: {p['status']}")
        lines += ["", "## Assets"]
        for asset in state["assets"].values():
            lines.append(f"- {asset['path']} sha256={asset['sha256']} "
                         f"from {asset['task_id']}")
        lines += ["", f"Done: {state['done']}", f"Halt: {state['halt']}"]
        _write(self.root / "PROJECT_STATE.md", "\n".join(lines) + "\n")
        _write(self.root / "ASSET_INDEX.json",
               json.dumps(state["assets"], ensure_ascii=False, indent=2) + "\n")
        return state


def _limit_text(value, label, *, max_len=MAX_TEXT):
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise ValueError(f"{label} must be nonempty text <= {max_len} characters")
    return value.strip()


def _asset_path(store, path):
    if not isinstance(path, str) or not path or Path(path).is_absolute():
        raise ValueError("asset must be a workspace-relative path")
    root = (store.root / "workspace").resolve()
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise ValueError(f"asset missing or outside workspace: {path}")
    return candidate


def verify_assets(store: Store):
    for asset in store.state()["assets"].values():
        p = _asset_path(store, asset["path"])
        if (p.stat().st_size != asset["bytes"]
                or hashlib.sha256(p.read_bytes()).hexdigest() != asset["sha256"]):
            raise PermissionError(f"previously recorded artifact modified: {asset['path']}")


def initialize(store: Store, goal: str, reference=""):
    goal = _limit_text(goal, "goal")
    if store.events.exists():
        raise ValueError("project already exists")
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "workspace").mkdir(exist_ok=True)
    with store.lock():
        store.append("INIT", goal=goal, reference=str(reference))
        store.publish()


def _proposal(proposal):
    text = _limit_text(proposal, "proposal", max_len=16000)
    return {"id": _hash({"proposal": text})[:16], "content": text}


def _task_id(store):
    return "task-" + str(len(store.read()) + 1)


def _req_id(store):
    return "req-" + str(len(store.read()) + 1)


def _check_dispatch(state, role, purpose, proposal_id, request_id):
    if role not in ROLES or purpose not in MODES:
        raise ValueError("invalid role or task purpose")
    if purpose in {"experiment", "review_experiment"} and role not in (
        {"experiment": "experimenter", "review_experiment": "reviewer"}[purpose],):
        raise PermissionError("purpose requires its designated role")
    if purpose == "final_review" and role != "reviewer":
        raise PermissionError("final review must be independent Reviewer")
    if purpose == "experiment":
        p = state["proposals"].get(proposal_id)
        if not p or p["status"] != "APPROVE":
            raise PermissionError("experiment requires independent approved exact proposal")
    if purpose == "review_experiment":
        p = state["proposals"].get(proposal_id)
        if not p or p["status"] != "PENDING":
            raise PermissionError("review requires pending experiment proposal")
    if request_id:
        req = state["requests"].get(request_id)
        if not req or req["status"] != "OPEN":
            raise ValueError("request is not open")
        if req["to"] not in (role, "supervisor"):
            raise PermissionError("request is directed to a different role")


def apply_decision(store: Store, choice: dict, level: str):
    state = store.state()
    if state["done"] or state["halt"]:
        raise ValueError("project is stopped")
    if any(t["status"] == "RUNNING" for t in state["tasks"].values()):
        raise RuntimeError("a worker is running; reconcile before re-dispatch")
    action = choice.get("action")
    reason = _limit_text(choice.get("reason"), "reason", max_len=1800)
    if action == "dispatch":
        role = choice.get("role")
        purpose = choice.get("purpose", "work")
        proposal_id = choice.get("proposal_id", "") or ""
        request_id = choice.get("request_id", "") or ""
        task_id = choice.get("task_id", "") or ""
        if task_id:
            current = state["tasks"].get(task_id)
            if not current or current["status"] != "READY":
                raise ValueError("only READY tasks may resume")
            if any(choice.get(k) not in (None, "", current[k])
                   for k in ("role", "purpose", "request_id", "proposal_id")):
                raise ValueError("resumed task identity cannot be changed")
            store.append("DECISION", level=level, action="resume", reason=reason,
                         task_id=task_id)
            return task_id
        instruction = _limit_text(choice.get("task"), "task")
        _check_dispatch(state, role, purpose, proposal_id, request_id)
        if purpose == "experiment":
            instruction = (
                "Execute ONLY the independently approved experiment proposal "
                + proposal_id + ". Use the approved design, controls and metrics "
                "without modification. Record exact runtime, negative results and "
                "workspace-relative output files. Do not alter external repositories."
            )
        elif purpose == "review_experiment":
            instruction = (
                "Independently review the exact pending experiment proposal "
                + proposal_id + ". State APPROVE, REVISE or REJECT with reasoning."
            )
        if any(t["role"] == role and t["purpose"] == purpose
               and t["instruction"] == instruction and t["status"] != "BLOCKED"
               for t in state["tasks"].values()):
            raise ValueError("identical task already exists")
        task_id = _task_id(store)
        store.append("DECISION", level=level, action=action, reason=reason, task_id=task_id)
        store.append("TASK", id=task_id, role=role, purpose=purpose,
                     instruction=instruction, proposal_id=proposal_id, request_id=request_id)
        return task_id
    if action == "answer":
        request_id = choice.get("request_id", "")
        request = state["requests"].get(request_id)
        if not request or request["status"] != "OPEN":
            raise ValueError("cannot answer nonexistent or answered request")
        message = _limit_text(choice.get("message"), "message")
        store.append("DECISION", level=level, action=action, reason=reason)
        store.append("REPLY", request_id=request_id, by="supervisor", message=message)
        return None
    if action == "finish":
        if any(r["status"] == "OPEN" for r in state["requests"].values()):
            raise PermissionError("cannot finish while requests remain open")
        if not state["final_review"] or state["final_review"]["verdict"] not in (
            "ACCEPT", "INCONCLUSIVE"):
            raise PermissionError("independent final review required before finish")
        store.append("DECISION", level=level, action=action, reason=reason)
        store.append("FINISH", reason=reason)
        return None
    if action == "block":
        if level != "high":
            raise PermissionError("only high Supervisor may declare final blocker")
        store.append("HALT", reason=reason)
        return None
    if action == "escalate":
        if level != "low":
            raise ValueError("high Supervisor cannot escalate to itself")
        store.append("DECISION", level=level, action="escalate", reason=reason)
        return "HIGH"
    raise ValueError("unsupported Supervisor action")


def _worker_result(store: Store, task_id: str, result: dict):
    state = store.state()
    task = state["tasks"][task_id]
    if task["status"] != "RUNNING":
        raise ValueError("result does not belong to the running task")
    action = result.get("action")
    summary = _limit_text(result.get("summary"), "worker summary", max_len=6000)
    if action == "request":
        target = result.get("to", "supervisor")
        if target not in (*ROLES, "supervisor"):
            raise ValueError("invalid recipient")
        question = _limit_text(result.get("question"), "question", max_len=3000)
        reason = _limit_text(result.get("reason"), "request reason", max_len=1800)
        refs = result.get("refs", [])
        if not isinstance(refs, list) or len(refs) > 12 or any(
                not isinstance(x, str) or len(x) > 256 for x in refs):
            raise ValueError("invalid asset references")
        rid = _req_id(store)
        store.append("REQUEST", id=rid, from_task=task_id, from_role=task["role"],
                     to=target, question=question, reason=reason, refs=refs)
        return
    if action not in ("complete", "blocked"):
        raise ValueError("worker must complete, request or block")
    proposal, review, verdict = None, None, ""
    if action == "complete":
        if result.get("proposal") is not None:
            if task["role"] != "researcher":
                raise PermissionError("only Researcher may propose an experiment")
            proposal = _proposal(result["proposal"])
            if proposal["id"] in state["proposals"]:
                raise ValueError("experiment proposal was already submitted")
        if task["purpose"] == "review_experiment":
            if task["role"] != "reviewer":
                raise PermissionError("experiment approval requires independent Reviewer")
            verdict = result.get("verdict")
            if verdict not in ("APPROVE", "REVISE", "REJECT"):
                raise ValueError("experiment review requires APPROVE/REVISE/REJECT")
            proposal_id = task["proposal_id"]
            if state["proposals"][proposal_id]["status"] != "PENDING":
                raise ValueError("proposal was already reviewed")
            review = {"proposal_id": proposal_id, "verdict": verdict}
        if task["purpose"] == "final_review":
            verdict = result.get("verdict")
            if verdict not in ("ACCEPT", "INCONCLUSIVE", "REVISE", "REJECT"):
                raise ValueError("final review requires a reviewer verdict")
    paths = result.get("artifacts", [])
    if not isinstance(paths, list) or len(paths) > 24 or not all(
            isinstance(x, str) and x for x in paths):
        raise ValueError("artifacts must be workspace-relative paths")
    if paths and task["role"] != "experimenter":
        raise PermissionError("only Experimenter may register new files as artifacts")
    assets = []
    for item in paths:
        path = _asset_path(store, item)
        raw = path.read_bytes()
        known = state["assets"].get(item)
        digest = hashlib.sha256(raw).hexdigest()
        if known and known["sha256"] != digest:
            raise PermissionError("frozen artifact was overwritten")
        assets.append({"path": item, "sha256": digest, "bytes": len(raw),
                       "task_id": task_id,
                       "description": str(result.get("artifact_description") or "")[:500]})
    if action == "complete" and task["request_id"]:
        req = state["requests"][task["request_id"]]
        if req["status"] != "OPEN":
            raise RuntimeError("assigned request was answered before worker finished")
    store.append("RESULT", task_id=task_id,
                 status="COMPLETE" if action == "complete" else "BLOCKED",
                 summary=summary, proposal=proposal, review=review, verdict=verdict,
                 reply_to=task["request_id"] if action == "complete" else "")
    for asset in assets:
        store.append("ASSET", **asset)


def _extract_json(message):
    content = str(message or "").strip()
    fence = chr(96) * 3
    if content.startswith(fence):
        lines = content.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == fence:
            content = "\n".join(lines[1:-1]).strip()
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("model must return one JSON object")
    return value


def _call(model, effort, prompt, workdir, log_path, *, writable=False, timeout=1200):
    """Reuse Danus's non-escalating Codex command and log telemetry parser."""
    codex_bin = codex.resolve_bin()
    cmd = codex.exec_cmd(
        codex_bin, model, effort, "-C", str(workdir),
        "--skip-git-repo-check", "--sandbox",
        "workspace-write" if writable else "read-only", "--json", prompt,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, cwd=str(workdir), stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT,
                                env=codex.subprocess_env(codex_bin),
                                start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise RuntimeError(f"model invocation timed out at {timeout}s")
        except KeyboardInterrupt:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            raise
    telemetry = codex_context.inspect_log(log_path)
    if code:
        raise RuntimeError(f"model exited with {code}; see {log_path}")
    return _extract_json(telemetry.get("last_agent_message"))


def _brief_state(store: Store, *, last=18):
    state = store.state()
    recent = [{k: v for k, v in e.items()
               if k not in ("instruction", "content")}
              for e in store.read()[-last:] if e["kind"] != "INIT"]
    return {"goal": state["goal"], "reference": state["reference"],
            "tasks": list(state["tasks"].values()),
            "requests": list(state["requests"].values()),
            "proposals": list(state["proposals"].values()),
            "assets": list(state["assets"].values()), "final_review": state["final_review"],
            "recent_events": recent}


_SUPERVISOR_POLICY = """
You are the Danus-R V8 adaptive scientific Supervisor.
The roles are researcher, reviewer and experimenter. One scientific role runs
at a time. You choose the next task dynamically, not from a fixed DAG.
Research Plan is high-level and stable; the goal is immutable.
Workers may ask any one role or Supervisor one question at a time, and continue
after a reply. Resolve the oldest material open question before making new work.
Prefer existing verified asset records and reports to re-reading raw data.
Never guess asset IDs or bypass Reviewer experiment approval. Never claim a
scientific result is verified without actual evidence and independent review.
Negative and inconclusive findings are acceptable; don't chase positive results.
The runtime, not you, enforces authority.
Return exactly one JSON object and no markdown:
New work: {"action":"dispatch","role":"researcher|reviewer|experimenter",
"purpose":"work|answer|experiment|review_experiment|final_review",
"task":"precise assignment","request_id":"","proposal_id":"","reason":"why"}
Resume an existing READY task: {"action":"dispatch","task_id":"task-N",
"reason":"why"}
Answer a pending request using reliable recorded information:
{"action":"answer","request_id":"req-N","message":"answer","reason":"why"}
If LOW cannot find a legal useful next step:
{"action":"escalate","reason":"what cannot be resolved"}
Finish only after independent final_review:
{"action":"finish","reason":"evidence-bounded conclusion"}
If HIGH finds no legal route: {"action":"block","reason":"precise blocker"}
For an experiment, Researcher must first return a proposal, then Reviewer
reviews its exact proposal_id, only then dispatch experimenter with
purpose=experiment and same proposal_id. Finally ask Reviewer to independently
audit the evidence and interpretation before finishing.
"""

_WORKER_POLICY = """
You are one assigned Danus-R scientific worker. Execute only your task.
You can ask ONE question to researcher, reviewer, experimenter or supervisor
at a time. State WHY the answer is needed. After it returns, reconsider next step.
Do not modify the fixed research goal. A negative scientific result is valid.
Keep experiment costs small. Never modify external reference repositories or
previously frozen artifacts. Prefer summaries to full raw-data re-interpretation.
Never fabricate successful tool calls, citations or file artifacts.
Researcher may propose a specific small experiment but must not execute it.
Reviewer independently checks proposed experiment and scientific conclusions.
Experimenter only executes the exact Reviewer-approved experimental plan.
Return exactly one JSON object (no markdown):
For one question:
{"action":"request","to":"researcher|reviewer|experimenter|supervisor",
"question":"focused question","reason":"why it is needed","refs":[],
"summary":"work done and what is missing"}
For a completed task:
{"action":"complete","summary":"detailed evidence-backed outcome","artifacts":[],
"proposal":null,"verdict":null}
For a blocked task: {"action":"blocked","summary":"specific blocker"}
If Researcher proposes an experiment, set proposal to complete standalone
experiment plan text: question, exact conditions, dataset, metric, controls,
runtime/resource bounds, acceptance and negative/inconclusive outcome criteria.
For Reviewer purpose=review_experiment, use verdict APPROVE/REVISE/REJECT.
For Reviewer purpose=final_review, use verdict ACCEPT/INCONCLUSIVE/REVISE/REJECT.
Experimenter may write files only for purpose=experiment, inside this workspace.
Report generated artifacts as workspace-relative file paths.
"""


def _supervisor(store: Store, level, error=""):
    prompt = (_SUPERVISOR_POLICY + "\nLEVEL: " + level +
              "\nPrevious decision error: " + error +
              "\nTrusted project status:\n" +
              json.dumps(_brief_state(store), ensure_ascii=False, indent=2))
    model = os.environ.get("DANUS_V8_LOW_MODEL", LOW_MODEL) if level == "low" else (
        os.environ.get("DANUS_V8_HIGH_MODEL", HIGH_MODEL))
    effort = LOW_EFFORT if level == "low" else HIGH_EFFORT
    return _call(model, effort, prompt, store.root / "workspace",
                 store.root / "logs" / f"supervisor-{len(store.read())}-{level}.jsonl",
                 writable=False, timeout=180)


def _worker(store: Store, task_id, *, timeout=1200):
    with store.lock():
        verify_assets(store)
        state = store.state()
        task = state["tasks"][task_id]
        if task["status"] != "READY":
            raise ValueError("task is not ready")
        if any(t["status"] == "RUNNING" for t in state["tasks"].values()):
            raise ValueError("another worker is active")
        if task["purpose"] == "experiment":
            _check_dispatch(state, task["role"], task["purpose"],
                            task["proposal_id"], task["request_id"])
        store.append("RUNNING", task_id=task_id)
        store.publish()
    state = store.state()
    task = state["tasks"][task_id]
    req = state["requests"].get(task["request_id"])
    replies = [r for r in state["requests"].values()
               if r["from_task"] == task_id and r["status"] == "ANSWERED"]
    payload = {
        "goal": state["goal"], "reference": state["reference"], "task": task,
        "request_to_answer": req,
        "replies_to_your_previous_questions": replies,
        "approved_experiment_proposal": state["proposals"].get(task["proposal_id"]),
        "known_assets": list(state["assets"].values()),
        "recent_completed_work": [
            {"role": t["role"], "purpose": t["purpose"],
             "summary": t["summary"][:3000]}
            for t in state["tasks"].values() if t["status"] == "COMPLETE"
        ][-10:],
    }
    prompt = _WORKER_POLICY + "\nCurrent assignment:\n" + json.dumps(
        payload, ensure_ascii=False, indent=2)
    role = task["role"]
    model, effort = DEFAULT_MODELS[role]
    model = os.environ.get(f"DANUS_V8_{role.upper()}_MODEL", model)
    effort = os.environ.get(f"DANUS_V8_{role.upper()}_EFFORT", effort)
    log = store.root / "logs" / f"{task_id}-{len(store.read())}.jsonl"
    try:
        reply = _call(model, effort, prompt, store.root / "workspace", log,
                      writable=role == "experimenter" and task["purpose"] == "experiment",
                      timeout=timeout)
        with store.lock():
            _worker_result(store, task_id, reply)
            verify_assets(store)
            store.publish()
    except Exception as exc:
        with store.lock():
            state = store.state()
            if state["tasks"][task_id]["status"] == "RUNNING":
                store.append("RESULT", task_id=task_id, status="BLOCKED",
                             summary=f"execution/validation error: {type(exc).__name__}: {exc}",
                             proposal=None, review=None, verdict="")
                store.publish()
        raise


def step(store: Store, *, worker_timeout=1200):
    with store.lock():
        verify_assets(store)
        state = store.state()
        if state["done"] or state["halt"]:
            return "STOP"
        if any(t["status"] == "RUNNING" for t in state["tasks"].values()):
            raise RuntimeError("interrupted worker must be reconciled before continuing")
    error = ""
    force_high = False
    for attempt in range(3):
        level = "high" if force_high or attempt == 2 else "low"
        try:
            decision = _supervisor(store, level, error)
            with store.lock():
                selected = apply_decision(store, decision, level)
                store.publish()
            if selected == "HIGH":
                error = "Low Supervisor voluntarily escalated"
                force_high = True
                continue
            if isinstance(selected, str):
                _worker(store, selected, timeout=worker_timeout)
            return selected or "CONTROL"
        except (ValueError, KeyError, PermissionError, RuntimeError,
                json.JSONDecodeError, OSError, subprocess.SubprocessError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            with store.lock():
                store.append("DENIED", level=level, error=error)
                store.publish()
    with store.lock():
        store.append("HALT", reason="No legal decision from either Supervisor: " + error)
        store.publish()
    return "HALT"


def status(store: Store):
    state = store.state()
    print(json.dumps({
        "goal": state["goal"], "done": state["done"], "halt": state["halt"],
        "tasks": [{"id": t["id"], "role": t["role"], "status": t["status"],
                   "purpose": t["purpose"], "summary": t["summary"][:300]}
                  for t in state["tasks"].values()],
        "requests": list(state["requests"].values()),
        "proposals": [{k: v for k, v in p.items() if k != "content"}
                      for p in state["proposals"].values()],
        "asset_count": len(state["assets"]), "final_review": state["final_review"],
    }, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Danus-R V8 adaptive runner")
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new")
    new.add_argument("project")
    new.add_argument("--goal-file", type=Path, required=True)
    new.add_argument("--reference", default="")
    for command in ("run", "step", "status", "recover"):
        sub = commands.add_parser(command)
        sub.add_argument("project")
        if command == "run":
            sub.add_argument("--max-steps", type=int, default=0)
            sub.add_argument("--worker-timeout", type=int, default=1200)
        if command == "recover":
            sub.add_argument("--task-id", required=True)
    args = parser.parse_args(argv)
    store = Store(project_root(args.project))
    if args.command == "new":
        initialize(store, args.goal_file.read_text(encoding="utf-8"),
                   reference=args.reference)
        print(store.root)
        return
    if args.command == "status":
        status(store)
        return
    if args.command == "recover":
        with store.lock():
            task = store.state()["tasks"].get(args.task_id)
            if not task or task["status"] != "RUNNING":
                raise ValueError("only interrupted RUNNING tasks can be recovered")
            if task["purpose"] == "experiment":
                raise PermissionError("cannot blindly rerun an interrupted experiment")
            store.append("RESET", task_id=args.task_id,
                         reason="operator reconciliation of read-only task")
            store.publish()
        return
    limit = 1 if args.command == "step" else args.max_steps
    if limit < 0:
        raise ValueError("negative step limit")
    iteration = 0
    with store.runner_lock():
        while not limit or iteration < limit:
            outcome = step(store, worker_timeout=getattr(args, "worker_timeout", 1200))
            print(f"V8 step {iteration + 1}: {outcome}", flush=True)
            iteration += 1
            if outcome in ("STOP", "HALT"):
                break


if __name__ == "__main__":
    main()
