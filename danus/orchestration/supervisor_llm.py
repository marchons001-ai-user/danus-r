"""Restricted LLM routing for S6 scientific ambiguities.

The deterministic reducer remains authoritative. The restricted Luna router is
used only for an already-bounded ``SCIENTIFIC_ROUTE_REQUIRED`` action. Accepted
or fail-closed decisions are persisted before their existing Handoff is opened,
so cold-start replay never needs a second accepted route decision.
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
from danus.core._util import append_jsonl, read_jsonl, utc_now
from danus.core.handoff import HandoffRequests
from danus.execution import context as C
from danus.execution import layout as L

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
) -> str:
    source_id = str(action.get("subject_id") or "")
    review_id = str(action.get("review_handoff_id") or "")
    common = (
        "# Supervisor Scientific Route\n\n"
        f"Decision `{decision_key}` routes source assignment `{source_id}` after generic review "
        f"`{review_id}`.\n\nSupervisor rationale:\n{rationale}\n\n"
        "Decision basis:\n" + "".join(f"- {item}\n" for item in basis) + "\n"
    )
    if route == "INTERPRET":
        instruction = (
            "Synthesize the scientific meaning of the reviewed evidence. State what is supported, what is not "
            "supported, and what evidence-backed next action is justified. Use only existing governed proposal "
            "mechanisms for any structural change; do not edit Plan or Progress files directly. End with COMPLETE "
            "when this interpretation work is done.\n"
        )
    elif route == "RETHINK":
        instruction = (
            "Substantively rethink the scientific assumption or direction that left the source assignment blocked. "
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
        "route": route,
        "rationale": rationale,
        "basis": list(basis),
        "decision_source": decision_source,
        "model": _MODEL,
        "effort": _EFFORT,
        "handoff_reason": reason,
        "request": _handoff_request(route, decision_key, action, rationale, list(basis)),
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


def _materialize_decision(ledger: ProgressLedger, decision: Dict[str, Any]) -> Dict[str, Any]:
    handoffs = HandoffRequests(ledger)
    subject_id = str(decision["subject_id"])
    route = str(decision["route"])
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
            "plan_version": decision["plan_version"],
            "node_id": decision["node_id"],
            "source_assignment_id": subject_id,
            "parent_handoff_id": decision["review_handoff_id"],
            "requested_by": "supervisor",
            "source_role": decision["source_role"],
            "reason": decision["handoff_reason"],
        }
        if any(current.get(key) != value for key, value in expected.items()):
            raise ValueError("existing scientific route handoff conflicts with durable decision")
        if str(current.get("request") or "").strip() != str(decision["request"]).strip():
            raise ValueError("existing scientific route request conflicts with durable decision")
        return current

    return handoffs.open(
        kind=route,
        plan_version=int(decision["plan_version"]),
        node_id=str(decision["node_id"]),
        subject_type="ASSIGNMENT",
        subject_id=subject_id,
        requested_by="supervisor",
        source_role=str(decision["source_role"]),
        source_assignment_id=subject_id,
        parent_handoff_id=str(decision["review_handoff_id"]),
        request=str(decision["request"]),
        reason=str(decision["handoff_reason"]),
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


def resolve_scientific_routes(root: Path, actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Persist/replay bounded decisions, then idempotently open their existing Handoffs."""
    from . import cli as CLI

    root = Path(root)
    out: List[Dict[str, Any]] = []
    with CLI.project_control_lock(root):
        ledger = ProgressLedger(root)
        for action in actions:
            if action.get("kind") != "SCIENTIFIC_ROUTE_REQUIRED":
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

            try:
                handoff = _materialize_decision(ledger, decision)
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
    report["scientific_route_decisions"] = []
    if not _eligible(report):
        return report
    decisions = resolve_scientific_routes(L.project_dir(project), report.get("actions", []))
    report["scientific_route_decisions"] = decisions
    if not any(item.get("handoff_id") for item in decisions):
        return report

    followup = CLI.do_supervise(
        project,
        apply=True,
        token_budget=token_budget,
        trigger="scientific_route",
    )
    followup["scientific_route_decisions"] = decisions
    return followup


def supervise_main(argv: List[str]) -> int:
    """CLI seam for manual and watch-mode durable restricted scientific routing."""
    from . import cli as CLI

    args = CLI.build_parser().parse_args(["supervise", *argv])
    if args.watch:
        if args.complete_node or args.reopen_node:
            raise SystemExit("--watch cannot be combined with node state mutation")
        for report in CLI.watch_supervisor(
            args.project,
            heartbeat=args.heartbeat,
            poll=args.poll,
            token_budget=args.token_budget,
        ):
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
