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
from danus import v8_validation
from danus import v8_inputs

ROLES = ("researcher", "reviewer", "experimenter")
MODES = ("work", "answer", "experiment", "interpret", "review_experiment", "final_review")
PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
MAX_TEXT = 12000
# Centralized HARD field limits. The prompt is generated with 70% headroom.
OUTPUT_TEXT_LIMITS = {
    "supervisor task / reply": MAX_TEXT,
    "supervisor decision reason": 1800,
    "researcher experiment proposal": 32768,
    "worker summary": 32000,
    "worker question": 3000,
    "worker request reason": 1800,
    "review issue field (each)": 1500,
    "review resolution field (each)": 1500,
    "review accepted deviation field (each)": 1500,
    "artifact description": 500,
    "request reference (each)": 256,
}

def _output_budget_guide():
    rows = ["Field (each string) | HARD characters | aim <= 70%",
            "--- | ---: | ---:"]
    for field, hard in OUTPUT_TEXT_LIMITS.items():
        rows.append(f"{field} | {hard} | {hard * 7 // 10}")
    return (
        "\nResponse length budget (Python len, Unicode characters, not tokens):\n"
        + "\n".join(rows)
        + "\nThe 70% figures are ceilings, NOT quotas. Be much shorter when possible; "
          "keep the earlier conservative preferred summary/proposal sizes. "
          "Required strings must contain substantive non-whitespace text. "
          "Never invent evidence to satisfy a required field.\n"
        + "Reviewer final_review REVISE: EVERY item in issues MUST contain all "
          "five keys: problem, impact, required_action, target_role, evidence. "
          "Each of the FOUR text fields must be nonempty; target_role is "
          "researcher or experimenter. Evidence should cite an actual file, "
          "observed output, or concrete missing record; an unsupported suspicion "
          "is not evidence. Example: "
          '{"problem":"Command omitted from run.log","impact":"Cannot reproduce invocation",'
          '"required_action":"Register a new log of an authorized rerun",'
          '"target_role":"experimenter",'
          '"evidence":"Inspected run.log: exit code present, executed command absent"}. '
          "Evidence may be one nonblank string OR a list of 1-8 nonblank "
          "strings (e.g. cited file paths); total combined limit is 1500 "
          "characters, aim <= 1050. No fabricated evidence. "
          "For resolved_issues, give id, status, reason AND evidence. "
          "For accepted_deviations, give item, reason AND evidence. "
          "If no defensible evidence exists, investigate first or ask a focused question.\n"
        + "At most 12 issues/resolutions/deviations per list (prefer <= 8), "
          "16 required_checks (prefer <= 11), 12 refs (prefer <= 8), "
          "24 output artifact paths (prefer <= 16).\n"
    )

LOW_MODEL = "gpt-5.6-luna"
HIGH_MODEL = "gpt-6.1-sol"
LOW_EFFORT = "low"
HIGH_EFFORT = "medium"
DEFAULT_MODELS = {
    "researcher": ("gpt-6.1-sol", "high"),
    "reviewer": ("gpt-6.1-sol", "medium"),
    "experimenter": ("gpt-6.1-sol", "medium"),
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
                 "final_review": None, "material_seq": 0,
                 "last_experiment_seq": 0, "last_interpret_seq": 0,
                 "validations": {}, "latest_validation": None, "inputs": None,
                 "review_issues": {}, "issue_merges": {}}
        for e in events[1:]:
            kind = e["kind"]
            if kind == "TASK":
                state["tasks"][e["id"]] = {
                    "id": e["id"], "role": e["role"], "purpose": e["purpose"],
                    "instruction": e["instruction"], "request_id": e.get("request_id", ""),
                    "proposal_id": e.get("proposal_id", ""),
                    "status": "READY", "summary": "",
                    "review_basis_seq": e.get("review_basis_seq", 0)}
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
                if e["status"] == "COMPLETE":
                    if task["role"] in ("researcher", "experimenter") or (
                            task["purpose"] == "review_experiment"):
                        state["material_seq"] = e["seq"]
                    if task["purpose"] == "experiment":
                        state["last_experiment_seq"] = e["seq"]
                    if task["purpose"] == "interpret":
                        state["last_interpret_seq"] = e["seq"]
                if e.get("issue_merge"):
                    group = e["issue_merge"]
                    state["issue_merges"][group["id"]] = {
                        **group, "status": "PENDING", "by": e["task_id"]}
                if e.get("proposal"):
                    pid = e["proposal"]["id"]
                    state["proposals"][pid] = {"id": pid, "content": e["proposal"]["content"],
                                               "required_checks": e["proposal"].get("required_checks", []),
                                               "status": "PENDING", "by": e["task_id"],
                                               "issue_merge_id": e["proposal"].get("issue_merge_id", "")}
                if e.get("review"):
                    review = e["review"]
                    state["proposals"][review["proposal_id"]]["status"] = review["verdict"]
                if task["purpose"] == "final_review":
                    feedback = e.get("review_feedback") or {}
                    for item in feedback.get("resolutions", []):
                        state["review_issues"][item["id"]].update(
                            status=item["status"], resolution=item["reason"],
                            evidence=item["evidence"], resolved_by=e["task_id"])
                        if item.get("canonical_id"):
                            state["review_issues"][item["id"]]["canonical_id"] = item["canonical_id"]
                    for item in feedback.get("issues", []):
                        state["review_issues"][item["id"]] = {
                            **item, "status": "OPEN", "created_by": e["task_id"]}
                    state["final_review"] = {"verdict": e.get("verdict", ""),
                                             "summary": e["summary"], "task_id": e["task_id"],
                                             "basis_seq": task["review_basis_seq"],
                                             "feedback": feedback}
            elif kind == "ISSUE_MERGE_REVIEW":
                state["issue_merges"][e["id"]]["status"] = e["verdict"]
            elif kind == "ASSET":
                state["material_seq"] = e["seq"]
                state["assets"][e["path"]] = {k: e[k] for k in
                    ("path", "sha256", "bytes", "task_id", "description")}
            elif kind == "INPUT_STAGE":
                if state["inputs"] is not None:
                    raise ValueError("V8 input manifest cannot be staged twice")
                state["inputs"] = {
                    "manifest_path": e["manifest_path"],
                    "manifest_sha256": e["manifest_sha256"],
                    "dataset_label": e["dataset_label"],
                    "files": e["files"],
                }
                state["material_seq"] = e["seq"]
            elif kind == "VALIDATION":
                report = {"task_id": e["task_id"], "proposal_id": e["proposal_id"],
                          "status": e["status"], "checks": e["checks"],
                          "reason": e["reason"], "seq": e["seq"]}
                state["validations"][e["task_id"]] = report
                state["latest_validation"] = report
            elif kind == "FINISH":
                state["done"] = True
            elif kind == "RESET":
                state["tasks"][e["task_id"]]["status"] = "READY"
            elif kind == "HALT":
                state["halt"] = e["reason"]
            elif kind == "UNHALT":
                state["halt"] = ""
        # Only the most recently assigned experiment may qualify as current
        # evidence. A newer READY/RUNNING/BLOCKED experiment cannot silently
        # inherit an earlier run's PASSED certificate.
        newest = next((t for t in reversed(list(state["tasks"].values()))
                       if t["purpose"] == "experiment"), None)
        if newest and (not state["latest_validation"] or
                       state["latest_validation"]["task_id"] != newest["id"]):
            state["latest_validation"] = {
                "task_id": newest["id"], "proposal_id": newest["proposal_id"],
                "status": "INCOMPLETE", "checks": [],
                "reason": f"newest experiment {newest['id']} ({newest['status']}) "
                          "has no independent validation report", "seq": 0,
            }
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
        lines += ["", "## Input provenance"]
        inp = state["inputs"]
        if inp:
            lines.append(f"Staged {inp['dataset_label']}: {inp['manifest_path']} "
                         f"sha256={inp['manifest_sha256']}")
            for entry in inp["files"]:
                lines.append(f"- {entry['path']} sha256={entry['sha256']} "
                             f"source={entry['source_path']}")
        else:
            lines.append("No operator-staged provenance; do not infer input origin")
        lines += ["", "## Open Reviewer corrections"]
        open_issues = [x for x in state["review_issues"].values()
                       if x["status"] == "OPEN"]
        for issue in open_issues:
            lines.append(f"- {issue['id']} -> {issue['target_role']}: "
                         f"{issue['problem']} | fix: {issue['required_action']}")
        if not open_issues:
            lines.append("None")
        lines += ["", "## Correction work grouping (not Reviewer resolution)"]
        for group in state["issue_merges"].values():
            lines.append(f"- {group['id']} {group['status']}: "
                         f"{', '.join(group['issue_ids'])} | {group['summary']}")
        lines += ["", "## Assets"]
        for asset in state["assets"].values():
            lines.append(f"- {asset['path']} sha256={asset['sha256']} "
                         f"from {asset['task_id']}")
        validation = state["latest_validation"]
        lines += ["", "## Latest experiment validation",
                  (f"{validation['status']} / {validation['task_id']}: "
                   f"{validation['reason']}" if validation else "No validated experiment"),
                  "", f"Done: {state['done']}", f"Halt: {state['halt']}"]
        _write(self.root / "PROJECT_STATE.md", "\n".join(lines) + "\n")
        _write(self.root / "ASSET_INDEX.json",
               json.dumps(state["assets"], ensure_ascii=False, indent=2) + "\n")
        return state


def _limit_text(value, label, *, max_len=MAX_TEXT):
    if not isinstance(value, str):
        detail = "missing or non-string value"
    elif not value.strip():
        detail = "blank or whitespace-only value"
    elif len(value) > max_len:
        detail = f"actual length {len(value)} exceeds {max_len}"
    else:
        return value.strip()
    raise ValueError(f"{label} must be nonempty text <= {max_len} characters ({detail})")


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


def _proposal(proposal, required_checks=None, issue_merge_id=""):
    text = _limit_text(proposal, "proposal",
                       max_len=OUTPUT_TEXT_LIMITS["researcher experiment proposal"])
    checks = v8_validation.normalize_checks(required_checks)
    identity = {"proposal": text}
    if checks:
        identity["required_checks"] = checks
    if issue_merge_id:
        identity["issue_merge_id"] = issue_merge_id
    return {"id": _hash(identity)[:16], "content": text,
            "required_checks": checks, "issue_merge_id": issue_merge_id}


def _review_evidence(value, label):
    """Accept a citation string or a small list of literal citation strings."""
    limit = OUTPUT_TEXT_LIMITS["review issue field (each)"]
    if isinstance(value, list):
        if not 1 <= len(value) <= 8:
            raise ValueError(f"{label} must list 1-8 nonempty evidence strings")
        value = "\n".join(_limit_text(part, f"{label}[{i}]", max_len=limit)
                          for i, part in enumerate(value, 1))
    return _limit_text(value, label, max_len=limit)


def _issue_merge(task_id, value, state):
    """Researcher grouping request; does not change Reviewer issue status."""
    if not isinstance(value, dict):
        raise ValueError("issue_merge must be an object")
    ids = value.get("issue_ids")
    if (not isinstance(ids, list) or not 2 <= len(ids) <= 12
            or any(not isinstance(x, str) for x in ids)
            or len(set(ids)) != len(ids)
            or any(state["review_issues"].get(x, {}).get("status") != "OPEN"
                   for x in ids)):
        raise ValueError("issue_merge requires 2-12 distinct existing OPEN issue IDs")
    for old in state["issue_merges"].values():
        if old["status"] in ("PENDING", "APPROVE") and set(old["issue_ids"]) == set(ids):
            raise ValueError("same active issue grouping already proposed")
    return {
        "id": f"{task_id}/merge",
        "issue_ids": ids,
        "summary": _limit_text(value.get("summary"), "merge summary", max_len=1500),
        "reason": _limit_text(value.get("reason"), "merge reason", max_len=1500),
    }


def _review_feedback(task_id, verdict, result, state):
    """Bounded Reviewer-owned correction and reasoned-waiver records."""
    raw_issues = result.get("issues") or []
    raw_resolutions = result.get("resolved_issues") or []
    raw_deviations = result.get("accepted_deviations") or []
    if any(not isinstance(items, list) or len(items) > 12
           for items in (raw_issues, raw_resolutions, raw_deviations)):
        raise ValueError("Reviewer feedback must contain lists of at most 12 entries")
    if verdict != "REVISE" and raw_issues:
        raise ValueError("new blocking issues require REVISE")
    issues, resolutions, deviations, handled = [], [], [], set()
    for i, item in enumerate(raw_issues, 1):
        if not isinstance(item, dict):
            raise ValueError("review issue must be an object")
        role = item.get("target_role")
        if role not in ("researcher", "experimenter"):
            raise ValueError("review issue requires researcher/experimenter target_role")
        problem = _limit_text(item.get("problem"), "issue problem",
                              max_len=OUTPUT_TEXT_LIMITS["review issue field (each)"])
        # A repeated 'old issue remains OPEN' finding is not a new issue.
        if any(re.match(r"^\s*(?:OPEN:\s*)?" + re.escape(old_id) +
                        r"\s+remains\s+OPEN\b", problem, re.IGNORECASE)
               for old_id, old in state["review_issues"].items()
               if old["status"] == "OPEN"):
            raise ValueError("Existing OPEN issue restated as new; use its original ID")
        action = _limit_text(item.get("required_action"), "issue required_action",
                             max_len=OUTPUT_TEXT_LIMITS["review issue field (each)"])
        if (any(old["status"] == "OPEN" and old["problem"].casefold() == problem.casefold()
                and old["required_action"].casefold() == action.casefold()
                for old in state["review_issues"].values())
                or any(old["problem"].casefold() == problem.casefold()
                       and old["required_action"].casefold() == action.casefold()
                       for old in issues)):
            raise ValueError("Identical issue restated as new; reuse the original issue ID")
        issues.append({
            "id": f"{task_id}/issue-{i}", "target_role": role,
            "problem": problem, "required_action": action,
            "impact": _limit_text(item.get("impact"), "issue impact",
                                   max_len=OUTPUT_TEXT_LIMITS["review issue field (each)"]),
            "evidence": _review_evidence(item.get("evidence"), "issue evidence"),
        })
    for item in raw_resolutions:
        if not isinstance(item, dict):
            raise ValueError("resolution must be an object")
        issue_id = item.get("id")
        if (not isinstance(issue_id, str) or issue_id in handled
                or state["review_issues"].get(issue_id, {}).get("status") != "OPEN"):
            raise ValueError("can only resolve each existing OPEN review issue once")
        handled.add(issue_id)
        disposition = item.get("status")
        if disposition not in ("RESOLVED", "WAIVED", "DUPLICATE"):
            raise ValueError("review resolution needs RESOLVED, WAIVED or DUPLICATE")
        canonical_id = item.get("canonical_id")
        if disposition == "DUPLICATE":
            if (not isinstance(canonical_id, str) or canonical_id == issue_id
                    or canonical_id in handled
                    or state["review_issues"].get(canonical_id, {}).get("status") != "OPEN"):
                raise ValueError("DUPLICATE requires a different existing OPEN canonical_id")
        elif canonical_id is not None:
            raise ValueError("canonical_id is only valid for DUPLICATE")
        resolutions.append({
            "id": issue_id, "status": disposition,
            **({"canonical_id": canonical_id} if disposition == "DUPLICATE" else {}),
            "reason": _limit_text(item.get("reason"), "resolution reason",
                                  max_len=OUTPUT_TEXT_LIMITS["review resolution field (each)"]),
            "evidence": _review_evidence(item.get("evidence"), "resolution evidence"),
        })
    if any(item["status"] == "DUPLICATE" and item["canonical_id"] in handled
           for item in resolutions):
        raise ValueError("DUPLICATE canonical issue must remain OPEN after this review")
    for item in raw_deviations:
        if not isinstance(item, dict):
            raise ValueError("accepted deviation must be an object")
        deviations.append({
            "item": _limit_text(item.get("item"), "deviation item",
                                max_len=OUTPUT_TEXT_LIMITS["review accepted deviation field (each)"]),
            "reason": _limit_text(item.get("reason"), "deviation reason",
                                  max_len=OUTPUT_TEXT_LIMITS["review accepted deviation field (each)"]),
            "evidence": _review_evidence(item.get("evidence"), "deviation evidence"),
        })
    remaining = [x["id"] for x in state["review_issues"].values()
                 if x["status"] == "OPEN" and x["id"] not in handled]
    if verdict == "REVISE" and not (raw_issues or remaining):
        raise ValueError("REVISE requires new issues or existing unresolved OPEN issues")
    if verdict in ("ACCEPT", "INCONCLUSIVE") and remaining:
        raise ValueError("ACCEPT/INCONCLUSIVE cannot ignore unresolved Reviewer issues")
    return {"issues": issues, "resolutions": resolutions,
            "accepted_deviations": deviations}


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
    if purpose == "final_review":
        if role != "reviewer":
            raise PermissionError("final review must be independent Reviewer")
        if not any(t["role"] == "researcher" and t["status"] == "COMPLETE"
                   for t in state["tasks"].values()):
            raise PermissionError("final scientific review requires completed Researcher work")
        if (state["last_experiment_seq"] and
                state["last_interpret_seq"] <= state["last_experiment_seq"]):
            raise PermissionError(
                "latest experiment needs completed Researcher interpretation "
                "before a final scientific review; use Reviewer work for early audits")
    if purpose == "interpret":
        if role != "researcher":
            raise PermissionError("scientific interpretation belongs to Researcher")
        if not state["last_experiment_seq"]:
            raise PermissionError("interpretation requires recorded experiment evidence")
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
        if any(t["request_id"] == request_id and
               t["status"] in {"READY", "RUNNING", "WAITING", "COMPLETE"}
               for t in state["tasks"].values()):
            raise PermissionError("request already has an active respondent")


def apply_decision(store: Store, choice: dict, level: str):
    state = store.state()
    if state["done"] or state["halt"]:
        raise ValueError("project is stopped")
    if any(t["status"] == "RUNNING" for t in state["tasks"].values()):
        raise RuntimeError("a worker is running; reconcile before re-dispatch")
    action = choice.get("action")
    reason = _limit_text(choice.get("reason"), "reason",
                         max_len=OUTPUT_TEXT_LIMITS["supervisor decision reason"])
    if action == "review_issue_merge":
        merge_id = choice.get("merge_id")
        if not isinstance(merge_id, str):
            raise ValueError("issue merge ID must be text")
        group = state["issue_merges"].get(merge_id)
        if not group or group["status"] != "PENDING":
            raise ValueError("issue merge review requires a PENDING merge ID")
        verdict = choice.get("verdict")
        if verdict not in ("APPROVE", "REJECT"):
            raise ValueError("issue merge verdict requires APPROVE or REJECT")
        if verdict == "APPROVE" and any(
                state["review_issues"].get(x, {}).get("status") != "OPEN"
                for x in group["issue_ids"]):
            raise ValueError("issue merge approval has stale OPEN issue IDs")
        store.append("DECISION", level=level, action=action, reason=reason)
        store.append("ISSUE_MERGE_REVIEW", id=merge_id, verdict=verdict,
                     level=level, reason=reason)
        return None
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
        material_version = state["material_seq"] if purpose in (
            "interpret", "final_review") else 0
        if any(t["role"] == role and t["purpose"] == purpose
               and t["instruction"] == instruction and t["status"] != "BLOCKED"
               and (purpose not in ("interpret", "final_review")
                    or t["review_basis_seq"] == material_version)
               for t in state["tasks"].values()):
            raise ValueError("identical task already exists for this evidence version")
        task_id = _task_id(store)
        store.append("DECISION", level=level, action=action, reason=reason, task_id=task_id)
        store.append("TASK", id=task_id, role=role, purpose=purpose,
                     instruction=instruction, proposal_id=proposal_id, request_id=request_id,
                     review_basis_seq=material_version)
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
        v8_inputs.verify_inputs(store, state)
        if any(r["status"] == "OPEN" for r in state["requests"].values()):
            raise PermissionError("cannot finish while requests remain open")
        if not state["final_review"] or state["final_review"]["verdict"] not in (
            "ACCEPT", "INCONCLUSIVE"):
            raise PermissionError("independent final review required before finish")
        if state["final_review"]["basis_seq"] < state["material_seq"]:
            raise PermissionError("final review is stale; later scientific work requires new review")
        open_issues = [item["id"] for item in state["review_issues"].values()
                       if item["status"] == "OPEN"]
        if open_issues:
            raise PermissionError("REVIEW_DEBT: unresolved Reviewer issues: "
                                  + ", ".join(open_issues))
        if any(t["purpose"] == "experiment" for t in state["tasks"].values()):
            check = state["latest_validation"]
            if (not check or check["status"] != "PASSED"
                    or state["tasks"][check["task_id"]]["status"] != "COMPLETE"):
                detail = check["reason"] if check else "no independent validation recorded"
                raise PermissionError(
                    "VALIDATION_DEBT: latest experimental evidence is not verified; "
                    + detail + "; arrange an authorized corrective or validation experiment")
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


def _worker_result(store: Store, task_id: str, result: dict, *, recovery=False):
    state = store.state()
    task = state["tasks"][task_id]
    if task["status"] != "RUNNING" and not (recovery and task["status"] == "BLOCKED"):
        raise ValueError("result does not belong to the running or explicitly recovered task")
    action = result.get("action")
    summary = _limit_text(result.get("summary"), "worker summary",
                          max_len=OUTPUT_TEXT_LIMITS["worker summary"])
    if action == "request":
        target = result.get("to", "supervisor")
        if target not in (*ROLES, "supervisor"):
            raise ValueError("invalid recipient")
        question = _limit_text(result.get("question"), "question",
                               max_len=OUTPUT_TEXT_LIMITS["worker question"])
        reason = _limit_text(result.get("reason"), "request reason",
                             max_len=OUTPUT_TEXT_LIMITS["worker request reason"])
        refs = result.get("refs", [])
        if not isinstance(refs, list) or len(refs) > 12 or any(
                not isinstance(x, str) or
                len(x) > OUTPUT_TEXT_LIMITS["request reference (each)"] for x in refs):
            raise ValueError("invalid asset references")
        rid = _req_id(store)
        store.append("REQUEST", id=rid, from_task=task_id, from_role=task["role"],
                     to=target, question=question, reason=reason, refs=refs)
        return
    if action not in ("complete", "blocked"):
        raise ValueError("worker must complete, request or block")
    proposal, review, verdict, review_feedback, issue_merge = None, None, "", None, None
    if action == "complete":
        if result.get("issue_merge") is not None:
            if task["role"] != "researcher" or task["purpose"] != "work":
                raise PermissionError("only Researcher work may request issue grouping")
            if result.get("proposal") is not None:
                raise ValueError("grouping must be approved before corrective proposal")
            issue_merge = _issue_merge(task_id, result["issue_merge"], state)
        if result.get("proposal") is not None:
            if task["role"] != "researcher":
                raise PermissionError("only Researcher may propose an experiment")
            merge_id = result.get("issue_merge_id") or ""
            if not isinstance(merge_id, str):
                raise ValueError("issue_merge_id must be text")
            if merge_id:
                group = state["issue_merges"].get(merge_id)
                if (not group or group["status"] != "APPROVE"
                        or any(state["review_issues"].get(x, {}).get("status") != "OPEN"
                               for x in group["issue_ids"])):
                    raise PermissionError("corrective proposal references an unapproved or stale merge")
            proposal = _proposal(result["proposal"], result.get("required_checks"), merge_id)
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
            review_feedback = _review_feedback(task_id, verdict, result, state)
    paths = result.get("artifacts", [])
    if not isinstance(paths, list) or len(paths) > 24 or not all(
            isinstance(x, str) and x for x in paths):
        raise ValueError("artifacts must be workspace-relative paths")
    assets = []
    for item in paths:
        path = _asset_path(store, item)
        raw = path.read_bytes()
        known = state["assets"].get(item)
        if (state["inputs"] and not known and
                path.is_relative_to((store.root / "workspace" / "input").resolve())):
            raise PermissionError("staged input files cannot be registered as new experiment outputs")
        digest = hashlib.sha256(raw).hexdigest()
        if known:
            if known["sha256"] != digest or known["bytes"] != len(raw):
                raise PermissionError("frozen artifact was overwritten")
            # An existing registered asset can be CITED by any role; it must
            # not be registered twice or acquire a new provenance owner.
            continue
        if task["role"] != "experimenter" or task["purpose"] != "experiment":
            raise PermissionError(
                "unregistered artifact cannot be claimed by this role: "
                + item + "; only approved Experimenter runs register new files")
        assets.append({"path": item, "sha256": digest, "bytes": len(raw),
                       "task_id": task_id,
                       "description": str(result.get("artifact_description") or "")[
                           :OUTPUT_TEXT_LIMITS["artifact description"]]})
    if action == "complete" and task["request_id"]:
        req = state["requests"][task["request_id"]]
        if req["status"] != "OPEN":
            raise RuntimeError("assigned request was answered before worker finished")
    store.append("RESULT", task_id=task_id,
                 status="COMPLETE" if action == "complete" else "BLOCKED",
                 summary=summary, proposal=proposal, review=review, verdict=verdict,
                 issue_merge=issue_merge, review_feedback=review_feedback,
                 reply_to=task["request_id"] if action == "complete" else "")
    for asset in assets:
        store.append("ASSET", **asset)
    if action == "complete" and task["purpose"] == "experiment":
        # Model text cannot write this event. Compute it from the Reviewer-
        # approved proposal and hashed, freshly registered output files.
        current = store.state()
        contract = current["proposals"][task["proposal_id"]]
        report = v8_validation.run_gate(
            store, task_id, contract, current["assets"])
        store.append("VALIDATION", task_id=task_id, proposal_id=task["proposal_id"],
                     status=report["status"], checks=report["checks"],
                     reason=report["reason"])


def recover_model_result(store: Store, task_id: str):
    """Explicitly salvage a finished, read-only Codex reply previously misparsed."""
    with store.lock():
        state = store.state()
        if any(t["status"] == "RUNNING" for t in state["tasks"].values()):
            raise RuntimeError("wait for current Worker to finish before recovering")
        task = state["tasks"].get(task_id)
        if not task or task["status"] != "BLOCKED":
            raise ValueError("only a BLOCKED task may be salvaged")
        if task["purpose"] == "experiment":
            raise PermissionError("cannot salvage experimental execution via model logs")
        known_failure = (
            "JSONDecodeError" in task["summary"] or
            "ValueError: proposal must be nonempty text <= 16000 characters"
            in task["summary"] or
            "PermissionError: only Experimenter may register new files as artifacts"
            in task["summary"]
        )
        if not known_failure:
            raise PermissionError("salvage is limited to known fixed result-validation defects")
        logs = sorted((store.root / "logs").glob(task_id + "-*.jsonl"))
        if len(logs) != 1:
            raise ValueError("requires exactly one original Codex log for task")
        reply = _completed_model_reply(logs[0])
        verify_assets(store)
        _worker_result(store, task_id, reply, recovery=True)
        store.append("RECOVERY", task_id=task_id,
                     source_log=logs[0].name,
                     source_sha256=hashlib.sha256(logs[0].read_bytes()).hexdigest())
        store.publish()
        return {"task_id": task_id, "status": store.state()["tasks"][task_id]["status"]}


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


def _completed_model_reply(log_path: Path) -> dict:
    """Read the FULL final Codex JSON message; context telemetry caps it at 8K.

    The generic context parser intentionally truncates messages for prompts.
    V8 needs the complete structured result, which may contain long reports.
    """
    final_text = None
    completed = False
    with log_path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "turn.completed":
                completed = True
            elif event.get("type") == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str):
                        final_text = text
    if not completed or not final_text:
        raise ValueError("Codex ended without a completed turn and final message")
    return _extract_json(final_text)


def _call(model, effort, prompt, workdir, log_path, *, writable=False, timeout=1200):
    """Reuse Danus's non-escalating Codex command; parse full final JSON."""
    codex_bin = codex.resolve_bin()
    cmd = codex.exec_cmd(
        codex_bin, model, effort, "-C", str(workdir),
        "--skip-git-repo-check", "--sandbox",
        "workspace-write" if writable else "read-only", "--json", "-",
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        # Codex (and bin/codex-dsh) read a lone '-' prompt from stdin. The
        # full text must NOT occupy one Linux argv argument (MAX_ARG_STRLEN).
        proc = subprocess.Popen(cmd, cwd=str(workdir), stdin=subprocess.PIPE,
                                stdout=log, stderr=subprocess.STDOUT,
                                env=codex.subprocess_env(codex_bin),
                                start_new_session=True, text=True, encoding="utf-8")
        try:
            proc.communicate(input=prompt, timeout=timeout)
            code = proc.returncode
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
    if code:
        raise RuntimeError(f"model exited with {code}; see {log_path}")
    return _completed_model_reply(log_path)


def _brief_state(store: Store, *, last=12):
    """Bounded *routing* context; the full proposal and evidence stay in Store.

    Researcher/Reviewer/Experimenter still receive the complete approved
    protocol and open corrections in their own assignments. Supervisor routes
    by stable IDs and short summaries, never certifies scientific adequacy.
    """
    state = store.state()
    all_tasks = list(state["tasks"].values())
    tasks = [t for i, t in enumerate(all_tasks)
             if i >= len(all_tasks) - 10 or t["status"] in ("READY", "RUNNING", "WAITING")]
    selected = [{
        "id": t["id"], "role": t["role"], "purpose": t["purpose"],
        "status": t["status"], "proposal_id": t["proposal_id"],
        "request_id": t["request_id"], "instruction": t["instruction"][:320],
        "summary": t["summary"][:400],
    } for t in tasks]
    all_proposals = list(state["proposals"].values())
    proposals = [{
        "id": p["id"], "status": p["status"], "by": p["by"],
        "required_check_ids": [x["id"] for x in p["required_checks"]],
    } for i, p in enumerate(all_proposals)
       if i >= len(all_proposals) - 6 or p["status"] == "PENDING"]
    requests = [{
        "id": r["id"], "from_task": r["from_task"], "to": r["to"],
        "status": r["status"], "question": r["question"][:350],
        "answer": r["answer"][:350],
    } for r in state["requests"].values() if r["status"] == "OPEN"]
    recent = []
    for event in store.read()[-last:]:
        if event["kind"] == "INIT":
            continue
        recent.append({
            k: (v[:400] if isinstance(v, str) else v)
            for k, v in event.items()
            if k in ("seq", "kind", "task_id", "level", "action",
                     "status", "verdict", "reason", "error", "summary")
        })
    final = state["final_review"]
    validation = state["latest_validation"]
    inputs = state["inputs"]
    issues = [{
        "id": i["id"], "status": i["status"], "target_role": i["target_role"],
        "problem": i["problem"][:300], "impact": i["impact"][:250],
        "required_action": i["required_action"][:450],
    } for i in state["review_issues"].values() if i["status"] == "OPEN"]
    return {
        "goal": state["goal"], "reference": state["reference"][:1000],
        "tasks": selected, "task_count": len(all_tasks),
        "requests": requests, "open_request_count": len(requests),
        "proposals": proposals, "proposal_count": len(all_proposals),
        "assets": [{"path": a["path"], "task_id": a["task_id"]}
                   for a in list(state["assets"].values())[-24:]],
        "asset_count": len(state["assets"]),
        "final_review": ({
            "verdict": final["verdict"], "task_id": final["task_id"],
            "basis_seq": final["basis_seq"], "summary": final["summary"][:900],
        } if final else None),
        "latest_validation": ({
            "status": validation["status"], "task_id": validation["task_id"],
            "reason": validation["reason"][:500],
        } if validation else None),
        "review_issues": issues, "open_review_issue_count": len(issues),
        "issue_merge_proposals": [{
            "id": g["id"], "status": g["status"], "issue_ids": g["issue_ids"],
            "summary": g["summary"][:400], "reason": g["reason"][:300],
        } for g in state.get("issue_merges", {}).values()
          if g["status"] == "PENDING" or
          (g["status"] == "APPROVE" and all(
              state["review_issues"].get(x, {}).get("status") == "OPEN"
              for x in g["issue_ids"]))][-8:],
        "latest_experiment_needs_interpretation": bool(
            state.get("last_experiment_seq", 0)
            and state.get("last_interpret_seq", 0) <= state["last_experiment_seq"]),
        "inputs": ({
            "dataset_label": inputs["dataset_label"],
            "manifest_path": inputs["manifest_path"],
        } if inputs else None),
        "recent_events": recent,
    }


_SUPERVISOR_POLICY = """
You are the Danus-R V8 adaptive scientific Supervisor.
The roles are researcher, reviewer and experimenter. One scientific role runs
at a time. You choose the next task dynamically, not from a fixed DAG.
Research Plan is high-level and stable; the goal is immutable.
Workers may ask any one role or Supervisor one question at a time, and continue
after a reply. Resolve the oldest material open question before making new work.
Prefer existing verified asset records and reports to re-reading raw data.
Never guess asset IDs or bypass Reviewer experiment approval. A staged
input's recorded SHA and local source path establish only byte identity with
that local file at staging time; they do NOT independently authenticate upstream
dataset release, extraction correctness, or biological function. Do not ignore
a valid provenance manifest located at workspace/input/PROVENANCE.json.
Never claim a
scientific result is verified without actual evidence and independent review.
Negative and inconclusive findings are acceptable; don't chase positive results.
The runtime, not you, enforces authority.
Reviewer owns scientific and protocol adequacy. You may approve a Researcher's
issue grouping as a shared WORK PLAN, not scientific resolution: all original
Reviewer issues stay OPEN. Inspect the listed issue IDs, common remedy and
reason. REJECT a grouping that would lose distinct scientific or reproducibility
obligations. Decide pending groupings before work relying on them.
Use {"action":"review_issue_merge","merge_id":"task-N/merge",
"verdict":"APPROVE|REJECT","reason":"specific decision"}.
Approval only permits planning; it is NOT Reviewer resolution. Grouped
corrections still require an exact Researcher experiment proposal and
independent Reviewer experiment approval. Only Reviewer may later mark
issues RESOLVED, WAIVED or DUPLICATE with evidence.
After a final_review REVISE, follow its structured OPEN review_issues and
route fixes to their target roles. If the SAME concrete defect survives two
completed corrective attempts, first route a Researcher scope/necessity
reassessment (or a read-only Reviewer audit), not another larger omnibus
protocol. Ask what evidence is actually needed for the fixed scientific goal,
which prior independent checks can be reused, and what smallest repair would
address the remaining risk. Count distinct failure modes, not repeated issue
IDs. This is a change of strategy, NOT automatic issue closure or a numerical
deadline for science. A pending proposal still requires ordinary independent
Reviewer review; Reviewer may REVISE an overbroad proposal before execution.
Before a costly corrective experiment,
prefer Researcher issue grouping when multiple findings clearly overlap;
an already approved independent scientific need need not be deferred. Treat repeated findings as the same IDs;
do not multiply repair tasks for an unchanged unresolved defect. Experimenter corrective execution still
needs an independently approved proposal; don't overwrite frozen outputs.
Do not FINISH with unresolved Reviewer issues. Reviewer may explicitly WAIVE
a redundant check only by recording the failure it could detect and which
independent evidence already rules it out (logical ablation).
A Worker COMPLETE records execution only; it does not certify scientific
validity. Independent host-side checks from the exact approved proposal
produce VALIDATION PASSED, INCOMPLETE or FAILED. FINISH is forbidden while
the latest experiment has validation debt. On INCOMPLETE or FAILED inspect
the recorded check results and arrange authorized diagnostic work or a new
Reviewer-approved corrective / validation-only experiment on frozen outputs.
Never simply retry identical work or erase historical failed experiments.
An INCONCLUSIVE scientific hypothesis is allowed only with valid experiment
evidence. If no authorized repair is possible, HIGH may explicitly HALT.
Keep JSON string fields concise; count CHARACTERS, not words or tokens.
Target at most 4,000 characters for a new task instruction or answer message,
and 800 characters for its reason. These are recommended output budgets,
not permission to omit essential scientific constraints or supporting facts.
Do not duplicate existing proposal text in task instructions: use proposal_id.
Return exactly one JSON object and no markdown:
New work: {"action":"dispatch","role":"researcher|reviewer|experimenter",
"purpose":"work|answer|experiment|interpret|review_experiment|final_review",
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
purpose=experiment and same proposal_id. After a successful experiment assign
Researcher purpose=interpret to analyze actual evidence, then request a
purpose=final_review from Reviewer. If latest_experiment_needs_interpretation
is true, final_review is illegal: route Researcher purpose=interpret first.
Reviewer purpose=work may inspect code
and raw evidence earlier to catch faults; this is an early technical audit,
NOT a final scientific conclusion review. A final_review must cover the latest
interpretation and evidence. Never conclude from pre-interpretation review.
"""

_WORKER_POLICY = """
You are one assigned Danus-R scientific worker. Execute only your task.
You can ask ONE question to researcher, reviewer, experimenter or supervisor
at a time. State WHY the answer is needed. After it returns, reconsider next step.
Do not modify the fixed research goal. A negative scientific result is valid.
Keep experiment costs small. Never modify external reference repositories or
previously frozen artifacts. Prefer summaries to full raw-data re-interpretation.
Never fabricate successful tool calls, citations or file artifacts.
When staged input files exist, read workspace/input/PROVENANCE.json and
use its byte-for-byte local source comparison as provenance evidence.
The operator, not any Worker, staged the inputs. This does not independently
authenticate the upstream dataset or scientific interpretation; disclose
those limitations, but do not demand that this manifest prove more than it does.
Researcher may propose a specific small experiment but must not execute it.
Start from the FIXED scientific question and the narrowest defensible conclusion,
including a negative or inconclusive result. Before EVERY corrective proposal,
perform a concise minimum-sufficient-evidence audit BY DISTINCT FAILURE MODE,
not by every repeated issue ID: (a) what wrong conclusion or unreproducible
result could this defect cause, (b) what independent existing evidence already
excludes that failure, (c) choose REUSE, NARROW, REPAIR or OMIT, and (d) for
REPAIR, what is the smallest executable check or change that closes the gap.
For an omitted or replaced check, cite the actual independent evidence and
its scope; shared code or self-reported PASS is not independent evidence.
Include only a SHORT audit summary in the proposal, not another exhaustive
checklist. If no new experiment is scientifically needed, report that finding
and seek independent Reviewer adjudication of still-OPEN issues instead of
proposing a redundant rerun. Never silently delete prior approved obligations:
a narrower replacement proposal needs its own Reviewer approval, and original
issues stay OPEN until the Reviewer explicitly resolves, waives or deduplicates
them with evidence.
If several OPEN Reviewer issues overlap, check whether the same repair or
evidence would satisfy them. If so, propose ONE shared work item preserving
each original issue and any distinct obligation. For researcher purpose=work,
optionally return "issue_merge":{"issue_ids":["task-N/issue-1",
"task-M/issue-2"],"summary":"shared corrective work",
"reason":"why one repair addresses each issue"}. Do NOT send a new experiment
proposal in the same response: Supervisor must APPROVE/REJECT first.
Approval groups work only; it does not close Reviewer findings. If APPROVED,
a later corrective proposal referring to that grouping must include
"issue_merge_id":"task-N/merge". If not approved, address distinct issues
independently. Do not manufacture overlapping groups for unrelated defects.
Reviewer independently checks proposed experiment and scientific conclusions.
Experimenter only executes the exact Reviewer-approved experimental plan.
Output budgets (CHARACTERS, not words, bytes or tokens; count text inside JSON
string values, excluding JSON punctuation):
- proposal: target at most 10,000 characters. Give a COMPLETE reproducible
  MINIMUM SUFFICIENT protocol (question, data, controls, metrics, bounds and
  negative criteria). Reuse pinned prior evidence rather than restating its
  full inventory. Exclude unsupported extras; never omit decisive evidence.
- summary (including review findings): target at most 5,000 characters.
  Put detailed scientific evidence in registered assets when authorized,
  then cite the existing asset paths in the summary; never hide critical errors.
- question: target at most 1,500 characters.
- request reason: target at most 800 characters.
These are conservative writing targets, not the validator's hard limits.
The validator retains headroom for justified longer content; do not split a
single proposal into partial messages just to meet the writing target.
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
Alongside proposal, supply required_checks as a JSON list of independent,
executable check specifications for the deterministic host Validation Gate:
{"id":"assets","kind":"asset_integrity"} checks freshly registered hashes;
{"id":"rows","kind":"csv_rows","path":"output.csv","expected_rows":256}
checks actual CSV rows; {"id":"mean","kind":"csv_mean","path":"output.csv",
"column":"mse","report_path":"report.json","report_key":"mse",
"abs_tolerance":1e-9} independently recomputes a column average against
a reported numeric field. For already frozen V7 records, v7_evidence can
use pinned evidence_ids, scope and protocol_id (not a substitute for
custom scientific checks). No check specification or unsupported bespoke
checks => validation INCOMPLETE; never mark them passed from prose or a
Worker-generated checks.json file. Reviewer must assess whether the
listed executable checks cover the scientific invariants in the proposal.
Do not falsely claim that these generic checks establish causal validity.
Experimenter must deliver the exact approved artifact paths without
changing required_checks. Model-declared VALID/PASS is not authority.
For Reviewer purpose=review_experiment, use verdict APPROVE/REVISE/REJECT.
Actively challenge unnecessary requirements BEFORE approving their execution.
For each costly or repeated check, ask what concrete failure it detects, whether
that failure can change the FIXED scientific conclusion or its reproducibility,
and whether existing independent evidence already covers it. Require a minimal
sufficient set of checks; REVISE an overbroad proposal to remove, reuse or
narrow unjustified work, even when every requested step is technically valid.
Conversely, do not approve a cheap but inadequate plan that omits decisive
evidence or relies on an Experimenter's claimed checker success. Revisions
must be newly proposed and independently approved; do not silently change the
pending exact proposal. Judge omissions by effect on the scoped conclusion. Distinguish (1) a
scientific defect that can change the result, (2) an engineering defect
that prevents independent reproduction, and (3) redundant checks already
covered by independent evidence. For each demanded check, explain which
error it would detect and why current evidence does not exclude it. The
goal is a defensible conclusion, not completing every procedural detail.
For Researcher purpose=interpret, analyze the already recorded experimental
outputs, including negative findings; do not produce a new experiment unless
you have a separate scientific reason. For Reviewer purpose=final_review,
assess the Researcher's interpretation against underlying evidence and the
COMPLETE approved protocol supplied in your assignment. Apply a scoped
necessity test to missing auxiliary steps: what distinct failure could they
have caught, whether it affects the conclusion, and whether existing
independent observations already exclude it. Prefer evidence-backed
accepted_deviations, DUPLICATE or WAIVED as appropriate over new procedural
work; do not claim a missing verification executed. Distinguish
conclusion-changing scientific defects, reproducibility gaps and already
independently covered repeated checks. For each blocking finding explain
its scientific impact and the minimum justified correction. Missing an
auxiliary procedure alone does not invalidate a conclusion; missing
decisive evidence cannot be waived. Verdict is
ACCEPT/INCONCLUSIVE/REVISE/REJECT. INCONCLUSIVE means scientifically uncertain
results from acceptable evidence, not incomplete required work. REVISE can
return issues: [] when earlier OPEN issues still need correction; name their
EXISTING IDs in the summary instead of creating new IDs. Only add issues
[{problem,impact,required_action,target_role,evidence}] for genuinely NEW,
distinct defects. A repeated finding, even with new evidence, is NOT new.
Evidence may be a nonblank string or a list of 1-8 nonblank citation
strings, combined <= 1500 characters (target <= 1050). The runtime assigns
IDs to truly new issues. Review each previous OPEN issue; use
resolved_issues: [{id,status:"RESOLVED"|"WAIVED"|"DUPLICATE",reason,evidence}].
For DUPLICATE also give canonical_id identifying a DIFFERENT existing OPEN
issue that remains active; explain why both findings impose the same
requirement, not just similar wording. This is not a scientific waiver.
WAIVED requires logical ablation: what failure the test was designed to
catch and which independent evidence already excludes it, within what scope.
Never mark an unfinished check RESOLVED or WAIVED merely to reduce counts.
For non-blocking protocol omissions with independently adequate evidence, use
accepted_deviations: [{item,reason,evidence}]. Do not claim omitted tests ran.
Do not waive a failed host Validation Gate in prose.
Reviewer decides scientific adequacy; Supervisor only coordinates work.
Experimenter may write files only for purpose=experiment, inside this workspace.
The artifacts array registers NEW output files only for approved Experimenter
experiment tasks. Researcher and Reviewer should normally set artifacts=[]
and cite previously registered asset paths in the summary instead.
For compatibility, an existing REGISTERED asset path in artifacts is treated
only as a read-only reference (not registered again). Never claim a new or
unregistered file as your own evidence outside an approved experiment.
Report newly generated experimental artifacts as workspace-relative paths.
"""


def _supervisor(store: Store, level, error=""):
    print(f"[V8] Supervisor-{level}: deciding next action ...", flush=True)
    prompt = (_SUPERVISOR_POLICY + _output_budget_guide() +
              "\nLEVEL: " + level +
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
        v8_inputs.verify_inputs(store)
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
    print(f"[V8] {task['role']}: running {task_id} ({task['purpose']}); "
          f"log: {store.root / 'logs'}", flush=True)
    req = state["requests"].get(task["request_id"])
    replies = [r for r in state["requests"].values()
               if r["from_task"] == task_id and r["status"] == "ANSWERED"]
    latest_experiment = next((t for t in reversed(list(state["tasks"].values()))
                              if t["purpose"] == "experiment"), None)
    related_proposal_id = task["proposal_id"] or (
        latest_experiment["proposal_id"] if latest_experiment else "")
    payload = {
        "goal": state["goal"], "reference": state["reference"], "task": task,
        "request_to_answer": req,
        "replies_to_your_previous_questions": replies,
        "approved_experiment_proposal": state["proposals"].get(related_proposal_id),
        "open_review_issues": [i for i in state["review_issues"].values()
                               if i["status"] == "OPEN"],
        "issue_merge_proposals": [g for g in state["issue_merges"].values()
                                  if g["status"] in ("PENDING", "APPROVE")
                                  and all(state["review_issues"].get(x, {}).get("status") == "OPEN"
                                          for x in g["issue_ids"])][-8:],
        "previous_final_review": state["final_review"],
        "known_assets": list(state["assets"].values()),
        "latest_validation": state["latest_validation"],
        "input_provenance": state["inputs"],
        "recent_completed_work": [
            {"role": t["role"], "purpose": t["purpose"],
             "summary": t["summary"][:3000]}
            for t in state["tasks"].values() if t["status"] == "COMPLETE"
        ][-10:],
    }
    prompt = _WORKER_POLICY + _output_budget_guide() + "\nCurrent assignment:\n" + json.dumps(
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
            v8_inputs.verify_inputs(store)
            _worker_result(store, task_id, reply)
            verify_assets(store)
            v8_inputs.verify_inputs(store)
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
        try:
            v8_inputs.verify_inputs(store, state)
        except (PermissionError, FileNotFoundError, ValueError, OSError) as exc:
            # This is a project integrity blocker, not a scientific hypothesis
            # failure. Show the exact reason in the existing TUI HALT channel.
            store.append("HALT", reason=f"INPUT_INTEGRITY: {type(exc).__name__}: {exc}")
            store.publish()
            return "HALT"
        if any(t["status"] == "RUNNING" for t in state["tasks"].values()):
            raise RuntimeError("interrupted worker must be reconciled before continuing")
    error = ""
    recent_results = [e for e in store.read() if e["kind"] == "RESULT"]
    recent_blocks = 0
    for result in reversed(recent_results):
        if result["status"] != "BLOCKED":
            break
        recent_blocks += 1
    force_high = recent_blocks >= 2
    validation_events = [e for e in store.read() if e["kind"] == "VALIDATION"]
    if len(validation_events) >= 2 and validation_events[-1]["status"] != "PASSED":
        def debt_signature(event):
            return (event["status"], tuple(sorted(
                (r["id"], r["status"]) for r in event["checks"]
                if r["status"] != "PASSED")))
        # Two separately executed runs with identical unresolved checks are
        # concrete lack of progress. Escalate routing, never bypass the gate.
        if debt_signature(validation_events[-1]) == debt_signature(validation_events[-2]):
            force_high = True
    for attempt in range(3):
        level = "high" if force_high or attempt == 2 else "low"
        try:
            try:
                decision = _supervisor(store, level, error)
            except OSError as exc:
                # Only model invocation failures are launch errors.
                # PermissionError from apply_decision is a legal route denial.
                with store.lock():
                    store.append("HALT", reason=f"SUPERVISOR_LAUNCH_ERROR: {type(exc).__name__}: {exc}")
                    store.publish()
                return "HALT"
            except (RuntimeError, subprocess.SubprocessError) as exc:
                # A failed Codex execution is not an illegal scientific route.
                with store.lock():
                    store.append("HALT", reason=f"SUPERVISOR_MODEL_ERROR: {type(exc).__name__}: {exc}")
                    store.publish()
                return "HALT"
            with store.lock():
                selected = apply_decision(store, decision, level)
                store.publish()
        except (ValueError, KeyError, PermissionError, RuntimeError,
                json.JSONDecodeError, subprocess.SubprocessError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            with store.lock():
                store.append("DENIED", level=level, error=error)
                store.publish()
            continue
        except OSError as exc:
            # File-system errors applying decisions are not launch errors.
            with store.lock():
                store.append("HALT", reason=f"SUPERVISOR_DECISION_IO_ERROR: {type(exc).__name__}: {exc}")
                store.publish()
            return "HALT"
        if selected == "HIGH":
            error = "Low Supervisor voluntarily escalated"
            force_high = True
            continue
        if isinstance(selected, str):
            # One decision starts at most one Worker. A failed worker is NOT a
            # rejected Supervisor decision, and must not trigger immediate
            # duplicate research or experimental execution.
            try:
                _worker(store, selected, timeout=worker_timeout)
            except Exception as exc:
                print(f"[V8] {selected} failed: {type(exc).__name__}: {exc}. "
                      "Inspect its log; do not silently rerun.", flush=True)
                return "WORKER_BLOCKED"
            print(f"[V8] {selected}: {store.state()['tasks'][selected]['status']}",
                  flush=True)
        return selected or "CONTROL"
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
        "input_provenance": state["inputs"],
        "latest_validation": state["latest_validation"],
        "review_issues": list(state["review_issues"].values()),
        "issue_merges": list(state["issue_merges"].values()),
    }, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Danus-R V8 adaptive runner")
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new")
    new.add_argument("project")
    new.add_argument("--goal-file", type=Path, required=True)
    new.add_argument("--reference", default="")
    stage = commands.add_parser("stage-inputs")
    stage.add_argument("project")
    stage.add_argument("--source-dir", type=Path, required=True)
    stage.add_argument("--dataset-label", required=True)
    stage.add_argument("--files", nargs="+", required=True)
    stage.add_argument("--extraction-script", type=Path)
    stage.add_argument("--extraction-summary", type=Path)
    for command in ("run", "step", "status", "recover", "salvage", "reconcile", "unblock"):
        sub = commands.add_parser(command)
        sub.add_argument("project")
        if command in ("unblock", "reconcile"):
            sub.add_argument("--reason", required=True)
        if command == "reconcile":
            sub.add_argument("--task-id", required=True)
        if command == "run":
            sub.add_argument("--max-steps", type=int, default=0)
            sub.add_argument("--worker-timeout", type=int, default=1200)
        if command in ("recover", "salvage"):
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
    if args.command == "stage-inputs":
        with store.runner_lock():
            with store.lock():
                info = v8_inputs.stage_inputs(
                    store, args.source_dir, args.files, args.dataset_label,
                    extraction_script=args.extraction_script,
                    extraction_summary=args.extraction_summary)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return
    if args.command == "unblock":
        with store.lock():
            if not store.state()["halt"]:
                raise ValueError("project is not halted")
            store.append("UNHALT", reason=_limit_text(args.reason, "reason", max_len=1000))
            store.publish()
        return
    if args.command == "salvage":
        with store.runner_lock():
            print(json.dumps(recover_model_result(store, args.task_id)))
        return
    if args.command == "reconcile":
        with store.lock():
            task = store.state()["tasks"].get(args.task_id)
            if not task or task["status"] != "RUNNING":
                raise ValueError("only interrupted RUNNING tasks can be reconciled")
            store.append("RESULT", task_id=args.task_id, status="BLOCKED",
                         summary="Operator recorded interrupted work as incomplete: " +
                                 _limit_text(args.reason, "reason", max_len=1000),
                         proposal=None, review=None, verdict="")
            store.publish()
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
            if outcome in ("STOP", "HALT", "WORKER_BLOCKED"):
                break


if __name__ == "__main__":
    main()
