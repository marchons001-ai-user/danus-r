"""Autonomous Research Plan bootstrap and reviewed revision loop.

The LLM never mutates Plan/task control state directly. A sole Researcher writes one
structured handoff into the scientific workspace. The control plane validates and
content-addresses that proposal, persists the existing ResearchPlanGraph change on
the Researcher's behalf, routes it to the Reviewer, then commits and materializes
versioned A2-8b task specs only after Reviewer approval.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.core._util import utc_now
from danus.execution import layout as L
from danus.execution.scaffold import atomic_write

from . import autonomy as A
from . import cli

BRIEF_FILE = "RESEARCH_BRIEF.md"
_PROPOSAL_REL = Path(".danus") / "plan_proposal.json"
_BOOTSTRAP_REL = Path("planning") / "bootstrap.json"
_REPAIR_ERROR_REL = Path("planning") / "repair_error.json"
_POLICY_MARKER = "<!-- DANUS_AUTONOMOUS_PLANNING -->"
_MAX_REPAIR_ATTEMPTS = 2


def _role_value(wl: L.WorkerLayout, key: str) -> str:
    try:
        for line in wl.role.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                name, value = line.split("=", 1)
                if name.strip() == key:
                    return value.strip()
    except OSError:
        pass
    return ""


def _set_role(wl: L.WorkerLayout, role: str, *, backend: str = "") -> None:
    values: Dict[str, str] = {}
    try:
        for line in wl.role.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except OSError:
        pass
    values["ROLE"] = role
    if backend:
        values["BACKEND"] = backend
    else:
        values.pop("BACKEND", None)
    order = ["MODEL", "REASONING_EFFORT", "ROLE", "BACKEND", "DANUS_AUTHOR"]
    keys = [key for key in order if key in values]
    keys.extend(sorted(set(values) - set(keys)))
    atomic_write(wl.role, "".join(f"{key}={values[key]}\n" for key in keys))


def _role_workers(project: str) -> Dict[str, List[str]]:
    out = {role: [] for role in L.SCIENTIFIC_ROLES}
    for worker in L.list_workers(project):
        wl = L.WorkerLayout(L.worker_dir(project, worker))
        role = _role_value(wl, "ROLE").lower()
        if role in out:
            out[role].append(worker)
    return out


def _proposal_path(pdir: Path) -> Path:
    return L.scientific_workspace(pdir) / _PROPOSAL_REL


def _proposal_dir(pdir: Path) -> Path:
    return pdir / "planning" / "proposals"


def _proposal_record_path(pdir: Path, proposal_id: str) -> Path:
    return _proposal_dir(pdir) / f"{proposal_id}.json"


def _repair_error_path(pdir: Path) -> Path:
    return pdir / _REPAIR_ERROR_REL


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _content_id(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()[:16]


def _proposal_instructions(path: Path, plan_version: Optional[int]) -> str:
    if plan_version is None:
        version = "<current plan_version from the committed Research Plan>"
        version_note = (
            "Immediately before writing the proposal, read the current committed Research Plan "
            "and copy its integer `plan_version` exactly into `base_plan_version`.\n"
        )
    else:
        version = str(plan_version)
        version_note = ""
    return f"""{version_note}Write exactly one JSON object to `{path}` only if a structural Plan change is needed.
Schema:
{{
  "base_plan_version": {version},
  "reason": "why the route must change",
  "post_hoc": false,
  "evidence_refs": [],
  "operations": [ResearchPlanGraph operations],
  "tasks": [{{"node_id": "R1", "role": "researcher|reviewer|experimenter", "task": "exact task"}}]
}}
Plan operation constraints are strict: every `add_node` has exactly `op`, `node_id`, `title`, `summary`, `parent`, and `depends_on`; `title` is at most {ResearchPlanGraph.MAX_TITLE_CHARS} characters and `summary` is at most {ResearchPlanGraph.MAX_SUMMARY_CHARS} characters. Keep the Plan coarse. Provide one task for every executable leaf introduced or materially changed by the proposal; experiment cells and implementation minutiae belong inside task text, not extra Plan nodes. Result-conditioned revisions must set `post_hoc=true` and cite immutable `evidence_refs`. Do not edit Plan files or `tasks/` directly and, in this autonomous mode, do not call `plan_propose_change`; the Supervisor will content-address this handoff and submit it through the normal reviewed change gate.
"""


def _researcher_footer(path: Path) -> str:
    return (
        "\n\n## Autonomous Plan Revision\n\n"
        "If new evidence, a blocker, or changed scientific understanding makes the committed "
        "route materially wrong or incomplete, use this project handoff instead of editing Plan "
        "state or calling `plan_propose_change` directly. Otherwise create no proposal file.\n\n"
        + _proposal_instructions(path, None)
    )


def _append_project_policy(project: str) -> None:
    path = L.research_charter(project)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if _POLICY_MARKER in text:
        return
    policy = (
        f"\n\n{_POLICY_MARKER}\n"
        "## Autonomous planning control\n\n"
        "When TASK.md supplies an Autonomous Plan Revision handoff path, that handoff is the "
        "project-authorized replacement for direct `plan_propose_change` calls. The Supervisor "
        "will freeze it, submit the existing Plan change gate on the Researcher's behalf, route "
        "Reviewer approval, and materialize only the approved exact tasks. All other Research "
        "Plan governance rules remain unchanged.\n"
    )
    atomic_write(path, text.rstrip() + policy)


def _bootstrap_task(pdir: Path, plan_version: int) -> str:
    return (
        "# Autonomous Research Bootstrap\n\n"
        f"Read the binding research brief at `{pdir / BRIEF_FILE}` and RESEARCH_CHARTER.md. "
        "Inspect only the existing workspace/context needed to understand the problem. Design the "
        "smallest coarse Research Plan DAG that can answer the brief, with explicit Researcher/"
        "Reviewer/Experimenter handoffs where scientifically needed. Do not run planned experiments "
        "in this bootstrap task.\n\n"
        + _proposal_instructions(_proposal_path(pdir), plan_version)
        + "\nAfter the proposal file is fully written, end this assignment with COMPLETE.\n"
    )


def _validate_roster(project: str) -> Dict[str, List[str]]:
    roles = _role_workers(project)
    if len(roles["researcher"]) != 1:
        raise SystemExit("autonomous planning requires exactly one Researcher worker")
    if not roles["reviewer"]:
        raise SystemExit("autonomous planning requires at least one Reviewer worker")
    return roles


def initialize_research(project: str, *, brief: Optional[str] = None, model: Optional[str] = None) -> Dict[str, Any]:
    """Create or resume the minimal autonomous-research control state."""
    pdir = L.project_dir(project)
    created = False
    if not pdir.is_dir():
        if not brief or not brief.strip():
            raise SystemExit("new autonomous research project requires a non-empty brief")
        result = cli.do_new(project, roles="low:3", model=model)
        workers = result["workers"]
        _set_role(L.WorkerLayout(L.worker_dir(project, workers[0])), "researcher")
        _set_role(L.WorkerLayout(L.worker_dir(project, workers[1])), "reviewer", backend="auto")
        _set_role(L.WorkerLayout(L.worker_dir(project, workers[2])), "experimenter")
        created = True

    roles = _validate_roster(project)
    brief_path = pdir / BRIEF_FILE
    if brief is not None:
        text = brief if brief.endswith("\n") else brief + "\n"
        if brief_path.is_file() and brief_path.read_text(encoding="utf-8") != text:
            raise SystemExit("research brief already exists with different content")
        if not brief_path.is_file():
            atomic_write(brief_path, text)
    if not brief_path.is_file() or not brief_path.read_text(encoding="utf-8").strip():
        raise SystemExit("autonomous research project has no research brief")

    _append_project_policy(project)
    if not (pdir / "plan" / "current.json").is_file():
        ResearchPlanGraph.create(pdir, node_id="P0", title="Research Project", summary="Autonomous research root")

    graph = ResearchPlanGraph(pdir)
    plan = graph.snapshot()
    bootstrap_result = "not-needed"
    bootstrap_assignment = None
    if plan["plan_version"] == 1 and set(plan["nodes"]) == {plan["root_id"]}:
        researcher = roles["researcher"][0]
        ledger = ProgressLedger(pdir)
        assignments = ledger.assignments()
        record_path = pdir / _BOOTSTRAP_REL
        record: Dict[str, Any] = {}
        if record_path.is_file():
            try:
                loaded = json.loads(record_path.read_text(encoding="utf-8"))
                record = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError):
                record = {}
        recorded_id = str(record.get("assignment_id") or "")
        recorded = next((a for a in assignments if a.get("assignment_id") == recorded_id), None)
        if recorded_id:
            if (
                recorded is None
                or recorded.get("worker") != researcher
                or recorded.get("node_id") != plan["root_id"]
                or int(recorded.get("plan_version") or 0) != 1
            ):
                raise SystemExit("bootstrap provenance does not match a valid root Researcher assignment")
            bootstrap_assignment = recorded_id
            bootstrap_result = "existing"
        else:
            root_assignments = [
                a for a in assignments
                if a.get("worker") == researcher
                and a.get("node_id") == plan["root_id"]
                and int(a.get("plan_version") or 0) == 1
            ]
            root_assignments.sort(key=lambda item: str(item.get("assigned_at") or ""))
            wl = L.WorkerLayout(L.worker_dir(project, researcher))
            try:
                current_task = wl.task.read_text(encoding="utf-8")
            except OSError:
                current_task = ""
            progressed = (
                current_task.startswith("# Autonomous Research Bootstrap\n")
                or _proposal_path(pdir).is_file()
                or (_proposal_dir(pdir).is_dir() and any(_proposal_dir(pdir).glob("*.json")))
                or any(_proposal_id_from_change(item) for item in graph.pending_changes())
            )
            if root_assignments and progressed:
                prior = root_assignments[0]
                bootstrap_assignment = str(prior.get("assignment_id") or "")
                atomic_write(record_path, _json_text({
                    "assignment_id": bootstrap_assignment,
                    "worker": researcher,
                    "node_id": plan["root_id"],
                    "plan_version": 1,
                    "recorded_at": utc_now(),
                    "recovered": True,
                }))
                bootstrap_result = "existing"
            else:
                task = _bootstrap_task(pdir, 1)
                assignment = cli.do_assign(f"{project}/{researcher}", task, node_id=plan["root_id"])
                bootstrap_assignment = assignment.get("assignment_id")
                atomic_write(record_path, _json_text({
                    "assignment_id": bootstrap_assignment,
                    "worker": researcher,
                    "node_id": plan["root_id"],
                    "plan_version": 1,
                    "task_sha256": ProgressLedger.task_sha256(task),
                    "recorded_at": utc_now(),
                    "recovered": False,
                }))
                bootstrap_result = cli._start_one(wl)

    return {
        "project": project,
        "created": created,
        "brief": str(brief_path),
        "plan_version": plan["plan_version"],
        "researcher": roles["researcher"][0],
        "reviewers": roles["reviewer"],
        "bootstrap_assignment_id": bootstrap_assignment,
        "bootstrap_start": bootstrap_result,
        "proposal_handoff": str(_proposal_path(pdir)),
    }


def _active_route(nodes: Dict[str, Any], node_id: str) -> bool:
    seen = set()
    current: Optional[str] = node_id
    while current:
        if current in seen:
            return False
        seen.add(current)
        node = nodes.get(current)
        if not isinstance(node, dict) or node.get("state") != "ACTIVE":
            return False
        current = node.get("parent")
    return True


def _active_leaves(plan: Dict[str, Any]) -> set[str]:
    nodes = plan["nodes"]
    parents = {
        str(node.get("parent"))
        for node_id, node in nodes.items()
        if node.get("parent") and _active_route(nodes, node_id)
    }
    return {node_id for node_id in nodes if _active_route(nodes, node_id) and node_id not in parents}


def _normalize_proposal(graph: ResearchPlanGraph, raw: Dict[str, Any], proposal_path: Path) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("proposal root must be an object")
    allowed = {"base_plan_version", "reason", "post_hoc", "evidence_refs", "operations", "tasks"}
    extra = set(raw) - allowed
    if extra:
        raise ValueError(f"unsupported proposal fields: {sorted(extra)}")

    current = graph.snapshot()
    try:
        base = int(raw.get("base_plan_version"))
    except (TypeError, ValueError) as exc:
        raise ValueError("base_plan_version must be an integer") from exc
    if base != current["plan_version"]:
        raise ValueError(f"stale proposal base {base}; current plan is {current['plan_version']}")
    reason = str(raw.get("reason") or "").strip()
    if not reason:
        raise ValueError("proposal reason is required")
    refs_raw = raw.get("evidence_refs", [])
    if not isinstance(refs_raw, list):
        raise ValueError("evidence_refs must be a list")
    refs = list(dict.fromkeys(str(x).strip() for x in refs_raw if str(x).strip()))

    ops = graph._normalize_operations(raw.get("operations", []))
    candidate = copy.deepcopy(current)
    graph._apply_operations(candidate, ops)
    graph._validate(candidate)
    if candidate["nodes"] == current["nodes"]:
        raise ValueError("proposal has no structural effect")

    current_leaves = _active_leaves(current)
    candidate_leaves = _active_leaves(candidate)
    if candidate_leaves == {candidate["root_id"]}:
        raise ValueError("proposal leaves no executable research route")
    required = {
        node_id
        for node_id in candidate_leaves
        if node_id not in current_leaves or current["nodes"].get(node_id) != candidate["nodes"].get(node_id)
    }
    required.discard(candidate["root_id"])

    tasks_raw = raw.get("tasks", [])
    if not isinstance(tasks_raw, list):
        raise ValueError("tasks must be a list")
    tasks: List[Dict[str, str]] = []
    seen = set()
    for item in tasks_raw:
        if not isinstance(item, dict) or set(item) != {"node_id", "role", "task"}:
            raise ValueError("each task requires exactly node_id, role, task")
        node_id = str(item.get("node_id") or "").strip()
        if not node_id or Path(node_id).name != node_id or node_id in {".", ".."}:
            raise ValueError(f"invalid task node_id: {node_id!r}")
        if node_id in seen:
            raise ValueError(f"duplicate task for node {node_id}")
        role = str(item.get("role") or "").strip().lower()
        if role not in L.SCIENTIFIC_ROLES:
            raise ValueError(f"invalid task role for {node_id}: {role}")
        task = str(item.get("task") or "")
        if not task.strip():
            raise ValueError(f"empty task for node {node_id}")
        if not task.endswith("\n"):
            task += "\n"
        if role == "researcher":
            task = task.rstrip() + _researcher_footer(proposal_path) + "\n"
        tasks.append({"node_id": node_id, "role": role, "task": task})
        seen.add(node_id)
    if seen != required:
        raise ValueError(
            f"task specs must match changed executable leaves exactly; required={sorted(required)}, got={sorted(seen)}"
        )
    tasks.sort(key=lambda item: item["node_id"])
    return {
        "base_plan_version": base,
        "reason": reason,
        "post_hoc": bool(raw.get("post_hoc", False)),
        "evidence_refs": refs,
        "operations": ops,
        "tasks": tasks,
    }


def _researcher_handoff_ready(pdir: Path, researcher: str) -> bool:
    current = ProgressLedger(pdir).current(researcher, refresh_stale=True)
    return bool(
        current
        and (
            (current.get("state") == "DONE" and current.get("outcome") == "COMPLETE")
            or (current.get("state") == "BLOCKED" and current.get("outcome") == "NEEDS_REVIEW")
        )
    )


def _capture_proposal(project: str) -> Optional[Dict[str, Any]]:
    pdir = L.project_dir(project)
    researcher = _validate_roster(project)["researcher"][0]
    path = _proposal_path(pdir)
    if not path.is_file() or not _researcher_handoff_ready(pdir, researcher):
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"action": "PLAN_PROPOSAL", "result": "blocked", "reason": f"invalid_json:{exc.msg}"}

    graph = ResearchPlanGraph(pdir)
    try:
        proposal = _normalize_proposal(graph, raw, path)
        proposal_id = _content_id({"proposer": researcher, "proposal": proposal})
        record_path = _proposal_record_path(pdir, proposal_id)
        if record_path.is_file():
            path.unlink(missing_ok=True)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            return {
                "action": "PLAN_PROPOSAL",
                "proposal_id": proposal_id,
                "change_id": record.get("change_id"),
                "result": "already_captured",
            }
        change_id = graph.propose_change(
            base_plan_version=proposal["base_plan_version"],
            proposer=researcher,
            reason=f"[proposal:{proposal_id}] {proposal['reason']}",
            operations=proposal["operations"],
            post_hoc=proposal["post_hoc"],
            evidence_refs=proposal["evidence_refs"],
        )
        record = {
            "proposal_id": proposal_id,
            "change_id": change_id,
            "proposer": researcher,
            "captured_at": utc_now(),
            "proposal": proposal,
        }
        atomic_write(record_path, _json_text(record))
        path.unlink(missing_ok=True)
        return {"action": "PLAN_PROPOSAL", "proposal_id": proposal_id, "change_id": change_id, "result": "captured"}
    except (OSError, ValueError) as exc:
        return {"action": "PLAN_PROPOSAL", "result": "blocked", "reason": str(exc)}


def _proposal_id_from_change(change: Dict[str, Any]) -> Optional[str]:
    reason = str(change.get("reason") or "")
    prefix = "[proposal:"
    if not reason.startswith(prefix):
        return None
    end = reason.find("]")
    value = reason[len(prefix):end] if end > len(prefix) else ""
    return value or None


def _load_proposal_record(pdir: Path, proposal_id: str) -> Dict[str, Any]:
    path = _proposal_record_path(pdir, proposal_id)
    if not path.is_file():
        raise ValueError(f"missing immutable proposal {proposal_id}")
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("proposal_id") != proposal_id:
        raise ValueError(f"proposal identity mismatch: {proposal_id}")
    if _content_id({"proposer": record.get("proposer"), "proposal": record.get("proposal")}) != proposal_id:
        raise ValueError(f"proposal content mismatch: {proposal_id}")
    return record


def _review_task(pdir: Path, change: Dict[str, Any], proposal_id: str) -> str:
    return (
        "# Autonomous Research Plan Review\n\n"
        f"Review Plan change `{change['change_id']}` against the research brief, charter, current "
        f"Plan v{change['base_plan_version']}, and the exact immutable proposal at "
        f"`{_proposal_record_path(pdir, proposal_id)}`. Approval covers both structural operations "
        "and exact role/task specs. Reject if the route is scientifically weak, unnecessarily large, "
        "post-hoc without required evidence, or bypasses role/review/experiment boundaries.\n\n"
        f"Call `plan_review_change(change_id=\"{change['change_id']}\", approve=<true|false>, "
        "rationale=<specific rationale>)` exactly once. Use approve=false for REVISE or REJECT. "
        "Do not commit or write task files. After recording the verdict, end with COMPLETE.\n"
    )


def _revision_task(pdir: Path, change: Dict[str, Any], proposal_id: str) -> str:
    current = ResearchPlanGraph(pdir).snapshot()["plan_version"]
    return (
        "# Revise Autonomous Research Plan\n\n"
        f"Reviewer rejected proposal `{proposal_id}` / change `{change['change_id']}`. Read the "
        f"immutable proposal at `{_proposal_record_path(pdir, proposal_id)}` and address only the "
        f"blocking rationale below.\n\nReviewer rationale:\n"
        f"{str(change.get('review_rationale') or 'Reviewer rejected the proposal')}\n\n"
        "Produce the smallest corrected replacement; do not broaden the project for non-blocking suggestions.\n\n"
        + _proposal_instructions(_proposal_path(pdir), current)
        + "\nAfter writing the replacement proposal, end with COMPLETE.\n"
    )


def _repair_task(pdir: Path) -> str:
    current = ResearchPlanGraph(pdir).snapshot()["plan_version"]
    return (
        "# Repair Autonomous Plan Proposal\n\n"
        f"Read the latest deterministic validation failure at `{_repair_error_path(pdir)}` and "
        f"the rejected proposal at `{_proposal_path(pdir)}`. Replace that proposal JSON with the "
        "smallest correction needed to satisfy the validator. Preserve the scientific intent and "
        "task decomposition unless the error itself requires changing them; do not broaden the "
        "research.\n\n"
        + _proposal_instructions(_proposal_path(pdir), current)
        + "\nAfter replacing the proposal, end with COMPLETE.\n"
    )


def _task_assignments(pdir: Path, task: str) -> List[Dict[str, Any]]:
    digest = ProgressLedger.task_sha256(task)
    matches = [a for a in ProgressLedger(pdir).assignments() if a.get("task_sha256") == digest]
    matches.sort(key=lambda item: str(item.get("assigned_at") or ""))
    return matches


def _assignment_for_task(pdir: Path, task: str) -> Optional[Dict[str, Any]]:
    matches = _task_assignments(pdir, task)
    return matches[-1] if matches else None


def _assign_role(project: str, role: str, task: str, node_id: str) -> Dict[str, Any]:
    pdir = L.project_dir(project)
    matches = _task_assignments(pdir, task)
    existing = matches[-1] if matches else None
    is_repair = task.startswith("# Repair Autonomous Plan Proposal\n")
    retry = bool(
        is_repair
        and existing
        and existing.get("state") in {"DONE", "BLOCKED"}
        and len(matches) < _MAX_REPAIR_ATTEMPTS
    )
    if existing and not retry:
        result = (
            "repair_exhausted"
            if is_repair
            and existing.get("state") in {"DONE", "BLOCKED"}
            and len(matches) >= _MAX_REPAIR_ATTEMPTS
            else "existing"
        )
        return {
            "action": "PLAN_ASSIGN",
            "role": role,
            "worker": existing.get("worker"),
            "assignment_id": existing.get("assignment_id"),
            "result": result,
            "state": existing.get("state"),
            "outcome": existing.get("outcome"),
        }
    wl = A._idle_worker(project, role, cli._project_rows(project), set(), cli._read_worker_role)
    if wl is None:
        return {"action": "PLAN_ASSIGN", "role": role, "result": "no_idle_worker"}
    assignment = cli.do_assign(f"{project}/{wl.name}", task, node_id=node_id)
    out = {
        "action": "PLAN_ASSIGN",
        "role": role,
        "worker": wl.name,
        "assignment_id": assignment.get("assignment_id"),
        "result": "assigned",
        "start_result": cli._start_one(wl),
    }
    if is_repair:
        out["attempt"] = len(matches) + 1
    return out


def _review_assignment(pdir: Path, change: Dict[str, Any], proposal_id: str) -> Optional[Dict[str, Any]]:
    return _assignment_for_task(pdir, _review_task(pdir, change, proposal_id))


def _materialize_tasks(pdir: Path, record: Dict[str, Any], version: int) -> List[str]:
    written: List[str] = []
    for spec in record["proposal"]["tasks"]:
        path = pdir / "tasks" / f"v{version}" / spec["role"] / f"{spec['node_id']}.md"
        text = spec["task"]
        if path.is_file():
            if path.read_text(encoding="utf-8") != text:
                raise ValueError(f"task spec conflict: {path}")
            continue
        atomic_write(path, text)
        written.append(str(path))
    return written


def _recover_materialized_tasks(pdir: Path) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    graph = ResearchPlanGraph(pdir)
    directory = _proposal_dir(pdir)
    if not directory.is_dir():
        return actions
    for path in sorted(directory.glob("*.json")):
        try:
            record = _load_proposal_record(pdir, path.stem)
            change = graph.get_change(record["change_id"])
            if change.get("status") != "COMMITTED" or not change.get("committed_version"):
                continue
            written = _materialize_tasks(pdir, record, int(change["committed_version"]))
            if written:
                actions.append({
                    "action": "PLAN_TASK_RECOVERY",
                    "proposal_id": path.stem,
                    "plan_version": int(change["committed_version"]),
                    "task_count": len(written),
                    "result": "recovered",
                })
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            actions.append({"action": "PLAN_TASK_RECOVERY", "result": "blocked", "reason": str(exc)})
    return actions


def _planning_step_unlocked(project: str) -> List[Dict[str, Any]]:
    pdir = L.project_dir(project)
    if (pdir / L.PAUSE_FILE).is_file():
        return []
    actions = _recover_materialized_tasks(pdir)
    captured = _capture_proposal(project)
    if captured:
        actions.append(captured)
        if captured.get("result") == "blocked":
            reason = str(captured.get("reason") or "proposal validation failed")
            atomic_write(_repair_error_path(pdir), _json_text({
                "plan_version": ResearchPlanGraph(pdir).snapshot()["plan_version"],
                "reason": reason,
                "recorded_at": utc_now(),
            }))
            item = _assign_role(
                project,
                "researcher",
                _repair_task(pdir),
                ResearchPlanGraph(pdir).snapshot()["root_id"],
            )
            item["action"] = "PLAN_REPAIR"
            item["reason"] = reason
            actions.append(item)
            return actions
        _repair_error_path(pdir).unlink(missing_ok=True)

    graph = ResearchPlanGraph(pdir)
    linked = [item for item in graph.pending_changes() if _proposal_id_from_change(item)]
    if linked:
        change = linked[0]
        proposal_id = _proposal_id_from_change(change)
        assert proposal_id is not None
        try:
            record = _load_proposal_record(pdir, proposal_id)
            if record.get("change_id") != change.get("change_id"):
                raise ValueError("proposal/change binding mismatch")
            if record["proposal"]["operations"] != change.get("operations"):
                raise ValueError("approved change operations diverge from immutable proposal")
            review = _review_assignment(pdir, change, proposal_id)
            if change["status"] == "PROPOSED":
                if review is None:
                    item = _assign_role(project, "reviewer", _review_task(pdir, change, proposal_id), graph.snapshot()["root_id"])
                    item.update({"change_id": change["change_id"], "proposal_id": proposal_id})
                    actions.append(item)
                elif review.get("state") in {"DONE", "BLOCKED"}:
                    actions.append({
                        "action": "PLAN_REVIEW",
                        "change_id": change["change_id"],
                        "proposal_id": proposal_id,
                        "result": "verdict_not_recorded",
                    })
            elif change["status"] == "APPROVED":
                if review and review.get("state") == "DONE" and review.get("outcome") == "COMPLETE":
                    version = graph.commit_change(change["change_id"], committer="supervisor")
                    written = _materialize_tasks(pdir, record, version)
                    actions.append({
                        "action": "PLAN_COMMIT",
                        "change_id": change["change_id"],
                        "proposal_id": proposal_id,
                        "plan_version": version,
                        "task_count": len(written),
                        "result": "committed",
                    })
                else:
                    actions.append({
                        "action": "PLAN_COMMIT",
                        "change_id": change["change_id"],
                        "proposal_id": proposal_id,
                        "result": "awaiting_reviewer_completion",
                    })
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            actions.append({"action": "PLAN_GOVERNANCE", "result": "blocked", "reason": str(exc)})
        return actions

    directory = _proposal_dir(pdir)
    if directory.is_dir():
        current_version = graph.snapshot()["plan_version"]
        rejected: List[tuple[str, Dict[str, Any]]] = []
        for path in sorted(directory.glob("*.json")):
            try:
                record = _load_proposal_record(pdir, path.stem)
                change = graph.get_change(record["change_id"])
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if change.get("status") == "REJECTED" and change.get("base_plan_version") == current_version:
                rejected.append((path.stem, change))
        if rejected:
            proposal_id, change = rejected[-1]
            review = _review_assignment(pdir, change, proposal_id)
            if review and review.get("state") == "DONE" and review.get("outcome") == "COMPLETE":
                item = _assign_role(project, "researcher", _revision_task(pdir, change, proposal_id), graph.snapshot()["root_id"])
                item.update({"change_id": change["change_id"], "proposal_id": proposal_id})
                actions.append(item)
    return actions


def planning_step(project: str) -> List[Dict[str, Any]]:
    """Run one serialized proposal/review/commit/materialization pass."""
    pdir = L.project_dir(project)
    with cli.project_control_lock(pdir):
        return _planning_step_unlocked(project)


def watch_research(project: str, *, heartbeat: float = 1800.0, poll: float = 1.0, token_budget: int = 0) -> Iterator[Dict[str, Any]]:
    pending = planning_step(project)
    for report in cli.watch_supervisor(project, heartbeat=heartbeat, poll=poll, token_budget=token_budget):
        actions = pending + planning_step(project)
        pending = []
        if actions:
            report["planning_applied"] = actions
        yield report


def _brief_from_args(args: argparse.Namespace) -> Optional[str]:
    if args.brief and args.stdin:
        raise SystemExit("choose only one of --brief or --stdin")
    if args.brief:
        return Path(args.brief).read_text(encoding="utf-8")
    if args.stdin:
        return sys.stdin.read()
    return None


def main(argv: Optional[List[str]] = None) -> int:
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

    init = initialize_research(args.project, brief=_brief_from_args(args), model=args.model)
    print(json.dumps({"research_init": init}, ensure_ascii=False, sort_keys=True), flush=True)
    if args.once:
        planning = planning_step(args.project)
        report = cli.do_supervise(args.project, apply=True, token_budget=args.token_budget, trigger="research")
        if planning:
            report["planning_applied"] = planning
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
        return 0

    for report in watch_research(args.project, heartbeat=args.heartbeat, poll=args.poll, token_budget=args.token_budget):
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return 0