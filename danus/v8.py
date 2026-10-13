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
