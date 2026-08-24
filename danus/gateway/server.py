#!/usr/bin/env python3
"""Danus gateway — the role-gated MCP server.

A thin MCP wrapper over ``danus.core`` (truth/evidence stores) + one external
integration (``danus.integrations`` arXiv search). It exposes only verbs an LLM
cannot reliably implement itself: content-addressed writes, cascade integrity,
verifier-gated fact submission, BM25, and literature lookup.

``fact_submit`` is the ONLY fact-write path. Historical analytical submissions
keep the ``statement + proof`` contract. Empirical submissions use the same tool
with ``fact_type="empirical"`` plus explicit ``scope`` and ``evidence_refs``;
they route through ``/verify/empirical`` before ``FactGraph.add_empirical``.
A verdict is always traced to global memory, and a successfully written fact can
back-link its source global-memory finding to ``verified``.

Config is read from the environment at CALL time (not import time):
  DANUS_PROJECT_DIR   project dir a worker is pinned to
  DANUS_AGENTS_ROOT   root holding all projects; main can address by name
  DANUS_AUTHOR        agent id for attribution
  DANUS_ROLE          worker | main | verifier | all
  DANUS_VERIFY_URL    mathematical verify endpoint, normally .../verify;
                      empirical verification derives .../verify/empirical
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
from danus.core import FACT_TYPES, FactGraph, GlobalMemory
from danus.integrations import search as _arxiv_search

from .roles import tools_for

_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# --------------------------------------------------------------------------- #
# config resolution (env read at call time — testable / reconfigurable)       #
# --------------------------------------------------------------------------- #

def _author() -> str:
    return os.environ.get("DANUS_AUTHOR", "unknown")


def _role() -> str:
    return os.environ.get("DANUS_ROLE", "verifier")


def _project(project: Optional[str] = None) -> Path:
    """Resolve one project without allowing a project-name path escape."""
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


def _verify_timeout() -> int:
    try:
        return int(os.environ.get("DANUS_VERIFY_TIMEOUT", "3600"))
    except ValueError:
        return 3600


def _verify(statement: str, proof: str) -> Dict[str, Any]:
    """Historical mathematical verifier POST; contract unchanged."""
    verify_url = os.environ.get("DANUS_VERIFY_URL", "")
    if not verify_url:
        raise RuntimeError("DANUS_VERIFY_URL is not set (verify service not wired yet)")
    data = json.dumps({"statement": statement, "proof": proof}).encode("utf-8")
    req = urllib.request.Request(
        verify_url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=_verify_timeout()) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _verify_empirical(
    statement: str,
    *,
    scope: Dict[str, Any],
    evidence_refs: List[str],
    evidence_summary: str,
) -> Dict[str, Any]:
    """POST an empirical candidate to the verify service's empirical route."""
    verify_url = os.environ.get("DANUS_VERIFY_URL", "")
    if not verify_url:
        raise RuntimeError("DANUS_VERIFY_URL is not set (verify service not wired yet)")
    empirical_url = verify_url.rstrip("/") + "/empirical"
    payload = {
        "statement": statement,
        "project_dir": str(_project().resolve()),
        "scope": scope,
        "evidence_refs": evidence_refs,
        "evidence_summary": evidence_summary,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        empirical_url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=_verify_timeout()) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


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
    entry_id = _gm(project).append(
        kind, claim=claim, evidence=evidence, author=_author(),
        verifiable=verifiable, glossary=glossary, links=links,
    )
    return {"id": entry_id, "kind": kind}


def gm_search(query: str, kinds: Optional[List[str]] = None, limit_per_kind: int = 10,
              project: Optional[str] = None) -> Dict[str, Any]:
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
    """The sole verifier-gated fact-write path for analytical and empirical facts.

    Backward compatibility: callers that provide only ``statement`` + ``proof``
    follow the historical analytical route unchanged.

    Empirical callers set ``fact_type="empirical"`` and provide non-empty ``scope``
    plus immutable EvidenceStore ``evidence_refs``. Deterministic evidence checks
    run inside ``/verify/empirical`` before any semantic LLM verifier.

    ``source_id`` optionally names the global-memory finding being promoted. It is
    marked ``verified`` only after a fact has actually been written; rejection or
    an accepted-but-failed graph write never promotes the source finding.
    """
    if fact_type not in FACT_TYPES:
        return {
            "accepted": False,
            "verdict": "error",
            "error": f"unknown fact_type {fact_type!r}; expected one of {FACT_TYPES}",
        }

    fg = _fg()
    gm = _gm()
    project_dir = _project()
    problem_id = os.environ.get("DANUS_PROBLEM_ID", Path(project_dir).name)
    scope = scope or {}
    evidence_refs = [str(x).strip() for x in (evidence_refs or []) if str(x).strip()]

    # Symbol coverage belongs only to the analytical path and remains advisory.
    undefined: List[str] = []
    if fact_type == "analytical":
        try:
            undefined = fg.undefined_symbols(
                statement=statement, proof=proof, intuition=intuition,
                predecessors=predecessors, glossary_introduces=glossary_introduces,
            )
        except Exception:
            undefined = []

    # 1) Verify. Transport/service failure means no verdict exists, so nothing is
    # traced or written and the worker gets a clean retry envelope.
    try:
        if fact_type == "empirical":
            result = _verify_empirical(
                statement,
                scope=scope,
                evidence_refs=evidence_refs,
                evidence_summary=evidence_summary,
            )
        else:
            result = _verify(statement, proof)
    except Exception as e:
        return {
            "accepted": False,
            "verdict": "error",
            "verification_type": fact_type,
            "error": str(e),
            "undefined_symbols": undefined,
        }

    if not isinstance(result, dict):
        return {
            "accepted": False,
            "verdict": "error",
            "verification_type": fact_type,
            "error": f"verify service returned a non-dict body ({type(result).__name__})",
            "undefined_symbols": undefined,
        }

    verdict = result.get("verdict")
    accepted = verdict == "correct"

    # 2) Write only an accepted candidate. Both types share the same FactGraph DAG.
    fact_id = None
    write_error = None
    if accepted:
        try:
            if fact_type == "empirical":
                fact_id = fg.add_empirical(
                    problem_id=problem_id,
                    author=_author(),
                    statement=statement,
                    scope=scope,
                    evidence_refs=evidence_refs,
                    evidence_summary=evidence_summary,
                    predecessors=predecessors,
                    intuition=intuition,
                    external_refs=external_refs,
                )
            else:
                fact_id = fg.add(
                    problem_id=problem_id,
                    author=_author(),
                    statement=statement,
                    proof=proof,
                    predecessors=predecessors,
                    glossary_introduces=glossary_introduces,
                    intuition=intuition,
                    external_refs=external_refs,
                )
        except Exception as e:
            write_error = str(e)

    # 3) Close the source-finding promotion loop only after the fact exists.
    source_status_error = None
    if fact_id and source_id:
        try:
            gm.set_status(source_id, "verified", fact_id=fact_id)
        except Exception as e:
            source_status_error = str(e)

    # 4) A real verdict is always persisted, including graph-write failure.
    gm.append(
        "verification",
        claim=statement,
        evidence="verdict: correct" if accepted else (result.get("repair_hints") or "verdict: wrong"),
        author=_author(),
        verifiable=False,
        links={
            "source_id": source_id,
            "predecessors": predecessors or [],
            "evidence_refs": evidence_refs if fact_type == "empirical" else [],
        },
        verification_type=fact_type,
        verdict=verdict,
        fact_id=fact_id,
        write_error=write_error,
        source_status_error=source_status_error,
        verification_report=result.get("verification_report"),
    )

    # 5) Return the same historical envelope plus additive scientific metadata.
    if not accepted:
        return {
            "accepted": False,
            "verdict": verdict,
            "verification_type": fact_type,
            "repair_hints": result.get("repair_hints"),
            "verification_report": result.get("verification_report"),
            "undefined_symbols": undefined,
        }
    if write_error:
        return {
            "accepted": True,
            "fact_id": None,
            "verification_type": fact_type,
            "write_error": write_error,
            "undefined_symbols": undefined,
        }
    out = {
        "accepted": True,
        "fact_id": fact_id,
        "verification_type": fact_type,
        "undefined_symbols": undefined,
    }
    if source_status_error:
        out["source_status_error"] = source_status_error
    return out


def fact_search(query: str, limit: int = 10, project: Optional[str] = None) -> Dict[str, Any]:
    return {"query": query, "results": _fg(project).search(query, limit=limit)}


def fact_revoke(fact_id: str, reason: str, project: Optional[str] = None) -> Dict[str, Any]:
    revoked = _fg(project).revoke(fact_id, reason=reason)
    return {"revoked": revoked}


# --------------------------------------------------------------------------- #
# arXiv theorem search                                                        #
# --------------------------------------------------------------------------- #

def search_arxiv_theorems(query: str, num_results: int = 10) -> Dict[str, Any]:
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
    "search_arxiv_theorems": search_arxiv_theorems,
}


def build_app(role: Optional[str] = None) -> FastMCP:
    app = FastMCP("danus-core")
    for name in tools_for(role if role is not None else _role()):
        app.tool(name=name)(_TOOLS[name])
    return app
