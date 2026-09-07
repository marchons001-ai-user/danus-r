"""Restricted LLM routing for S6 scientific ambiguities.

The deterministic reducer remains authoritative. The restricted Luna router is
used only for an already-bounded ``SCIENTIFIC_ROUTE_REQUIRED`` action. Accepted
or fail-closed decisions are persisted before their existing Handoff is opened,
so cold-start replay never needs a second accepted route decision.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from danus import codex
from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core._util import append_jsonl, read_jsonl, utc_now
from danus.core.handoff import HandoffRequests
from danus.execution import context as C
from danus.execution import layout as L
from danus.orchestration.control import generic_review_class

_MODEL = "gpt-5.6-luna"
_EFFORT = "low"
_ROUTES = ("INTERPRET", "RETHINK", "ESCALATE")
_DECISION_EVENT = "SUPERVISOR_ROUTE_DECIDED"
_MAX_MESSAGE_CHARS = 4000
_MAX_RATIONALE_CHARS = 800
_MAX_WALL_SECONDS = 180


def _assignment(assignments: List[Dict[str, Any]], assignment_id: str) -> Dict[str, Any]:
    for item in assignments:
        if item.get("assignment_id") == assignment_id:
            return item
    raise ValueError(f"unknown assignment: {assignment_id}")


def _checkpoint_message(root: Path, assignment: Dict[str, Any]) -> str:
    message = str(assignment.get("result") or "").strip()
    if message:
        return message[-_MAX_MESSAGE_CHARS:]
    worker = str(assignment.get("worker") or "")
    role = str(assignment.get("role") or "").lower()
    checkpoint = C.checkpoint_for_prompt(
        root / "workers" / worker,
        role,
        assignment=assignment,
    )
    if not checkpoint:
        raise ValueError(f"verified checkpoint unavailable for assignment {assignment.get('assignment_id')}")
    message = str(checkpoint.get("last_agent_message") or "").strip()
    if not message:
        raise ValueError(f"final message unavailable for assignment {assignment.get('assignment_id')}")
    return message[-_MAX_MESSAGE_CHARS:]


def _route_context(root: Path, action: Dict[str, Any]) -> Dict[str, Any]:
    """Revalidate the durable Assignment/Review authority for one S6 route."""
    if action.get("kind") != "SCIENTIFIC_ROUTE_REQUIRED":
        raise ValueError("action is not a scientific routing ambiguity")
    if action.get("candidate_routes") != list(_ROUTES):
        raise ValueError("scientific route candidates do not match the S6-A contract")

    ledger = ProgressLedger(root)
    assignments = ledger.assignments()
    source_id = str(action.get("subject_id") or "")
    source = _assignment(assignments, source_id)
    worker = str(source.get("worker") or "")
    current = ledger.current(worker, refresh_stale=False) if worker else None
    node_id = str(action.get("node_id") or "")
    if (
        not current
        or current.get("assignment_id") != source_id
        or source.get("state") != "BLOCKED"
        or source.get("outcome") != "NEEDS_REVIEW"
        or source.get("node_id") != node_id
        or ledger._is_plan_stale(source)
    ):
        raise ValueError("source assignment no longer matches the scientific ambiguity")

    handoffs = HandoffRequests(ledger)
    review_id = str(action.get("review_handoff_id") or "")
    review = handoffs.get(review_id)
    if (
        not review
        or review.get("status") != "RESOLVED"
        or review.get("kind") != "REVIEW"
        or review.get("subject_type") != "ASSIGNMENT"
        or review.get("subject_id") != source_id
        or review.get("node_id") != node_id
        or ledger._is_plan_stale({
            "plan_version": int(review.get("plan_version") or 0),
            "node_id": node_id,
        })
    ):
        raise ValueError("completed generic review is unavailable")

    review_assignment_id = str(action.get("review_assignment_id") or "")
    review_assignment = _assignment(assignments, review_assignment_id)
    if (
        review.get("resolution_assignment_id") != review_assignment_id
        or review_assignment.get("state") != "DONE"
        or review_assignment.get("outcome") != "COMPLETE"
        or str(review_assignment.get("role") or "").lower() != "reviewer"
        or ledger._is_plan_stale(review_assignment)
    ):
        raise ValueError("review assignment is not a completed Reviewer result")
    if generic_review_class(review_assignment) != "SCIENTIFIC_AMBIGUITY":
        raise ValueError("generic review did not authorize scientific routing")

    evidence_refs: List[str] = []
    source_handoff_id = str(source.get("handoff_id") or "")
    if source_handoff_id:
        source_handoff = handoffs.get(source_handoff_id)
        if source_handoff:
            evidence_refs = list(dict.fromkeys(
                str(value).strip()
                for value in (source_handoff.get("evidence_refs") or [])
                if str(value).strip()
            ))
    return {
        "ledger": ledger,
        "source": source,
        "review": review,
        "review_assignment": review_assignment,
        "evidence_refs": evidence_refs,
    }


def build_route_packet(root: Path, action: Dict[str, Any]) -> Dict[str, Any]:
    """Build the smallest verified context needed for one bounded route choice."""
    root = Path(root)
    context = _route_context(root, action)

    plan = ResearchPlanGraph(root).snapshot()
    plan_version = int(action.get("plan_version") or 0)
    if plan_version != int(plan["plan_version"]):
        raise ValueError("scientific routing action has stale plan_version")
    node_id = str(action.get("node_id") or "")
    node = plan["nodes"].get(node_id)
    if not node or node.get("state") != "ACTIVE":
        raise ValueError("scientific routing action does not target an active plan node")

    source = context["source"]
    review = context["review"]
    review_assignment = context["review_assignment"]
    source_id = str(source["assignment_id"])
    review_id = str(review["handoff_id"])
    review_assignment_id = str(review_assignment["assignment_id"])
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
            "evidence_refs": list(context["evidence_refs"]),
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


def _decision_identity(action: Dict[str, Any]) -> Dict[str, Any]:
    if action.get("kind") != "SCIENTIFIC_ROUTE_REQUIRED":
        raise ValueError("action is not a scientific routing ambiguity")
    candidates = action.get("candidate_routes")
    if candidates != list(_ROUTES):
        raise ValueError("scientific route candidates do not match the S6-A contract")
    identity = {
        "schema_version": 1,
        "plan_version": int(action.get("plan_version") or 0),
        "node_id": str(action.get("node_id") or ""),
        "subject_type": str(action.get("subject_type") or ""),
        "subject_id": str(action.get("subject_id") or ""),
        "review_handoff_id": str(action.get("review_handoff_id") or ""),
        "review_assignment_id": str(action.get("review_assignment_id") or ""),
        "candidate_routes": list(candidates),
    }
    if (
        identity["plan_version"] <= 0
        or not identity["node_id"]
        or identity["subject_type"] != "ASSIGNMENT"
        or not identity["subject_id"]
        or not identity["review_handoff_id"]
        or not identity["review_assignment_id"]
    ):
        raise ValueError("scientific route identity is incomplete")
    return identity


def _decision_key(action: Dict[str, Any]) -> str:
    return ProgressLedger._content_id(_decision_identity(action))


def _decision_semantics(event: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in event.items() if key not in {"event", "at"}}


def _load_route_decision(
    ledger: ProgressLedger,
    decision_key: str,
    identity: Dict[str, Any],
) -> Dict[str, Any] | None:
    matches = [
        dict(event)
        for event in read_jsonl(ledger.path)
        if event.get("event") == _DECISION_EVENT
        and event.get("decision_key") == decision_key
    ]
    if not matches:
        return None
    first = matches[0]
    expected = {**identity, "decision_key": decision_key}
    if any(first.get(key) != value for key, value in expected.items()):
        raise ValueError("durable route decision identity conflict")
    semantics = _decision_semantics(first)
    if any(_decision_semantics(item) != semantics for item in matches[1:]):
        raise ValueError("conflicting durable route decisions share one decision_key")
    route = str(first.get("route") or "")
    if route not in _ROUTES or not str(first.get("request") or "").strip():
        raise ValueError("durable route decision is invalid")
    return first


def _handoff_request(
    route: str,
    decision_key: str,
    action: Dict[str, Any],
    rationale: str,
    basis: List[str],
    packet: Dict[str, Any] | None = None,
) -> str:
    source_id = str(action.get("subject_id") or "")
    review_id = str(action.get("review_handoff_id") or "")
    evidence = ""
    if packet:
        refs = list(packet["source"].get("evidence_refs") or [])
        evidence = (
            "\nSource final result:\n"
            + str(packet["source"].get("final_message") or "")
            + "\n\nIndependent review result:\n"
            + str(packet["review"].get("final_message") or "")
            + "\n"
        )
        if refs:
            evidence += "\nInherited Evidence Records (immutable):\n" + "".join(
                f"- `{evidence_id}`\n" for evidence_id in refs
            )
    common = (
        "# Supervisor Scientific Route\n\n"
        f"Decision `{decision_key}` routes source assignment `{source_id}` after generic review "
        f"`{review_id}`.\n\nSupervisor rationale:\n{rationale}\n\n"
        "Decision basis:\n" + "".join(f"- {item}\n" for item in basis) + evidence + "\n"
    )
    source_role = str(action.get("source_role") or "researcher").lower()
    continuation = (
        "This handoff supersedes the blocked Researcher assignment. If no structural Plan change is needed, "
        "complete the remaining source-node Researcher work in this assignment; COMPLETE means that Researcher "
        "work is fully done. If a structural Plan change is needed, use the existing governed Plan proposal "
        "mechanism before COMPLETE. "
        if source_role == "researcher"
        else "If no structural Plan change is needed, return a bounded scientific result for the blocked source "
        "assignment; the control plane may resume that same assignment with your result. "
    )
    if route == "INTERPRET":
        instruction = (
            continuation
            + "Synthesize the scientific meaning of the reviewed evidence. State what is supported, what is not "
            "supported, and what evidence-backed next action is justified. Use only existing governed proposal "
            "mechanisms for any structural change; do not edit Plan or Progress files directly. End with COMPLETE "
            "when this interpretation work is done.\n"
        )
    elif route == "RETHINK":
        instruction = (
            continuation
            + "Substantively rethink the scientific assumption or direction that left the source assignment blocked. "
            "Do not cosmetically repeat the same route. Express any structural change only through existing governed "
            "proposal mechanisms; do not edit Plan or Progress files directly. End with COMPLETE when this rethink "
            "work is done.\n"
        )
    else:
        instruction = (
            "Autonomous scientific routing stops here. The main/human control surface must inspect this decision, "
            "the source assignment, and its completed review before choosing any further action.\n"
        )
    return common + instruction


def _decision_event(
    action: Dict[str, Any],
    decision_key: str,
    *,
    route: str,
    rationale: str,
    basis: List[str],
    decision_source: str,
    packet: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    identity = _decision_identity(action)
    reason = (
        "restricted supervisor fail-closed escalation"
        if decision_source == "fail_closed"
        else f"restricted supervisor selected {route.lower()}"
    )
    return {
        "event": _DECISION_EVENT,
        "decision_key": decision_key,
        **identity,
        "source_role": str(action.get("source_role") or "researcher"),
        "evidence_refs": list((packet or {}).get("source", {}).get("evidence_refs") or []),
        "route": route,
        "rationale": rationale,
        "basis": list(basis),
        "decision_source": decision_source,
        "model": _MODEL,
        "effort": _EFFORT,
        "handoff_reason": reason,
        "request": _handoff_request(route, decision_key, action, rationale, list(basis), packet),
        "at": utc_now(),
    }


def _persist_route_decision(
    ledger: ProgressLedger,
    event: Dict[str, Any],
    identity: Dict[str, Any],
) -> Dict[str, Any]:
    decision_key = str(event["decision_key"])
    existing = _load_route_decision(ledger, decision_key, identity)
    if existing is not None:
        if _decision_semantics(existing) != _decision_semantics(event):
            raise ValueError("attempted to replace an existing durable route decision")
        return existing
    append_jsonl(ledger.path, event)
    persisted = _load_route_decision(ledger, decision_key, identity)
    if persisted is None:
        raise OSError("durable route decision was not readable after append")
    return persisted


def _materialize_decision(
    ledger: ProgressLedger,
    decision: Dict[str, Any],
    *,
    evidence_refs: List[str] | None = None,
) -> Dict[str, Any]:
    handoffs = HandoffRequests(ledger)
    subject_id = str(decision["subject_id"])
    route = str(decision["route"])
    node_id = str(decision["node_id"])
    if ledger._is_plan_stale({
        "plan_version": int(decision["plan_version"]),
        "node_id": node_id,
    }):
        raise ValueError("durable route decision plan binding is stale")
    current_version = ResearchPlanGraph(ledger.root).snapshot()["plan_version"]
    raw_refs = decision.get("evidence_refs") if "evidence_refs" in decision else (evidence_refs or [])
    refs = list(dict.fromkeys(
        str(value).strip() for value in (raw_refs or []) if str(value).strip()
    ))
    lineage = [
        item
        for item in handoffs.all()
        if item.get("kind") in _ROUTES
        and item.get("subject_type") == "ASSIGNMENT"
        and item.get("subject_id") == subject_id
    ]
    if lineage:
        if len(lineage) != 1:
            raise ValueError("multiple scientific route handoffs exist for one source assignment")
        current = lineage[0]
        expected = {
            "kind": route,
            "node_id": node_id,
            "source_assignment_id": subject_id,
            "parent_handoff_id": decision["review_handoff_id"],
            "requested_by": "supervisor",
            "source_role": decision["source_role"],
            "reason": decision["handoff_reason"],
        }
        if any(current.get(key) != value for key, value in expected.items()):
            raise ValueError("existing scientific route handoff conflicts with durable decision")
        if ledger._is_plan_stale({
            "plan_version": int(current.get("plan_version") or 0),
            "node_id": node_id,
        }):
            raise ValueError("existing scientific route handoff plan binding is stale")
        if list(current.get("evidence_refs") or []) != refs:
            raise ValueError("existing scientific route evidence conflicts with durable decision")
        if str(current.get("request") or "").strip() != str(decision["request"]).strip():
            raise ValueError("existing scientific route request conflicts with durable decision")
        return current

    return handoffs.open(
        kind=route,
        plan_version=int(current_version),
        node_id=node_id,
        subject_type="ASSIGNMENT",
        subject_id=subject_id,
        requested_by="supervisor",
        source_role=str(decision["source_role"]),
        source_assignment_id=subject_id,
        parent_handoff_id=str(decision["review_handoff_id"]),
        request=str(decision["request"]),
        reason=str(decision["handoff_reason"]),
        evidence_refs=refs,
    )


def _decision_report(
    decision: Dict[str, Any],
    handoff: Dict[str, Any],
    *,
    replayed: bool,
    usage: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return {
        "decision_key": decision["decision_key"],
        "subject_id": decision["subject_id"],
        "status": "valid" if decision["decision_source"] == "llm" else "fail_closed",
        "route": decision["route"],
        "rationale": decision["rationale"],
        "basis": decision["basis"],
        "decision_source": decision["decision_source"],
        "model": decision["model"],
        "effort": decision["effort"],
        "usage": usage or {},
        "replayed": replayed,
        "handoff_id": handoff.get("handoff_id"),
        "handoff_status": handoff.get("status"),
    }


def _route_stop_reason(root: Path, token_budget: int) -> str:
    from . import cli as CLI

    rows = [CLI.worker_status(L.WorkerLayout(path)) for path in sorted((root / "workers").glob("*")) if path.is_dir()]
    report = CLI.reconcile(root, rows)
    if report.get("project_state") != "RUNNING":
        return f"project_state:{report.get('project_state')}"
    CLI._apply_resource_budget(report, rows, CLI._resource_snapshot(root, rows, token_budget))
    if report.get("budget_stop"):
        return str(report["budget_stop"])
    if CLI.quota_policy(CLI.Q.load_quota(root)).get("block_new_codex_rounds"):
        return "quota_block_new_codex_rounds"
    return ""


def resolve_scientific_routes(root: Path, actions: List[Dict[str, Any]], token_budget: int = 0) -> List[Dict[str, Any]]:
    """Persist/replay bounded decisions, then idempotently open their existing Handoffs."""
    from . import cli as CLI

    root = Path(root)
    out: List[Dict[str, Any]] = []
    with CLI.project_control_lock(root):
        ledger = ProgressLedger(root)
        for action in actions:
            if action.get("kind") != "SCIENTIFIC_ROUTE_REQUIRED":
                continue
            stop = _route_stop_reason(root, token_budget)
            if stop:
                out.append({"subject_id": action.get("subject_id"), "status": "blocked", "reason": stop})
                continue
            subject_id = str(action.get("subject_id") or "")
            try:
                identity = _decision_identity(action)
                decision_key = ProgressLedger._content_id(identity)
                existing = _load_route_decision(ledger, decision_key, identity)
            except ValueError as exc:
                out.append({
                    "subject_id": subject_id,
                    "status": "failed",
                    "error": f"decision_conflict:{exc}",
                    "model": _MODEL,
                    "effort": _EFFORT,
                    "usage": {},
                })
                continue

            usage: Dict[str, Any] = {}
            replayed = existing is not None
            decision = existing
            if decision is None:
                try:
                    packet = build_route_packet(root, action)
                except (FileNotFoundError, ValueError) as exc:
                    out.append({
                        "subject_id": subject_id,
                        "decision_key": decision_key,
                        "status": "failed",
                        "error": f"invalid_context:{exc}",
                        "model": _MODEL,
                        "effort": _EFFORT,
                        "usage": {},
                    })
                    continue

                try:
                    result = _run_luna(_prompt(packet))
                except Exception as exc:
                    result = {
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                        "tool_calls": 0,
                        "reply": "",
                        "usage": {},
                    }
                usage = result.get("usage") or {}
                validated = _validate(packet, result)
                if validated["status"] == "valid":
                    decision = _decision_event(
                        action,
                        decision_key,
                        route=str(validated["route"]),
                        rationale=str(validated["rationale"]),
                        basis=list(validated["basis"]),
                        decision_source="llm",
                        packet=packet,
                    )
                else:
                    error = str(validated.get("error") or "model_call_failed")
                    decision = _decision_event(
                        action,
                        decision_key,
                        route="ESCALATE",
                        rationale=f"Restricted Supervisor failed closed: {error}",
                        basis=list(packet["allowed_basis"]),
                        decision_source="fail_closed",
                        packet=packet,
                    )
                try:
                    decision = _persist_route_decision(ledger, decision, identity)
                except (OSError, ValueError) as exc:
                    out.append({
                        "subject_id": subject_id,
                        "decision_key": decision_key,
                        "status": "failed",
                        "error": f"decision_persist_failed:{exc}",
                        "model": _MODEL,
                        "effort": _EFFORT,
                        "usage": usage,
                    })
                    continue

            stop = _route_stop_reason(root, token_budget)
            if stop:
                out.append({"subject_id": subject_id, "status": "persisted_unmaterialized", "reason": stop})
                continue
            try:
                context = _route_context(root, action)
            except ValueError as exc:
                out.append({
                    "decision_key": decision_key,
                    "subject_id": subject_id,
                    "status": "persisted_unmaterialized",
                    "route": decision.get("route"),
                    "decision_source": decision.get("decision_source"),
                    "error": f"route_context_invalid:{exc}",
                    "model": _MODEL,
                    "effort": _EFFORT,
                    "usage": usage,
                    "replayed": replayed,
                })
                continue

            try:
                handoff = _materialize_decision(
                    ledger,
                    decision,
                    evidence_refs=list(context["evidence_refs"]),
                )
            except ValueError as exc:
                out.append({
                    "decision_key": decision_key,
                    "subject_id": subject_id,
                    "status": "persisted_unmaterialized",
                    "route": decision.get("route"),
                    "decision_source": decision.get("decision_source"),
                    "error": f"handoff_materialization_failed:{exc}",
                    "model": _MODEL,
                    "effort": _EFFORT,
                    "usage": usage,
                    "replayed": replayed,
                })
                continue
            out.append(_decision_report(decision, handoff, replayed=replayed, usage=usage))
    return out


def consult_scientific_routes(root: Path, actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compatibility alias retained for callers from S6-B."""
    return resolve_scientific_routes(root, actions)


def _proposal_from_route(root: Path, assignment_id: str) -> bool:
    if (root / ".danus" / "plan_proposal.json").is_file():
        return True
    directory = root / "planning" / "proposals"
    if not directory.is_dir():
        return False
    for path in directory.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(record.get("source_assignment_id") or "") == assignment_id:
            return True
    return False


def _route_consumed(ledger: ProgressLedger, source_id: str, handoff_id: str) -> bool:
    prefix = f"resumed after scientific route {handoff_id}"
    return any(
        event.get("event") == "STATE"
        and event.get("assignment_id") == source_id
        and event.get("actor") == "supervisor"
        and str(event.get("note") or "").startswith(prefix)
        for event in read_jsonl(ledger.path)
    )


def _researcher_route_authoritative(
    ledger: ProgressLedger,
    node_id: str,
    route_assignment_id: str,
    handoff_id: str,
) -> bool:
    events = read_jsonl(ledger.path)
    completed_at = -1
    for index, event in enumerate(events):
        if (
            event.get("event") == "STATE"
            and event.get("assignment_id") == route_assignment_id
            and event.get("state") == "DONE"
            and event.get("outcome") == "COMPLETE"
        ):
            completed_at = index
    if completed_at < 0:
        return False

    marker = f"scientific route {handoff_id} completed Researcher source:"
    for event in events[completed_at + 1:]:
        if (
            event.get("event") == "ASSIGNED"
            and str(event.get("node_id") or "") == node_id
            and event.get("assignment_id") != route_assignment_id
        ):
            return False
        if event.get("event") == "NODE_STATE" and str(event.get("node_id") or "") == node_id:
            if event.get("state") == "OPEN":
                return False
            if event.get("state") == "DONE" and str(event.get("note") or "").startswith(marker):
                return False
    return True


def apply_scientific_route_dispositions(
    project: str,
    token_budget: int = 0,
) -> List[Dict[str, Any]]:
    """Consume completed Assignment routes only while the control plane permits progress."""
    from . import cli as CLI

    root = L.project_dir(project)
    out: List[Dict[str, Any]] = []
    with CLI.project_control_lock(root):
        ledger = ProgressLedger(root)
        routes = [
            handoff
            for handoff in HandoffRequests(ledger).all()
            if handoff.get("kind") in {"INTERPRET", "RETHINK"}
            and handoff.get("subject_type") == "ASSIGNMENT"
            and handoff.get("status") == "RESOLVED"
        ]
        if not routes:
            return out

        control = CLI._do_supervise_unlocked(
            project,
            apply=False,
            token_budget=token_budget,
            trigger="scientific_route_disposition_check",
        )
        if control.get("project_state") != "RUNNING":
            reason = f"project_state:{control.get('project_state') or 'UNKNOWN'}"
            return [
                {
                    "handoff_id": handoff["handoff_id"],
                    "source_assignment_id": str(handoff.get("subject_id") or ""),
                    "result": "blocked",
                    "reason": reason,
                }
                for handoff in routes
            ]
        if control.get("budget_stop"):
            reason = str(control["budget_stop"])
            return [
                {
                    "handoff_id": handoff["handoff_id"],
                    "source_assignment_id": str(handoff.get("subject_id") or ""),
                    "result": "blocked",
                    "reason": reason,
                }
                for handoff in routes
            ]

        assignments = {
            str(item.get("assignment_id") or ""): item
            for item in ledger.assignments()
            if item.get("assignment_id")
        }
        completed = set(ledger.completed_nodes())
        for handoff in routes:
            source_id = str(handoff.get("subject_id") or "")
            source = assignments.get(source_id)
            route_assignment_id = str(handoff.get("resolution_assignment_id") or "")
            route_assignment = assignments.get(route_assignment_id)
            if (
                source is None
                or route_assignment is None
                or route_assignment.get("state") != "DONE"
                or route_assignment.get("outcome") != "COMPLETE"
                or str(route_assignment.get("role") or "").lower() != "researcher"
            ):
                continue

            if ledger._reopened_after(source) or ledger._reopened_after(route_assignment):
                continue
            source_stale = ledger._is_plan_stale(source)
            route_stale = ledger._is_plan_stale(route_assignment)
            if source_stale or route_stale:
                worker = str(source.get("worker") or "")
                if worker:
                    current = ledger.current(worker, refresh_stale=True)
                    if current and current.get("assignment_id") == source_id:
                        assignments[source_id] = current
                out.append({
                    "handoff_id": handoff["handoff_id"],
                    "source_assignment_id": source_id,
                    "result": "stale",
                    "reason": (
                        "source_plan_binding_stale"
                        if source_stale
                        else "route_plan_binding_stale"
                    ),
                })
                continue

            node_id = str(handoff.get("node_id") or "")
            if _proposal_from_route(root, route_assignment_id):
                out.append({
                    "handoff_id": handoff["handoff_id"],
                    "source_assignment_id": source_id,
                    "result": "plan_proposal",
                })
                continue

            source_role = str(source.get("role") or "").lower()
            source_worker = str(source.get("worker") or "")
            wl = L.WorkerLayout(L.worker_dir(project, source_worker)) if source_worker else None
            if source_role == "experimenter":
                consumed = _route_consumed(ledger, source_id, handoff["handoff_id"])
                if consumed:
                    if source.get("state") == "BLOCKED" and source.get("outcome") == "NEEDS_REVIEW":
                        out.append({
                            "handoff_id": handoff["handoff_id"],
                            "source_assignment_id": source_id,
                            "result": "awaiting_human",
                        })
                    elif source.get("state") in {"ASSIGNED", "RUNNING"} and wl is not None:
                        pause_reason = CLI._pause_reason(wl)
                        if pause_reason and pause_reason != "review:assignment_needs_review":
                            out.append({
                                "handoff_id": handoff["handoff_id"],
                                "source_assignment_id": source_id,
                                "result": "paused",
                                "reason": pause_reason,
                            })
                            continue
                        if pause_reason == "review:assignment_needs_review":
                            wl.pause.unlink(missing_ok=True)
                        start_result = CLI._start_one(wl)
                        out.append({
                            "handoff_id": handoff["handoff_id"],
                            "source_assignment_id": source_id,
                            "result": "restarted",
                            "start_result": start_result,
                        })
                    continue
                if source.get("state") != "BLOCKED" or source.get("outcome") != "NEEDS_REVIEW":
                    continue
                if wl is None:
                    continue
                pause_reason = CLI._pause_reason(wl)
                if pause_reason and pause_reason != "review:assignment_needs_review":
                    out.append({
                        "handoff_id": handoff["handoff_id"],
                        "source_assignment_id": source_id,
                        "result": "paused",
                        "reason": pause_reason,
                    })
                    continue
                try:
                    result = _checkpoint_message(root, route_assignment)
                except ValueError:
                    continue
                if pause_reason == "review:assignment_needs_review":
                    wl.pause.unlink(missing_ok=True)
                note = (
                    f"resumed after scientific route {handoff['handoff_id']}\n\n"
                    f"{handoff['kind']} result:\n{result}"
                )
                ledger.update(
                    worker=source_worker,
                    state="RUNNING",
                    actor="supervisor",
                    note=note,
                    expected_assignment_id=source_id,
                )
                start_result = CLI._start_one(wl)
                out.append({
                    "handoff_id": handoff["handoff_id"],
                    "source_assignment_id": source_id,
                    "result": "resumed",
                    "start_result": start_result,
                })
                continue

            if source_role == "researcher" and node_id not in completed:
                if not _researcher_route_authoritative(
                    ledger, node_id, route_assignment_id, handoff["handoff_id"]
                ):
                    continue
                if wl is None:
                    continue
                pause_reason = CLI._pause_reason(wl)
                if pause_reason and pause_reason != "review:assignment_needs_review":
                    out.append({
                        "handoff_id": handoff["handoff_id"],
                        "source_assignment_id": source_id,
                        "result": "paused",
                        "reason": pause_reason,
                    })
                    continue
                try:
                    result = _checkpoint_message(root, route_assignment)
                except ValueError:
                    continue
                if pause_reason == "review:assignment_needs_review":
                    wl.pause.unlink(missing_ok=True)
                ledger.set_node_state(
                    node_id,
                    state="DONE",
                    actor="supervisor",
                    note=f"scientific route {handoff['handoff_id']} completed Researcher source: {result}",
                )
                completed.add(node_id)
                out.append({
                    "handoff_id": handoff["handoff_id"],
                    "source_assignment_id": source_id,
                    "node_id": node_id,
                    "result": "completed",
                })
    return out


def _eligible(report: Dict[str, Any]) -> bool:
    return bool(
        report.get("project_state") == "RUNNING"
        and report.get("convergence", {}).get("stable")
        and not report.get("budget_stop")
        and not report.get("quota_policy", {}).get("block_new_codex_rounds")
    )


def _route_report(project: str, report: Dict[str, Any], token_budget: int) -> Dict[str, Any]:
    from . import cli as CLI

    report = dict(report)
    decisions: List[Dict[str, Any]] = []
    if _eligible(report):
        decisions = resolve_scientific_routes(L.project_dir(project), report.get("actions", []), token_budget=token_budget)
    report["scientific_route_decisions"] = decisions
    if any(item.get("handoff_id") or item.get("reason") for item in decisions):
        report = CLI.do_supervise(
            project,
            apply=True,
            token_budget=token_budget,
            trigger="scientific_route",
        )
        report["scientific_route_decisions"] = decisions

    dispositions = apply_scientific_route_dispositions(project, token_budget=token_budget)
    report["scientific_route_dispositions"] = dispositions
    if any(item.get("result") in {"resumed", "restarted", "completed"} for item in dispositions):
        refreshed = CLI.do_supervise(
            project,
            apply=True,
            token_budget=token_budget,
            trigger="scientific_route_disposition",
        )
        refreshed["scientific_route_decisions"] = decisions
        refreshed["scientific_route_dispositions"] = dispositions
        return refreshed
    return report


def supervise_main(argv: List[str]) -> int:
    """CLI seam for manual and watch-mode durable restricted scientific routing."""
    from . import cli as CLI

    args = CLI.build_parser().parse_args(["supervise", *argv])
    if args.watch:
        if args.complete_node or args.reopen_node:
            raise SystemExit("--watch cannot be combined with node state mutation")
        from . import planning

        watcher = (
            planning.watch_research(
                args.project,
                heartbeat=args.heartbeat,
                poll=args.poll,
                token_budget=args.token_budget,
            )
            if (L.project_dir(args.project) / planning.BRIEF_FILE).is_file()
            else CLI.watch_supervisor(
                args.project,
                heartbeat=args.heartbeat,
                poll=args.poll,
                token_budget=args.token_budget,
            )
        )
        for report in watcher:
            routed = _route_report(args.project, report, args.token_budget)
            print(json.dumps(routed, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    if not args.apply:
        return CLI.main(["supervise", *argv])

    report = CLI.do_supervise(
        args.project,
        complete_node=args.complete_node,
        reopen_node=args.reopen_node,
        apply=True,
        token_budget=args.token_budget,
    )
    routed = _route_report(args.project, report, args.token_budget)
    print(json.dumps(routed, ensure_ascii=False, indent=2))
    return 0


def research_main(argv: List[str]) -> int:
    """Run the existing research planner with the same durable S6 route seam."""
    from . import cli as CLI
    from . import planning

    parser = argparse.ArgumentParser(
        prog="danus research",
        description="Bootstrap/resume autonomous planning and DAG execution from a research brief.",
    )
    parser.add_argument("project")
    parser.add_argument("--brief", default=None, help="research brief text file; required for a new project")
    parser.add_argument("--stdin", action="store_true", help="read a new-project research brief from stdin")
    parser.add_argument("--model", default=None, help="model for a newly scaffolded project")
    parser.add_argument("--once", action="store_true", help="run one planning + Supervisor pass and return")
    parser.add_argument("--heartbeat", type=float, default=1800.0)
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument("--token-budget", type=int, default=0)
    args = parser.parse_args(argv)

    init = planning.initialize_research(
        args.project,
        brief=planning._brief_from_args(args),
        model=args.model,
    )
    print(json.dumps({"research_init": init}, ensure_ascii=False, sort_keys=True), flush=True)
    if args.once:
        applied = planning.planning_step(args.project, token_budget=args.token_budget)
        report = CLI.do_supervise(
            args.project,
            apply=True,
            token_budget=args.token_budget,
            trigger="research",
        )
        report = _route_report(args.project, report, args.token_budget)
        if applied:
            report["planning_applied"] = applied
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
        return 0

    for report in planning.watch_research(
        args.project,
        heartbeat=args.heartbeat,
        poll=args.poll,
        token_budget=args.token_budget,
    ):
        routed = _route_report(args.project, report, args.token_budget)
        print(json.dumps(routed, ensure_ascii=False, sort_keys=True), flush=True)
    return 0
