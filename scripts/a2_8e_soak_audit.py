#!/usr/bin/env python3
"""Read-only acceptance audit for the A2-8e autonomous long-run soak."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from danus.core import ResearchPlanGraph
from danus.core._util import read_jsonl
from danus.execution import layout as L
from danus.orchestration import cli
from danus.orchestration.control import reconcile

_THREAD_NOISE = "failed to record rollout items: thread not found"


def _load_transcript(path: Path) -> tuple[list[dict[str, Any]], str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records, text


def _planning_actions(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        action
        for record in records
        for action in record.get("planning_applied", [])
        if isinstance(action, dict)
    ]


def _repair_token_estimates(
    records: list[dict[str, Any]], repairs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    estimates: list[dict[str, Any]] = []
    for repair in repairs:
        assignment_id = str(repair.get("assignment_id") or "")
        worker = str(repair.get("worker") or "")
        tokens = None
        for record in records:
            decisions = record.get("decisions", [])
            if not any(
                isinstance(item, dict)
                and str(item.get("assignment_id") or "") == assignment_id
                and item.get("action") == "REVIEW"
                and item.get("reason") == "completion_not_recorded"
                for item in decisions
            ):
                continue
            for row in record.get("budget", {}).get("context", []):
                if isinstance(row, dict) and str(row.get("worker") or "") == worker:
                    value = row.get("round_usage_tokens")
                    if isinstance(value, int):
                        tokens = value
                    break
            if tokens is not None:
                break
        estimates.append(
            {
                "assignment_id": assignment_id,
                "worker": worker,
                "attempt": repair.get("attempt"),
                "round_usage_tokens": tokens,
            }
        )
    return estimates


def audit(project: str, transcript: Path) -> dict[str, Any]:
    pdir = L.project_dir(project)
    if not pdir.is_dir():
        raise SystemExit(f"no such project: {project}")
    fixture_path = pdir / "soak" / "fixture.json"
    if not fixture_path.is_file():
        raise SystemExit(f"missing A2-8e fixture manifest: {fixture_path}")
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    records, transcript_text = _load_transcript(transcript)
    if not records:
        raise SystemExit("transcript contains no Danus JSON records")

    automatic_assignment_ids: set[str] = set()
    for record in records:
        init = record.get("research_init")
        if isinstance(init, dict) and init.get("bootstrap_assignment_id"):
            automatic_assignment_ids.add(str(init["bootstrap_assignment_id"]))
        for key in ("planning_applied", "autonomy_applied"):
            for action in record.get(key, []):
                if isinstance(action, dict) and action.get("assignment_id"):
                    automatic_assignment_ids.add(str(action["assignment_id"]))

    supervisor_events = read_jsonl(pdir / "supervisor" / "events.jsonl")
    for event in supervisor_events:
        for action in event.get("autonomy_applied", []):
            if isinstance(action, dict) and action.get("assignment_id"):
                automatic_assignment_ids.add(str(action["assignment_id"]))

    ledger_events = read_jsonl(pdir / "progress" / "ledger.jsonl")
    assignments = [event for event in ledger_events if event.get("event") == "ASSIGNED"]
    node_events = [event for event in ledger_events if event.get("event") == "NODE_STATE"]
    unexplained_assignments = [
        {
            "assignment_id": event.get("assignment_id"),
            "worker": event.get("worker"),
            "role": event.get("role"),
            "node_id": event.get("node_id"),
        }
        for event in assignments
        if str(event.get("assignment_id") or "") not in automatic_assignment_ids
    ]
    nonautomatic_node_events = [
        {
            "node_id": event.get("node_id"),
            "state": event.get("state"),
            "note": event.get("note"),
            "plan_version": event.get("plan_version"),
        }
        for event in node_events
        if not str(event.get("note") or "").startswith("automatic")
    ]

    planning = _planning_actions(records)
    blocked_proposals = [
        item for item in planning
        if item.get("action") == "PLAN_PROPOSAL" and item.get("result") == "blocked"
    ]
    captured_proposals = [
        item for item in planning
        if item.get("action") == "PLAN_PROPOSAL"
        and item.get("result") in {"captured", "already_captured"}
    ]
    repairs = [
        item for item in planning
        if item.get("action") == "PLAN_REPAIR" and item.get("result") == "assigned"
    ]
    repair_exhausted = [
        item for item in planning
        if item.get("action") == "PLAN_REPAIR" and item.get("result") == "repair_exhausted"
    ]
    repair_tokens = _repair_token_estimates(records, repairs)

    changes: list[dict[str, Any]] = []
    for path in sorted((pdir / "plan" / "changes").glob("*.json")):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict):
            changes.append(obj)
    committed = [item for item in changes if item.get("status") == "COMMITTED"]
    autonomous_committed = [
        item for item in committed if str(item.get("reason") or "").startswith("[proposal:")
    ]
    post_hoc_committed = [item for item in autonomous_committed if item.get("post_hoc") is True]
    nongoverned_commits = [
        {
            "change_id": item.get("change_id"),
            "reason": item.get("reason"),
            "committer": item.get("committer"),
        }
        for item in committed
        if not str(item.get("reason") or "").startswith("[proposal:")
        or item.get("committer") != "supervisor"
    ]

    role_assignment_counts = Counter(str(event.get("role") or "") for event in assignments)

    quota_policies = [
        event.get("quota_policy", {})
        for event in supervisor_events
        if isinstance(event.get("quota_policy"), dict)
    ]
    quota_states = Counter(str(policy.get("state") or "UNKNOWN") for policy in quota_policies)
    projected = [
        float(policy["projected_weekly_burn_to_reset"])
        for policy in quota_policies
        if isinstance(policy.get("projected_weekly_burn_to_reset"), (int, float))
    ]
    quota_blocks = sum(bool(policy.get("block_new_codex_rounds")) for policy in quota_policies)

    log_noise = 0
    for path in sorted((pdir / "workers").glob("*/logs/*")):
        if not path.is_file():
            continue
        try:
            log_noise += path.read_text(encoding="utf-8", errors="replace").count(_THREAD_NOISE)
        except OSError:
            pass

    rows = cli.do_status(project)
    final_report = reconcile(pdir, rows)
    live_workers = [row["worker"] for row in rows if row.get("alive")]

    final_budget = None
    for record in reversed(records):
        tokens = record.get("budget", {}).get("tokens")
        if isinstance(tokens, dict):
            final_budget = tokens
            break

    graph = ResearchPlanGraph(pdir)
    plan = graph.snapshot()
    checks = {
        "clean_complete": (
            final_report.get("project_state") == "COMPLETE"
            and not final_report.get("frontier")
            and not final_report.get("decisions")
        ),
        "plan_revised_after_initial_commit": int(plan.get("plan_version") or 0) >= 3,
        "at_least_two_autonomous_commits": len(autonomous_committed) >= 2,
        "post_hoc_revision_committed": len(post_hoc_committed) >= 1,
        "all_scientific_roles_executed": all(
            role_assignment_counts.get(role, 0) >= 1
            for role in ("researcher", "reviewer", "experimenter")
        ),
        "no_unexplained_assignments": not unexplained_assignments,
        "no_manual_node_state_changes": not nonautomatic_node_events,
        "no_nongoverned_plan_commits": not nongoverned_commits,
        "repair_not_exhausted": not repair_exhausted,
        "no_live_workers_at_terminal": not live_workers,
    }

    proposal_attempts = len(blocked_proposals) + len(captured_proposals)
    result = {
        "project": project,
        "accepted": all(checks.values()),
        "checks": checks,
        "final": {
            "project_state": final_report.get("project_state"),
            "plan_version": plan.get("plan_version"),
            "completed_nodes": final_report.get("completed_nodes", []),
            "frontier": final_report.get("frontier", []),
            "decisions": final_report.get("decisions", []),
            "live_workers": live_workers,
        },
        "autonomy": {
            "assignment_count": len(assignments),
            "role_assignment_counts": dict(sorted(role_assignment_counts.items())),
            "automatic_assignment_ids_seen": len(automatic_assignment_ids),
            "unexplained_assignments": unexplained_assignments,
            "nonautomatic_node_events": nonautomatic_node_events,
            "committed_plan_changes": len(committed),
            "autonomous_committed_plan_changes": len(autonomous_committed),
            "post_hoc_committed_plan_changes": len(post_hoc_committed),
            "nongoverned_commits": nongoverned_commits,
        },
        "proposal_repair_observation": {
            "proposal_attempts": proposal_attempts,
            "blocked_proposals": len(blocked_proposals),
            "captured_proposals": len(captured_proposals),
            "repairs_assigned": len(repairs),
            "repair_exhausted": len(repair_exhausted),
            "malformed_rate": (
                len(blocked_proposals) / proposal_attempts if proposal_attempts else 0.0
            ),
            "repair_round_token_estimates": repair_tokens,
            "repair_known_tokens_total": sum(
                int(item["round_usage_tokens"])
                for item in repair_tokens
                if isinstance(item.get("round_usage_tokens"), int)
            ),
        },
        "codex_upstream_noise": {
            "message": _THREAD_NOISE,
            "transcript_count": transcript_text.count(_THREAD_NOISE),
            "worker_log_count": log_noise,
            "acceptance_blocker": False,
        },
        "quota_observation": {
            "state_counts": dict(sorted(quota_states.items())),
            "block_new_codex_rounds_events": quota_blocks,
            "projected_weekly_burn_to_reset": {
                "samples": len(projected),
                "first": projected[0] if projected else None,
                "last": projected[-1] if projected else None,
                "min": min(projected) if projected else None,
                "max": max(projected) if projected else None,
            },
            "acceptance_blocker": False,
        },
        "token_budget_observation": final_budget,
        "fixture": {
            "e1_protocol_id": fixture.get("e1_protocol_id"),
            "plan_created_by_fixture": fixture.get("plan_created_by_fixture"),
            "assignments_created_by_fixture": fixture.get("assignments_created_by_fixture"),
        },
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit an A2-8e autonomous long-run soak")
    parser.add_argument("project")
    parser.add_argument("--transcript", required=True)
    args = parser.parse_args()

    result = audit(args.project, Path(args.transcript))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
