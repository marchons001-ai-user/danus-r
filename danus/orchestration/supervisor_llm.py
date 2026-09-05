"""Restricted LLM routing for S6 scientific ambiguities.

The deterministic reducer remains authoritative. This module only turns an
already-bounded ``SCIENTIFIC_ROUTE_REQUIRED`` action into one ephemeral route
recommendation. It never writes Plan/Progress/Handoff state.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from danus import codex
from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core.handoff import HandoffRequests
from danus.execution import context as C
from danus.execution import layout as L

_MODEL = "gpt-5.6-luna"
_EFFORT = "low"
_ROUTES = ("INTERPRET", "RETHINK", "ESCALATE")
_MAX_MESSAGE_CHARS = 4000
_MAX_RATIONALE_CHARS = 800
_MAX_WALL_SECONDS = 180


def _assignment(assignments: List[Dict[str, Any]], assignment_id: str) -> Dict[str, Any]:
    for item in assignments:
        if item.get("assignment_id") == assignment_id:
            return item
    raise ValueError(f"unknown assignment: {assignment_id}")


def _checkpoint_message(root: Path, assignment: Dict[str, Any]) -> str:
    worker = str(assignment.get("worker") or "")
    role = str(assignment.get("role") or "").lower()
    checkpoint = C.checkpoint_for_prompt(root / "workers" / worker, role)
    if not checkpoint or checkpoint.get("assignment_id") != assignment.get("assignment_id"):
        raise ValueError(f"verified checkpoint unavailable for assignment {assignment.get('assignment_id')}")
    message = str(checkpoint.get("last_agent_message") or "").strip()
    if not message:
        raise ValueError(f"final message unavailable for assignment {assignment.get('assignment_id')}")
    return message[-_MAX_MESSAGE_CHARS:]


def build_route_packet(root: Path, action: Dict[str, Any]) -> Dict[str, Any]:
    """Build the smallest verified context needed for one bounded route choice."""
    root = Path(root)
    if action.get("kind") != "SCIENTIFIC_ROUTE_REQUIRED":
        raise ValueError("action is not a scientific routing ambiguity")
    if action.get("candidate_routes") != list(_ROUTES):
        raise ValueError("scientific route candidates do not match the S6-A contract")

    plan = ResearchPlanGraph(root).snapshot()
    plan_version = int(action.get("plan_version") or 0)
    if plan_version != int(plan["plan_version"]):
        raise ValueError("scientific routing action has stale plan_version")
    node_id = str(action.get("node_id") or "")
    node = plan["nodes"].get(node_id)
    if not node or node.get("state") != "ACTIVE":
        raise ValueError("scientific routing action does not target an active plan node")

    ledger = ProgressLedger(root)
    assignments = ledger.assignments()
    source_id = str(action.get("subject_id") or "")
    source = _assignment(assignments, source_id)
    if (
        source.get("state") != "BLOCKED"
        or source.get("outcome") != "NEEDS_REVIEW"
        or source.get("node_id") != node_id
        or int(source.get("plan_version") or 0) != plan_version
    ):
        raise ValueError("source assignment no longer matches the scientific ambiguity")

    review_id = str(action.get("review_handoff_id") or "")
    review = HandoffRequests(ledger).get(review_id)
    if (
        not review
        or review.get("status") != "RESOLVED"
        or review.get("kind") != "REVIEW"
        or review.get("subject_type") != "ASSIGNMENT"
        or review.get("subject_id") != source_id
    ):
        raise ValueError("completed generic review is unavailable")

    review_assignment_id = str(action.get("review_assignment_id") or "")
    review_assignment = _assignment(assignments, review_assignment_id)
    if (
        review.get("resolution_assignment_id") != review_assignment_id
        or review_assignment.get("state") != "DONE"
        or review_assignment.get("outcome") != "COMPLETE"
        or str(review_assignment.get("role") or "").lower() != "reviewer"
    ):
        raise ValueError("review assignment is not a completed Reviewer result")

    basis = [
        f"node:{node_id}",
        f"source_assignment:{source_id}",
        f"review_handoff:{review_id}",
        f"review_assignment:{review_assignment_id}",
    ]
    return {
        "schema_version": 1,
        "plan_version": plan_version,
        "node_id": node_id,
        "node": dict(node),
        "source": {
            "assignment_id": source_id,
            "role": source.get("role"),
            "worker": source.get("worker"),
            "note": source.get("note"),
            "final_message": _checkpoint_message(root, source),
        },
        "review": {
            "handoff_id": review_id,
            "assignment_id": review_assignment_id,
            "summary": review.get("summary"),
            "final_message": _checkpoint_message(root, review_assignment),
        },
        "candidate_routes": list(_ROUTES),
        "allowed_basis": basis,
    }


def _prompt(packet: Dict[str, Any]) -> str:
    return (
        "You are the restricted Danus-R Supervisor scientific router. The deterministic "
        "control plane has already proved that a route choice is needed. Do not solve the "
        "research problem and do not second-guess a Reviewer verdict. Choose exactly one "
        "route from candidate_routes using only the supplied packet.\n\n"
        "Route meanings:\n"
        "- INTERPRET: the evidence/review is valid but a Researcher should synthesize its scientific meaning.\n"
        "- RETHINK: the current scientific direction or assumption needs substantive reworking by a Researcher.\n"
        "- ESCALATE: the supplied state is insufficient or inappropriate for autonomous routing and a human/main agent must decide.\n\n"
        "Return exactly one JSON object and nothing else with exactly these keys: "
        "route, rationale, basis. route must be in candidate_routes. rationale must be a "
        f"short string of at most {_MAX_RATIONALE_CHARS} characters. basis must be a non-empty "
        "array containing only strings from allowed_basis. Do not use tools.\n\n"
        "PACKET:\n" + json.dumps(packet, ensure_ascii=False, sort_keys=True)
    )


def _run_luna(prompt: str) -> Dict[str, Any]:
    """Run Luna/low through the existing ChatGPT-subscription Codex launcher."""
    if codex.backend() != "chatgpt":
        return {
            "status": "failed",
            "error": "restricted Supervisor requires CODEX_BACKEND=chatgpt",
            "model": _MODEL,
            "effort": _EFFORT,
            "tool_calls": 0,
            "reply": "",
            "usage": {},
        }

    codex_bin = codex.resolve_bin()
    with tempfile.TemporaryDirectory(prefix="danus-supervisor-") as tmp:
        work = Path(tmp)
        log_path = work / "route.jsonl"
        cmd = codex.exec_cmd(
            codex_bin,
            _MODEL,
            _EFFORT,
            "-C", str(work),
            "--skip-git-repo-check",
            "--sandbox", "read-only",
            "--json",
            prompt,
        )
        with log_path.open("w", encoding="utf-8") as log:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    cwd=str(work),
                    env=codex.subprocess_env(codex_bin),
                    start_new_session=True,
                )
            except OSError as exc:
                return {
                    "status": "failed", "error": f"{type(exc).__name__}: {exc}",
                    "model": _MODEL, "effort": _EFFORT, "tool_calls": 0,
                    "reply": "", "usage": {},
                }
            try:
                returncode = proc.wait(timeout=_MAX_WALL_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
                proc.wait()
                return {
                    "status": "failed", "error": "timeout",
                    "model": _MODEL, "effort": _EFFORT, "tool_calls": 0,
                    "reply": "", "usage": {},
                }

        telemetry = C.inspect_log(log_path)
        usage = {
            "input": int(telemetry.get("input_tokens") or 0),
            "output": int(telemetry.get("output_tokens") or 0),
            "reasoning": int(telemetry.get("reasoning_output_tokens") or 0),
        }
        return {
            "status": "completed" if returncode == 0 else "failed",
            "error": "" if returncode == 0 else f"codex_exit_{returncode}",
            "model": _MODEL,
            "effort": _EFFORT,
            "tool_calls": int(telemetry.get("tool_calls") or 0),
            "reply": str(telemetry.get("last_agent_message") or "").strip(),
            "usage": usage,
        }


def _validate(packet: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    base = {
        "subject_id": packet["source"]["assignment_id"],
        "model": _MODEL,
        "effort": _EFFORT,
        "usage": result.get("usage") or {},
    }
    if result.get("status") != "completed":
        return {**base, "status": "failed", "error": str(result.get("error") or "model_call_failed")}
    if int(result.get("tool_calls") or 0) != 0:
        return {**base, "status": "failed", "error": "tool_use_forbidden"}

    try:
        obj = json.loads(str(result.get("reply") or ""))
    except json.JSONDecodeError:
        return {**base, "status": "failed", "error": "invalid_json"}
    if not isinstance(obj, dict) or set(obj) != {"route", "rationale", "basis"}:
        return {**base, "status": "failed", "error": "invalid_schema"}

    route = obj.get("route")
    rationale = obj.get("rationale")
    basis = obj.get("basis")
    if route not in packet["candidate_routes"]:
        return {**base, "status": "failed", "error": "route_not_allowed"}
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > _MAX_RATIONALE_CHARS:
        return {**base, "status": "failed", "error": "invalid_rationale"}
    if (
        not isinstance(basis, list)
        or not basis
        or len(basis) > len(packet["allowed_basis"])
        or any(not isinstance(item, str) or item not in packet["allowed_basis"] for item in basis)
        or len(set(basis)) != len(basis)
    ):
        return {**base, "status": "failed", "error": "invalid_basis"}

    return {
        **base,
        "status": "valid",
        "route": route,
        "rationale": rationale.strip(),
        "basis": basis,
    }


def consult_scientific_routes(root: Path, actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return ephemeral validated route decisions; never materialize or persist them."""
    out: List[Dict[str, Any]] = []
    for action in actions:
        if action.get("kind") != "SCIENTIFIC_ROUTE_REQUIRED":
            continue
        subject_id = str(action.get("subject_id") or "")
        try:
            packet = build_route_packet(root, action)
        except (FileNotFoundError, ValueError) as exc:
            out.append({
                "subject_id": subject_id,
                "model": _MODEL,
                "effort": _EFFORT,
                "usage": {},
                "status": "failed",
                "error": f"invalid_context:{exc}",
            })
            continue
        out.append(_validate(packet, _run_luna(_prompt(packet))))
    return out


def supervise_main(argv: List[str]) -> int:
    """CLI seam: only a manual ``supervise --apply`` may spend a Luna route call."""
    from . import cli as CLI

    args = CLI.build_parser().parse_args(["supervise", *argv])
    if args.watch or not args.apply:
        return CLI.main(["supervise", *argv])

    report = CLI.do_supervise(
        args.project,
        complete_node=args.complete_node,
        reopen_node=args.reopen_node,
        apply=True,
        token_budget=args.token_budget,
    )
    report["scientific_route_decisions"] = []
    eligible = (
        report.get("project_state") == "RUNNING"
        and bool(report.get("convergence", {}).get("stable"))
        and not report.get("budget_stop")
        and not report.get("quota_policy", {}).get("block_new_codex_rounds")
    )
    if eligible:
        report["scientific_route_decisions"] = consult_scientific_routes(
            L.project_dir(args.project), report.get("actions", [])
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0
