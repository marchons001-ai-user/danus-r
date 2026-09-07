#!/usr/bin/env python3
"""Danus gateway — the role-gated MCP server.

A thin MCP wrapper over ``danus.core`` (the truth/evidence stores) + one external
integration (``danus.integrations`` arXiv search). It exposes only the verbs an
LLM can't do reliably itself (content-addressed writes, cascade integrity, the
verifier-gated fact write, BM25) — reads / local memory / novelty judgment are the
agent's own file operations, deliberately NOT tools.

The permission model (which tools each role sees) lives in ``roles.py``. The
``fact_submit`` tool is the ONLY fact-write path. It routes analytical claims to
the historical proof verifier and empirical claims to the empirical verification
route, writes the node IFF the verdict is ``correct``, and ALWAYS traces any
verdict to global memory (kind ``verification``) — accept, reject, or
accept-but-write-failed — so a stateless verifier outcome is never lost.

Config is read from the environment at CALL time (not import time) so the server
is testable and reconfigurable:
  DANUS_PROJECT_DIR   the project dir a worker is pinned to (fallback for main)
  DANUS_AGENTS_ROOT   root holding all projects (<root>/<project>); lets main
                      address any project by name via the ``project`` arg
  DANUS_AUTHOR        this agent's id, for attribution
  DANUS_ASSIGNMENT_ID the runtime-bound assignment allowed to submit Reviewer verdicts
  DANUS_ROLE          worker | main | verifier | all  (selects exposed tools;
                      unset falls back to the read-only verifier set — fail-closed)
  DANUS_VERIFY_URL    analytical verify endpoint, normally .../verify; the
                      empirical sibling route is derived as .../verify/empirical
  DANUS_PROBLEM_ID    problem id stamped on written facts (default: project name)
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from danus._mcp import FastMCP
from danus.core import (
    FACT_TYPES,
    EvidenceStore,
    FactGraph,
    GlobalMemory,
    ProgressLedger,
    ResearchPlanGraph,
)
from danus.core.handoff import HandoffRequests
from danus.integrations import search as _arxiv_search

from .roles import tools_for

_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# --------------------------------------------------------------------------- #
# config resolution (env read at call time — testable / reconfigurable)       #
# --------------------------------------------------------------------------- #

def _author() -> str:
    return os.environ.get("DANUS_AUTHOR", "unknown")


def _role() -> str:
    # Fail-closed: an UNSET role gets the most-restrictive read-only set, same as
    # a mis-typed one (roles.tools_for). Dev use of the full set is explicit:
    # DANUS_ROLE=all.
    return os.environ.get("DANUS_ROLE", "verifier")


def _project(project: Optional[str] = None) -> Path:
    """Resolve the project dir to operate on.

    ``project`` (the main agent's per-call selector) wins: it names a project
    under ``DANUS_AGENTS_ROOT`` (``<root>/<project>``), so one session can touch
    several projects. With no ``project`` we fall back to ``DANUS_PROJECT_DIR``
    (a worker is always pinned this way). The name is validated to a single path
    segment — no ``/`` or ``..`` — so it can never escape the agents root."""
    agents_root = os.environ.get("DANUS_AGENTS_ROOT", "")
    project_dir = os.environ.get("DANUS_PROJECT_DIR", "")
    if project:
        if not agents_root:
            raise RuntimeError("DANUS_AGENTS_ROOT is not set; cannot resolve a project by name")
        if not _PROJECT_NAME_RE.match(project):
            raise RuntimeError(f"invalid project name: {project!r}")
        pdir = Path(agents_root) / project
        if not pdir.is_dir():
            raise RuntimeError(f"no such project: {project!r} (under {agents_root})")
        return pdir
    if not project_dir:
        raise RuntimeError("DANUS_PROJECT_DIR is not set and no project was given")
    return Path(project_dir)


def _gm(project: Optional[str] = None) -> GlobalMemory:
    return GlobalMemory(_project(project))


def _fg(project: Optional[str] = None) -> FactGraph:
    return FactGraph(_project(project))


def _pg(project: Optional[str] = None) -> ResearchPlanGraph:
    return ResearchPlanGraph(_project(project))


def _pl(project: Optional[str] = None) -> ProgressLedger:
    return ProgressLedger(_project(project))


def _verify_endpoint(*, empirical: bool = False) -> str:
    """Resolve the analytical or empirical sibling endpoint from one env value."""
    verify_url = os.environ.get("DANUS_VERIFY_URL", "").rstrip("/")
    if not verify_url:
        raise RuntimeError("DANUS_VERIFY_URL is not set (verify service not wired yet)")
    if not empirical:
        return verify_url
    if verify_url.endswith("/verify"):
        return verify_url + "/empirical"
    return verify_url + "/verify/empirical"


def _post_verify(url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        timeout = int(os.environ.get("DANUS_VERIFY_TIMEOUT", "3600"))
    except ValueError:
        timeout = 3600
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted local URL)
        return json.loads(resp.read().decode("utf-8"))


def _verify(statement: str, proof: str) -> Dict[str, Any]:
    """Historical analytical verification HTTP call."""
    return _post_verify(_verify_endpoint(), {"statement": statement, "proof": proof})


def _verify_empirical(
    *,
    statement: str,
    scope: Dict[str, Any],
    evidence_refs: List[str],
    evidence_summary: str,
) -> Dict[str, Any]:
    """Empirical verification HTTP call; deterministic checks run in the service."""
    return _post_verify(
        _verify_endpoint(empirical=True),
        {
            "statement": statement,
            "project_dir": str(_project().resolve()),
            "scope": scope,
            "evidence_refs": evidence_refs,
            "evidence_summary": evidence_summary,
        },
    )


# --------------------------------------------------------------------------- #
# global memory                                                               #
# --------------------------------------------------------------------------- #

def gm_add(
    kind: str,
    claim: str,
    evidence: str = "",
    verifiable: Optional[bool] = None,
    glossary: Optional[Dict[str, str]] = None,
    links: Optional[Dict[str, Any]] = None,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """Publish a finding to shared global memory (claim + evidence). Verifiable
    kinds (conclusion/example/counterexample/proof_attempt) require explicit
    evidence; judgments (plan/direction/obstacle/master_guidance/elaboration) do
    not. Define your symbols in ``glossary`` and reuse project terminology.

    Main agent: pass ``project`` to target one of several projects by name;
    workers omit it (pinned to their own project)."""
    entry_id = _gm(project).append(
        kind, claim=claim, evidence=evidence, author=_author(),
        verifiable=verifiable, glossary=glossary, links=links,
    )
    return {"id": entry_id, "kind": kind}


def gm_search(query: str, kinds: Optional[List[str]] = None, limit_per_kind: int = 10,
              project: Optional[str] = None) -> Dict[str, Any]:
    """BM25 over shared global-memory findings. Use to reuse others' results,
    avoid duplicate work, and learn which paths already died. Main agent: pass
    ``project`` to search a specific project; workers omit it (pinned to their own project)."""
    return _gm(project).search(query, kinds=kinds, limit_per_kind=limit_per_kind)


# --------------------------------------------------------------------------- #
# fact graph                                                                  #
# --------------------------------------------------------------------------- #

def fact_submit(
    statement: str,
    proof: str = "",
    predecessors: Optional[List[str]] = None,
    glossary_introduces: Optional[Dict[str, str]] = None,
    intuition: str = "",
    source_id: Optional[str] = None,
    external_refs: Optional[List[Dict[str, Any]]] = None,
    fact_type: str = "analytical",
    scope: Optional[Dict[str, Any]] = None,
    evidence_refs: Optional[List[str]] = None,
    evidence_summary: str = "",
) -> Dict[str, Any]:
    """The only way to write a verified scientific fact.

    ``fact_type="analytical"`` preserves the historical ``statement + proof``
    path. ``fact_type="empirical"`` requires explicit ``scope`` and immutable
    ``evidence_refs`` and routes to ``/verify/empirical``; do not fabricate a
    prose proof for numerical evidence.

    Once a verdict exists, the outcome is **always** recorded to global memory
    (kind ``verification``). If ``source_id`` names the shared finding being
    promoted, a successful fact write also marks that finding ``verified`` and
    back-links the returned ``fact_id``. A rejection does not mark the source
    ``refuted``: failed support/verification is not proof that the claim is false.

    ``external_refs`` remains mutable bibliographic metadata and does not affect
    either analytical or empirical fact identity."""
    fg = _fg()
    gm = _gm()
    problem_id = os.environ.get("DANUS_PROBLEM_ID", Path(_project()).name)
    kind = (fact_type or "analytical").strip().lower()
    preds = predecessors or []

    # Cheap input checks happen before HTTP/LLM work.
    if kind not in FACT_TYPES:
        return {"accepted": False, "verdict": "error",
                "error": f"unknown fact_type {fact_type!r}; expected one of {FACT_TYPES}"}
    if not statement.strip():
        return {"accepted": False, "verdict": "error", "error": "statement is required"}
    if kind == "analytical" and not proof.strip():
        return {"accepted": False, "verdict": "error", "error": "analytical fact requires proof"}

    empirical_scope = scope or {}
    empirical_refs = list(dict.fromkeys(
        str(x).strip() for x in (evidence_refs or []) if str(x).strip()
    ))
    if kind == "empirical":
        if not empirical_scope:
            return {"accepted": False, "verdict": "error",
                    "error": "empirical fact requires non-empty scope"}
        if not empirical_refs:
            return {"accepted": False, "verdict": "error",
                    "error": "empirical fact requires evidence_refs"}
        if glossary_introduces:
            return {"accepted": False, "verdict": "error",
                    "error": "glossary_introduces is currently analytical-only"}

    # Mathematical symbol coverage remains advisory and analytical-only.
    undefined: List[str] = []
    if kind == "analytical":
        try:
            undefined = fg.undefined_symbols(
                statement=statement, proof=proof, intuition=intuition,
                predecessors=predecessors, glossary_introduces=glossary_introduces,
            )
        except Exception:
            undefined = []

    # 1) Verify. If the service errors, no verdict exists yet: return a clean
    # error so the worker can retry. Nothing is promoted or traced as a verdict.
    try:
        if kind == "analytical":
            result = _verify(statement, proof)
        else:
            result = _verify_empirical(
                statement=statement,
                scope=empirical_scope,
                evidence_refs=empirical_refs,
                evidence_summary=evidence_summary,
            )
    except Exception as e:
        out: Dict[str, Any] = {"accepted": False, "verdict": "error", "error": str(e)}
        if kind == "analytical":
            out["undefined_symbols"] = undefined
        return out

    if not isinstance(result, dict):
        out = {
            "accepted": False,
            "verdict": "error",
            "error": f"verify service returned a non-dict body ({type(result).__name__})",
        }
        if kind == "analytical":
            out["undefined_symbols"] = undefined
        return out

    verdict = result.get("verdict")
    accepted = verdict == "correct"

    # 2) Write the verified fact. The two types share the same DAG but retain
    # type-specific identity/body semantics.
    fact_id = None
    write_error = None
    if accepted:
        try:
            if kind == "analytical":
                fact_id = fg.add(
                    problem_id=problem_id, author=_author(), statement=statement, proof=proof,
                    predecessors=predecessors, glossary_introduces=glossary_introduces,
                    intuition=intuition, external_refs=external_refs,
                )
            else:
                fact_id = fg.add_empirical(
                    problem_id=problem_id, author=_author(), statement=statement,
                    scope=empirical_scope, evidence_refs=empirical_refs,
                    evidence_summary=evidence_summary, predecessors=predecessors,
                    intuition=intuition, external_refs=external_refs,
                )
        except Exception as e:
            write_error = str(e)

    # 3) Close the source finding only after the fact really exists. Keep this
    # separate from the verifier verdict: accepted-but-write-failed is not promoted.
    promotion_error = None
    if fact_id and source_id:
        try:
            gm.set_status(source_id, "verified", fact_id=fact_id)
        except Exception as e:
            promotion_error = str(e)

    # 4) ALWAYS trace once a verifier verdict exists, including write/promotion
    # failures. Empirical deterministic checks are already embedded in the report.
    report = result.get("verification_report")
    gm.append(
        "verification",
        claim=statement,
        evidence="verdict: correct" if accepted else (result.get("repair_hints") or "verdict: wrong"),
        author=_author(),
        verifiable=False,
        links={
            "source_id": source_id,
            "predecessors": preds,
            "evidence_refs": empirical_refs if kind == "empirical" else [],
        },
        verification_type=kind,
        scope=empirical_scope if kind == "empirical" else {},
        verdict=verdict,
        fact_id=fact_id,
        write_error=write_error,
        promotion_error=promotion_error,
        verification_report=report,
    )

    # 5) Return.
    if not accepted:
        out = {
            "accepted": False,
            "verdict": verdict,
            "repair_hints": result.get("repair_hints"),
            "verification_report": report,
        }
        if kind == "analytical":
            out["undefined_symbols"] = undefined
        return out
    if write_error:
        out = {"accepted": True, "fact_id": None, "write_error": write_error}
        if kind == "analytical":
            out["undefined_symbols"] = undefined
        return out
    out = {"accepted": True, "fact_id": fact_id}
    if promotion_error:
        out["promotion_error"] = promotion_error
    if kind == "analytical":
        out["undefined_symbols"] = undefined
    return out


def fact_search(query: str, limit: int = 10, project: Optional[str] = None) -> Dict[str, Any]:
    """BM25 search over the unified verified scientific fact graph. The derived
    search index is rebuilt on demand from fact files; the graph stays the single
    source of truth. Results include fact type, scope, and empirical evidence ids
    so a searched claim can be followed to its immutable Evidence Record/artifacts."""
    return {"query": query, "results": _fg(project).search(query, limit=limit)}


def fact_revoke(
    fact_id: str,
    reason: str,
    project: Optional[str] = None,
    replacement_fact_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Cascade-revoke a wrong/superseded fact and everything that depends on it.
    Operator / main-agent only. ``replacement_fact_id`` must already name an active
    verified fact and cannot depend on the retiring fact. Historical files remain
    archived under ``_revoked``. Main agent: pass ``project`` to target the project."""
    revoked = _fg(project).revoke(
        fact_id,
        reason=reason,
        replacement_fact_id=replacement_fact_id,
    )
    out: Dict[str, Any] = {"revoked": revoked}
    if replacement_fact_id:
        out["replacement_fact_id"] = replacement_fact_id
    return out


# --------------------------------------------------------------------------- #
# durable upward handoff                                                      #
# --------------------------------------------------------------------------- #

@contextmanager
def _worker_write_scope(*, researcher_only: bool = False):
    from danus.orchestration.cli import project_control_lock
    from danus.execution import layout as L

    ledger = _pl()
    with project_control_lock(_project()), ledger.assignment_lock():
        assignment = ledger.current(_author(), refresh_stale=True)
        expected = os.environ.get("DANUS_ASSIGNMENT_ID", "")
        role = str((assignment or {}).get("role") or "").lower()
        if (
            not assignment or not expected
            or assignment["assignment_id"] != expected
            or assignment["state"] not in {"ASSIGNED", "RUNNING"}
            or role not in ({"researcher"} if researcher_only else {"researcher", "reviewer", "experimenter"})
            or _role().lower() not in {role, "all"}
            or ledger._is_plan_stale(assignment)
        ):
            raise ValueError("write requires the gateway's current active assignment")
        if ((_project() / L.PAUSE_FILE).exists()
            or (_project() / "workers" / _author() / L.PAUSE_FILE).exists()):
            raise ValueError("assignment is paused")
        if assignment.get("handoff_id"):
            handoff = HandoffRequests(ledger).get(assignment["handoff_id"])
            if (not handoff or handoff["status"] != "ASSIGNED"
                or handoff.get("assigned_assignment_id") != expected):
                raise ValueError("assignment handoff is no longer authoritative")
        yield assignment


@_worker_write_scope()
def handoff_request(
    kind: str,
    reason: str,
    evidence_refs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Open the caller's one durable upward handoff for this assignment.

    Experimenters may request INTERPRET or ESCALATE. Researchers and Reviewers may
    request ESCALATE. ``evidence_refs`` is optional and valid only for INTERPRET;
    when an Experiment Runtime already produced Evidence Records, pass their ids so
    the Researcher receives exact immutable evidence instead of searching broadly.
    Routing identity is derived from the authenticated role and current Assignment;
    callers cannot choose another worker, node, target, or subject. Repeating the
    same kind from the same Assignment returns the existing handoff only when its
    Evidence refs match exactly.
    """
    requested_kind = str(kind or "").strip().upper()
    gateway_role = str(_role() or "").strip().lower()
    allowed = {
        "experimenter": {"INTERPRET", "ESCALATE"},
        "researcher": {"ESCALATE"},
        "reviewer": {"ESCALATE"},
    }
    ledger = _pl()
    author = _author()
    source = ledger.current(author, refresh_stale=True)
    if source is None:
        raise ValueError(f"worker {author} has no current assignment")
    source_role = str(source.get("role") or "").strip().lower()
    role = source_role if gateway_role == "all" else gateway_role
    if role not in allowed or requested_kind not in allowed[role]:
        raise ValueError(f"role {role or 'unknown'} cannot request {requested_kind or 'empty'} handoff")
    if source_role != role:
        raise ValueError(f"gateway role {role} does not match current assignment role {source_role}")
    if source.get("state") not in {"ASSIGNED", "RUNNING"}:
        raise ValueError(
            f"handoff request requires active assignment; current state is {source.get('state')}"
        )
    reason_text = str(reason or "").strip()
    if not reason_text:
        raise ValueError("reason is required")

    refs = list(dict.fromkeys(
        str(value).strip() for value in (evidence_refs or []) if str(value).strip()
    ))
    if refs and requested_kind != "INTERPRET":
        raise ValueError("evidence_refs are supported only for INTERPRET handoffs")
    if refs:
        store = EvidenceStore(_project())
        missing = [evidence_id for evidence_id in refs if store.get_record(evidence_id) is None]
        if missing:
            raise ValueError(f"unknown evidence_refs: {missing}")

    handoffs = HandoffRequests(ledger)
    source_id = str(source["assignment_id"])
    existing = next(
        (
            item
            for item in handoffs.all()
            if item.get("kind") == requested_kind
            and item.get("subject_type") == "ASSIGNMENT"
            and item.get("subject_id") == source_id
            and item.get("source_assignment_id") == source_id
        ),
        None,
    )
    if existing is not None:
        existing_refs = list(existing.get("evidence_refs") or [])
        if existing_refs != refs:
            raise ValueError(
                "existing handoff for this assignment has different evidence_refs"
            )
        return {
            "handoff_id": existing["handoff_id"],
            "kind": existing["kind"],
            "status": existing["status"],
            "target_role": existing["target_role"],
            "evidence_refs": existing_refs,
        }

    if requested_kind == "INTERPRET":
        evidence_block = ""
        if refs:
            evidence_block = (
                "Evidence Records (immutable):\n"
                + "".join(f"- `{evidence_id}`\n" for evidence_id in refs)
                + "\n"
            )
        request = (
            "# Interpret Experiment Result\n\n"
            f"Source assignment `{source_id}` on Plan node `{source['node_id']}` requested Researcher interpretation.\n\n"
            f"Reason:\n{reason_text}\n\n"
            f"{evidence_block}"
            "Inspect the source task/workspace and the listed persisted evidence when present. Determine the bounded "
            "scientific meaning without re-running or broadening the experiment. Use only existing Researcher-authorized "
            "knowledge, Plan, or Implementation Contract paths if a concrete follow-up is justified. Do not alter "
            "the source Assignment or Handoff state. End with COMPLETE when this interpretation request itself is handled.\n"
        )
    else:
        request = (
            "# Escalation Request\n\n"
            f"Source assignment `{source_id}` from role `{source_role}` on Plan node `{source['node_id']}` requires "
            "Supervisor/control-plane attention.\n\n"
            f"Reason:\n{reason_text}\n\n"
            "This request records an unresolved control/scientific routing issue. Its existence does not authorize "
            "an automatic Plan, Contract, or source-Assignment disposition.\n"
        )

    plan = _pg().snapshot()
    node_id = str(source["node_id"])
    node = plan["nodes"].get(node_id)
    if node is None or node.get("state") != "ACTIVE":
        raise ValueError(f"source plan node {node_id} is not ACTIVE")
    opened = handoffs.open(
        kind=requested_kind,
        plan_version=int(plan["plan_version"]),
        node_id=node_id,
        subject_type="ASSIGNMENT",
        subject_id=source_id,
        requested_by=author,
        source_role=source_role,
        source_assignment_id=source_id,
        request=request,
        reason=reason_text,
        evidence_refs=refs,
    )
    return {
        "handoff_id": opened["handoff_id"],
        "kind": opened["kind"],
        "status": opened["status"],
        "target_role": opened["target_role"],
        "evidence_refs": list(opened.get("evidence_refs") or []),
    }


# --------------------------------------------------------------------------- #
# pre-code semantic alignment gate                                            #
# --------------------------------------------------------------------------- #

def implementation_pending_contracts(project: Optional[str] = None) -> Dict[str, Any]:
    """Return the latest unconsumed implementation contract for each gated node."""
    contracts = _pl(project).pending_implementation_contracts()
    return {"contracts": contracts, "count": len(contracts)}


@_worker_write_scope(researcher_only=True)
def implementation_submit_contract(
    node_id: str,
    plan_version: int,
    goal: str,
    must_preserve_invariants: List[str],
    forbidden_behaviors: List[str],
    acceptance_conditions: List[str],
) -> Dict[str, Any]:
    """Researcher-only path for a material implementation change.

    Use this for changes whose semantic direction matters before code exists, such
    as backend/routing policy, role authority, frozen experiment protocol, or core
    architecture. The returned ``task`` is the exact task the Supervisor must bind
    after Reviewer APPROVE; changing it invalidates the approval.
    """
    if _pl().current(_author())["node_id"] != str(node_id).strip():
        raise ValueError("contract node does not match current assignment")
    record = _pl().submit_implementation_contract(
        proposer=_author(),
        node_id=node_id,
        plan_version=plan_version,
        goal=goal,
        must_preserve_invariants=must_preserve_invariants,
        forbidden_behaviors=forbidden_behaviors,
        acceptance_conditions=acceptance_conditions,
    )
    return {
        "contract_id": record["contract_id"],
        "status": record["status"],
        "task": record["task"],
    }


@contextmanager
def _review_scope(subject_type: str, subject_id: str):
    from danus.orchestration.cli import project_control_lock

    ledger = _pl()
    with project_control_lock(_project()), ledger.assignment_lock():
        assignment = ledger.current(_author(), refresh_stale=True)
        expected = os.environ.get("DANUS_ASSIGNMENT_ID", "")
        if (
            _role().lower() not in {"reviewer", "all"}
            or not assignment
            or not expected
            or assignment.get("assignment_id") != expected
            or assignment.get("role", "").lower() != "reviewer"
            or assignment.get("state") not in {"ASSIGNED", "RUNNING", "DONE"}
            or ledger._is_plan_stale(assignment)
        ):
            raise ValueError("review requires the gateway's current Reviewer assignment")
        handoff = HandoffRequests(ledger).get(str(assignment.get("handoff_id") or ""))
        if (
            not handoff
            or handoff.get("kind") != "REVIEW"
            or handoff.get("subject_type") != subject_type
            or handoff.get("subject_id") != subject_id
            or handoff.get("node_id") != assignment.get("node_id")
            or handoff.get("assigned_assignment_id") != expected
            or handoff.get("status") not in {"ASSIGNED", "RESOLVED"}
        ):
            raise ValueError("review subject does not match the assigned REVIEW handoff")
        yield assignment, handoff


def implementation_review_contract(
    contract_id: str,
    verdict: str,
    rationale: str,
) -> Dict[str, Any]:
    """Reviewer-only semantic verdict; exact retries return the durable verdict."""
    with _review_scope("IMPLEMENTATION_CONTRACT", contract_id) as (review_assignment, review_handoff):
        ledger = _pl()
        reviewer = _author()
        normalized = str(verdict or "").strip().upper()
        if normalized not in ProgressLedger.IMPLEMENTATION_VERDICTS:
            raise ValueError(f"verdict must be one of {ProgressLedger.IMPLEMENTATION_VERDICTS}")
        rationale_text = str(rationale or "").strip()
        if not rationale_text:
            raise ValueError("rationale is required")
        current = ledger.implementation_contract(contract_id)
        if current is not None:
            existing_verdict = str(current.get("verdict") or "").upper()
            if existing_verdict in ProgressLedger.IMPLEMENTATION_VERDICTS:
                if (
                    existing_verdict == normalized
                    and current.get("review_assignment_id") == review_assignment["assignment_id"]
                    and str(current.get("reviewer") or "") == reviewer
                    and str(current.get("rationale") or "") == rationale_text
                ):
                    return {"contract_id": contract_id, "status": current["status"]}
                raise ValueError(f"conflicting replay for reviewed implementation contract {contract_id}")
        if review_assignment.get("state") not in {"ASSIGNED", "RUNNING"} or review_handoff.get("status") != "ASSIGNED":
            raise ValueError("new verdict requires an active review assignment")
        record = ledger.review_implementation_contract(
            contract_id,
            reviewer=reviewer,
            verdict=normalized,
            rationale=rationale_text,
            review_assignment_id=review_assignment["assignment_id"],
        )
        return {"contract_id": contract_id, "status": record["status"]}


# --------------------------------------------------------------------------- #
# research-plan change gate                                                   #
# --------------------------------------------------------------------------- #

def plan_pending_changes(project: Optional[str] = None) -> Dict[str, Any]:
    """Return current-base proposed/approved revisions; stale requests are retired."""
    changes = _pg(project).pending_changes()
    return {"changes": changes, "count": len(changes)}


@_worker_write_scope(researcher_only=True)
def plan_propose_change(
    base_plan_version: int,
    reason: str,
    operations: List[Dict[str, Any]],
    post_hoc: bool = False,
    evidence_refs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Researcher-only path: propose a structural change without mutating Plan."""
    change_id = _pg().propose_change(
        base_plan_version=base_plan_version,
        proposer=_author(),
        reason=reason,
        operations=operations,
        post_hoc=post_hoc,
        evidence_refs=evidence_refs,
    )
    return {"change_id": change_id, "status": "PROPOSED"}


def plan_review_change(
    change_id: str,
    rationale: str,
    verdict: str = "",
    approve: Optional[bool] = None,
) -> Dict[str, Any]:
    """Reviewer-only verdict; exact retries return the already durable review."""
    with _review_scope("PLAN_CHANGE", change_id) as (review_assignment, review_handoff):
        graph = _pg()
        reviewer = _author()
        if verdict and str(verdict).strip():
            normalized = str(verdict).strip().upper()
            if normalized not in ResearchPlanGraph.REVIEW_VERDICTS:
                raise ValueError(f"verdict must be one of {ResearchPlanGraph.REVIEW_VERDICTS}")
            if approve is not None and bool(approve) != (normalized == "APPROVE"):
                raise ValueError("approve compatibility flag conflicts with explicit verdict")
        else:
            if approve is None:
                raise ValueError("plan review requires verdict APPROVE, REVISE, or REJECT")
            normalized = "APPROVE" if bool(approve) else "REVISE"
        rationale_text = str(rationale or "").strip()
        if not rationale_text:
            raise ValueError("rationale is required")
        current = graph.get_change(change_id)
        existing_verdict = str(current.get("review_verdict") or "").upper()
        if existing_verdict in ResearchPlanGraph.REVIEW_VERDICTS:
            if (
                existing_verdict == normalized
                and current.get("review_assignment_id") == review_assignment["assignment_id"]
                and str(current.get("reviewer") or "") == reviewer
                and str(current.get("review_rationale") or "") == rationale_text
            ):
                return {
                    "change_id": change_id,
                    "status": current["status"],
                    "verdict": existing_verdict,
                }
            raise ValueError(f"conflicting replay for reviewed plan change {change_id}")
        if review_assignment.get("state") not in {"ASSIGNED", "RUNNING"} or review_handoff.get("status") != "ASSIGNED":
            raise ValueError("new verdict requires an active review assignment")
        record = graph.review_change(
            change_id,
            reviewer=reviewer,
            rationale=rationale_text,
            verdict=normalized,
            approve=None,
            review_assignment_id=review_assignment["assignment_id"],
        )
        return {
            "change_id": change_id,
            "status": record["status"],
            "verdict": record.get("review_verdict"),
        }


def plan_commit_change(change_id: str, project: Optional[str] = None) -> Dict[str, Any]:
    """Supervisor/control-plane-only path: commit one approved plan change."""
    version = _pg(project).commit_change(change_id, committer=_author())
    return {"change_id": change_id, "status": "COMMITTED", "plan_version": version}


# --------------------------------------------------------------------------- #
# arXiv theorem search (external integration)                                 #
# --------------------------------------------------------------------------- #

def search_arxiv_theorems(query: str, num_results: int = 10) -> Dict[str, Any]:
    """Semantic search over arXiv theorem statements (Matlas). Returns
    **verbatim, as-published** theorem / lemma / definition statements — statement
    fidelity matters for math reasoning and citation checking. Phrase the query as
    a *complete mathematical statement* when possible. Returns ranked results,
    each with ``title``, the full ``theorem`` text, ``arxiv_id``, and the in-paper
    ``theorem_id``. External HTTP, no auth; on outage returns an ``error`` and
    empty ``results`` (retry / fall back to built-in web search)."""
    return _arxiv_search(query, num_results=num_results)


# --------------------------------------------------------------------------- #
# role-based registration                                                     #
# --------------------------------------------------------------------------- #

_TOOLS = {
    "gm_add": gm_add,
    "gm_search": gm_search,
    "fact_submit": fact_submit,
    "fact_search": fact_search,
    "fact_revoke": fact_revoke,
    "handoff_request": handoff_request,
    "implementation_pending_contracts": implementation_pending_contracts,
    "implementation_submit_contract": implementation_submit_contract,
    "implementation_review_contract": implementation_review_contract,
    "plan_pending_changes": plan_pending_changes,
    "plan_propose_change": plan_propose_change,
    "plan_review_change": plan_review_change,
    "plan_commit_change": plan_commit_change,
    "search_arxiv_theorems": search_arxiv_theorems,
}


def build_app(role: Optional[str] = None) -> FastMCP:
    """Build the stdio MCP app exposing exactly the tools ``role`` may use.
    ``role`` defaults to ``DANUS_ROLE`` (env); unset falls back to the read-only
    verifier set (fail-closed)."""
    app = FastMCP("danus-core")
    for name in tools_for(role if role is not None else _role()):
        app.tool(name=name)(_TOOLS[name])
    return app
