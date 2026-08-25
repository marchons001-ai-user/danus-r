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
  DANUS_ROLE          worker | main | verifier | all  (selects exposed tools;
                      unset falls back to the read-only verifier set — fail-closed)
  DANUS_VERIFY_URL    analytical verify endpoint, normally .../verify; the
                      empirical sibling route is derived as .../verify/empirical
  DANUS_PROBLEM_ID    problem id stamped on written facts (default: project name)
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from danus._mcp import FastMCP
from danus.core import FACT_TYPES, FactGraph, GlobalMemory, ResearchPlanGraph
from danus.integrations import search as _arxiv_search

from .roles import tools_for

_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _author() -> str:
    return os.environ.get("DANUS_AUTHOR", "unknown")


def _role() -> str:
    return os.environ.get("DANUS_ROLE", "verifier")


def _project(project: Optional[str] = None) -> Path:
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


def _verify_endpoint(*, empirical: bool = False) -> str:
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
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _verify(statement: str, proof: str) -> Dict[str, Any]:
    return _post_verify(_verify_endpoint(), {"statement": statement, "proof": proof})


def _verify_empirical(*, statement: str, scope: Dict[str, Any], evidence_refs: List[str], evidence_summary: str) -> Dict[str, Any]:
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


def gm_add(kind: str, claim: str, evidence: str = "", verifiable: Optional[bool] = None,
           glossary: Optional[Dict[str, str]] = None, links: Optional[Dict[str, Any]] = None,
           project: Optional[str] = None) -> Dict[str, Any]:
    entry_id = _gm(project).append(
        kind, claim=claim, evidence=evidence, author=_author(),
        verifiable=verifiable, glossary=glossary, links=links,
    )
    return {"id": entry_id, "kind": kind}


def gm_search(query: str, kinds: Optional[List[str]] = None, limit_per_kind: int = 10,
              project: Optional[str] = None) -> Dict[str, Any]:
    return _gm(project).search(query, kinds=kinds, limit_per_kind=limit_per_kind)


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
    fg = _fg()
    gm = _gm()
    problem_id = os.environ.get("DANUS_PROBLEM_ID", Path(_project()).name)
    kind = (fact_type or "analytical").strip().lower()
    preds = predecessors or []
    if kind not in FACT_TYPES:
        return {"accepted": False, "verdict": "error", "error": f"unknown fact_type {fact_type!r}; expected one of {FACT_TYPES}"}
    if not statement.strip():
        return {"accepted": False, "verdict": "error", "error": "statement is required"}
    if kind == "analytical" and not proof.strip():
        return {"accepted": False, "verdict": "error", "error": "analytical fact requires proof"}
    empirical_scope = scope or {}
    empirical_refs = list(dict.fromkeys(str(x).strip() for x in (evidence_refs or []) if str(x).strip()))
    if kind == "empirical":
        if not empirical_scope:
            return {"accepted": False, "verdict": "error", "error": "empirical fact requires non-empty scope"}
        if not empirical_refs:
            return {"accepted": False, "verdict": "error", "error": "empirical fact requires evidence_refs"}
        if glossary_introduces:
            return {"accepted": False, "verdict": "error", "error": "glossary_introduces is currently analytical-only"}
    undefined: List[str] = []
    if kind == "analytical":
        try:
            undefined = fg.undefined_symbols(
                statement=statement, proof=proof, intuition=intuition,
                predecessors=predecessors, glossary_introduces=glossary_introduces,
            )
        except Exception:
            undefined = []
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
        out = {"accepted": False, "verdict": "error", "error": f"verify service returned a non-dict body ({type(result).__name__})"}
        if kind == "analytical":
            out["undefined_symbols"] = undefined
        return out
    verdict = result.get("verdict")
    accepted = verdict == "correct"
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
    promotion_error = None
    if fact_id and source_id:
        try:
            gm.set_status(source_id, "verified", fact_id=fact_id)
        except Exception as e:
            promotion_error = str(e)
    report = result.get("verification_report")
    gm.append(
        "verification",
        claim=statement,
        evidence="verdict: correct" if accepted else (result.get("repair_hints") or "verdict: wrong"),
        author=_author(),
        verifiable=False,
        links={"source_id": source_id, "predecessors": preds, "evidence_refs": empirical_refs if kind == "empirical" else []},
        verification_type=kind,
        scope=empirical_scope if kind == "empirical" else {},
        verdict=verdict,
        fact_id=fact_id,
        write_error=write_error,
        promotion_error=promotion_error,
        verification_report=report,
    )
    if not accepted:
        out = {"accepted": False, "verdict": verdict, "repair_hints": result.get("repair_hints"), "verification_report": report}
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
    return {"query": query, "results": _fg(project).search(query, limit=limit)}


def fact_revoke(
    fact_id: str,
    reason: str,
    project: Optional[str] = None,
    replacement_fact_id: Optional[str] = None,
) -> Dict[str, Any]:
    revoked = _fg(project).revoke(fact_id, reason=reason, replacement_fact_id=replacement_fact_id)
    out: Dict[str, Any] = {"revoked": revoked}
    if replacement_fact_id:
        out["replacement_fact_id"] = replacement_fact_id
    return out


def plan_propose_change(
    base_plan_version: int,
    reason: str,
    operations: List[Dict[str, Any]],
    post_hoc: bool = False,
    evidence_refs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    change_id = _pg().propose_change(
        base_plan_version=base_plan_version,
        proposer=_author(),
        reason=reason,
        operations=operations,
        post_hoc=post_hoc,
        evidence_refs=evidence_refs,
    )
    return {"change_id": change_id, "status": "PROPOSED"}


def plan_review_change(change_id: str, approve: bool, rationale: str) -> Dict[str, Any]:
    record = _pg().review_change(change_id, reviewer=_author(), approve=approve, rationale=rationale)
    return {"change_id": change_id, "status": record["status"]}


def plan_commit_change(change_id: str, project: Optional[str] = None) -> Dict[str, Any]:
    version = _pg(project).commit_change(change_id, committer=_author())
    return {"change_id": change_id, "status": "COMMITTED", "plan_version": version}


def search_arxiv_theorems(query: str, num_results: int = 10) -> Dict[str, Any]:
    return _arxiv_search(query, num_results=num_results)


_TOOLS = {
    "gm_add": gm_add,
    "gm_search": gm_search,
    "fact_submit": fact_submit,
    "fact_search": fact_search,
    "fact_revoke": fact_revoke,
    "plan_propose_change": plan_propose_change,
    "plan_review_change": plan_review_change,
    "plan_commit_change": plan_commit_change,
    "search_arxiv_theorems": search_arxiv_theorems,
}


def build_app(role: Optional[str] = None) -> FastMCP:
    app = FastMCP("danus-core")
    for name in tools_for(role if role is not None else _role()):
        app.tool(name=name)(_TOOLS[name])
    return app
