"""Schemas + content-addressed ids for Danus truth-layer records.

Kept minimal on purpose: code only models the *fixed* parts of the data
structures. Behavior — when to publish, verify, promote, or interpret — stays in
prompts/skills.

Backward compatibility is load-bearing: the existing analytical ``Fact`` shape
and ``compute_fact_id`` hash are preserved exactly. Scientific extensions are
additive: empirical facts use a separate canonical hash payload and optional
fields that old analytical facts simply omit on disk.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

# --------------------------------------------------------------------------- #
# global memory kinds (the strong categorization) + statuses                  #
# --------------------------------------------------------------------------- #

# kind -> default `verifiable` (objectively checkable vs. a judgment).
GLOBAL_KINDS: Dict[str, bool] = {
    "conclusion": True,
    "example": True,
    "counterexample": True,
    "proof_attempt": True,
    "plan": False,
    "dead_end": False,
    "direction": False,
    "obstacle": False,
    "master_guidance": False,  # main agent, via GPT-5.5-pro (DATA_MODEL.md §2.3)
    "verification": False,     # trace of a fact_submit verification outcome (logged by fact_submit)
    "elaboration": False,      # main agent's periodic high-signal progress synthesis (DATA_MODEL.md §2.4)
}

# A global-memory entry's lifecycle. Set/advanced by the agent; the store just
# records it (no enforcement machinery).
STATUSES = (
    "unverified", "verifying", "verified", "refuted",  # verifiable entries
    "open", "supported", "challenged",                  # judgment entries
)

# Fact types are classified by how the claim is supported/verified, not by its
# rhetorical role. A counterexample can therefore be analytical or empirical.
FACT_TYPES = ("analytical", "empirical")


# --------------------------------------------------------------------------- #
# fact node (the only structured schema we keep)                              #
# --------------------------------------------------------------------------- #

# Canonical key order for a structured external reference (a published result the
# proof cites). Extra keys are preserved but sorted after these. Kept loose on
# purpose — bibliographic data is filled by the worker and corrected by the paper
# pipeline's reference auditor, not policed here.
EXTERNAL_REF_KEYS = ("key", "authors", "title", "arxiv", "year", "venue", "doi", "cited_for")


def clean_external_refs(refs: object) -> List[Dict[str, object]]:
    """Normalize an external-refs payload to a list of plain JSON-safe dicts with a
    stable key order. Drops non-dict entries; never raises (advisory data)."""
    if not refs:
        return []
    out: List[Dict[str, object]] = []
    for r in refs:  # type: ignore[union-attr]
        if not isinstance(r, dict):
            continue
        ordered = {k: r[k] for k in EXTERNAL_REF_KEYS if k in r}
        for k in sorted(r):  # preserve any extra keys, deterministically
            if k not in ordered:
                ordered[k] = r[k]
        out.append(ordered)
    return out


@dataclass
class Fact:
    """A verified scientific fact = one node in the shared fact graph.

    Existing analytical facts use ``statement`` + ``proof`` and retain their
    exact historical on-disk/hash behavior. Empirical facts additionally carry a
    machine-readable ``scope`` and immutable ``evidence_refs`` plus a short human
    ``evidence_summary``; those fields are ignored by analytical facts.
    """

    fact_id: str
    problem_id: str
    author: str
    predecessors: List[str]                    # bare-hex fact ids (the DAG)
    statement: str
    proof: str
    glossary_introduces: Dict[str, str] = field(default_factory=dict)  # symbol -> definition
    intuition: str = ""
    # Structured bibliography of external (published) results the proof cites.
    # Mutable metadata — NOT part of the analytical content-addressed fact_id.
    external_refs: List[Dict[str, object]] = field(default_factory=list)
    # Scientific extensions. Defaults preserve old analytical callers/files.
    fact_type: str = "analytical"
    scope: Dict[str, Any] = field(default_factory=dict)
    evidence_refs: List[str] = field(default_factory=list)
    evidence_summary: str = ""


def _normalize(text: str) -> str:
    """Whitespace-stable canonical form for content hashing (cosmetic edits do
    not perturb the fact_id)."""
    return re.sub(r"\s+", " ", text or "").strip()


def compute_fact_id(
    *,
    problem_id: str,
    predecessors: List[str],
    glossary_introduces: Dict[str, str],
    statement: str,
    proof: str,
) -> str:
    """Historical analytical-fact id. DO NOT change this payload.

    Deterministic 16-hex SHA-256 of the canonical content. ``external_refs`` and
    newer scientific fields are deliberately absent so every pre-A2-4 analytical
    fact keeps the exact same id.
    """
    body = {
        "problem_id": problem_id,
        "predecessors": sorted(predecessors),
        "glossary_introduces": dict(
            sorted((str(k), str(v)) for k, v in glossary_introduces.items())
        ),
        "statement": _normalize(statement),
        "proof": _normalize(proof),
    }
    canon = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()[:16]


def compute_empirical_fact_id(
    *,
    problem_id: str,
    predecessors: List[str],
    statement: str,
    scope: Dict[str, Any],
    evidence_refs: List[str],
) -> str:
    """Content-address an empirical claim without touching analytical ids.

    Identity binds the claim to its tested scope and immutable evidence-record
    ids. ``evidence_summary`` is intentionally excluded: it is explanatory text,
    not the evidence identity. Evidence records themselves are content-addressed.
    """
    refs = sorted({str(x).strip() for x in evidence_refs if str(x).strip()})
    body = {
        "fact_type": "empirical",
        "problem_id": problem_id,
        "predecessors": sorted(predecessors),
        "statement": _normalize(statement),
        "scope": scope,
        "evidence_refs": refs,
    }
    canon = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()[:16]
