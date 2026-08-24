"""fact graph — project-shared, verified, content-addressed DAG.

One human/agent-readable markdown file per fact. Existing analytical facts retain
the historical statement/proof format and hash. Empirical facts live in the same
DAG but bind a statement to an explicit tested scope and immutable evidence-record
ids. See DATA_MODEL.md §3.

Pure data-structure I/O. *Whether* a claim deserves to be a fact is a verification
gate decision; this module only preserves deterministic identity, dependency
integrity, readable storage, search, and cascade revocation.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import bm25
from . import glossary as _glossary
from ._util import append_jsonl, utc_now
from .schema import Fact, clean_external_refs, compute_empirical_fact_id, compute_fact_id

_PRED_RE = re.compile(r"^predecessors:\s*\[(.*)\]\s*$")
_GLOSS_LINE_RE = re.compile(r"^\s{2}([^:]+):\s*(.*)$")


def statement_of(text: str) -> str:
    """The fact's ``## statement`` body (up to the next ``##`` heading), as a
    one-line snippet — what a searcher needs to recognize a fact."""
    out: List[str] = []
    in_stmt = False
    for line in text.splitlines():
        if line.strip().startswith("## "):
            if in_stmt:
                break
            in_stmt = line.strip().lower() == "## statement"
            continue
        if in_stmt:
            out.append(line.strip())
    return " ".join(s for s in out if s).strip()


def serialize_fact(fact: Fact) -> str:
    """Render a Fact to readable markdown-with-frontmatter.

    Analytical serialization is intentionally unchanged. Only empirical facts
    add ``fact_type`` / ``scope`` / ``evidence_refs`` and an ``## evidence`` body.
    """
    lines = [
        "---",
        f"fact_id: {fact.fact_id}",
        f"problem_id: {fact.problem_id}",
        f"author: {fact.author}",
        f"predecessors: [{', '.join(fact.predecessors)}]",
    ]
    if fact.fact_type == "empirical":
        lines.append("fact_type: empirical")
        lines.append("scope: " + json.dumps(fact.scope, ensure_ascii=False, sort_keys=True))
        lines.append("evidence_refs: " + json.dumps(fact.evidence_refs, ensure_ascii=False))
    if fact.glossary_introduces:
        lines.append("glossary_introduces:")
        for k in sorted(fact.glossary_introduces):
            lines.append(f"  {k}: {fact.glossary_introduces[k]}")
    else:
        lines.append("glossary_introduces: {}")
    lines.append("external_refs: " + json.dumps(fact.external_refs, ensure_ascii=False))
    lines += ["---", "", "## statement", fact.statement.strip()]
    if fact.fact_type == "empirical":
        lines += ["", "## evidence", fact.evidence_summary.strip()]
    else:
        lines += ["", "## proof", fact.proof.strip()]
    if fact.intuition.strip():
        lines += ["", "## intuition", fact.intuition.strip()]
    lines.append("")
    return "\n".join(lines)


def parse_frontmatter(text: str) -> Dict[str, object]:
    """Parse the small frontmatter subset used by the graph.

    Old facts have no ``fact_type``/``scope``/``evidence_refs`` and therefore
    read back as ``analytical`` / ``{}`` / ``[]``.
    """
    preds: List[str] = []
    gloss: Dict[str, str] = {}
    refs: List[Dict[str, object]] = []
    fact_type = "analytical"
    scope: Dict[str, Any] = {}
    evidence_refs: List[str] = []
    in_gloss = False
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if i > 0 and line.strip() == "---":
            break
        stripped = line.strip()
        m = _PRED_RE.match(stripped)
        if m:
            preds = [x.strip() for x in m.group(1).split(",") if x.strip()]
            in_gloss = False
            continue
        if stripped.startswith("fact_type:"):
            fact_type = stripped.split(":", 1)[1].strip() or "analytical"
            in_gloss = False
            continue
        if stripped.startswith("scope:"):
            in_gloss = False
            payload = stripped[len("scope:"):].strip()
            try:
                obj = json.loads(payload) if payload else {}
                scope = obj if isinstance(obj, dict) else {}
            except json.JSONDecodeError:
                scope = {}
            continue
        if stripped.startswith("evidence_refs:"):
            in_gloss = False
            payload = stripped[len("evidence_refs:"):].strip()
            try:
                obj = json.loads(payload) if payload else []
                evidence_refs = [str(x) for x in obj] if isinstance(obj, list) else []
            except json.JSONDecodeError:
                evidence_refs = []
            continue
        if stripped.startswith("glossary_introduces:"):
            in_gloss = "{}" not in line
            continue
        if stripped.startswith("external_refs:"):
            in_gloss = False
            payload = stripped[len("external_refs:"):].strip()
            try:
                obj = json.loads(payload) if payload else []
                refs = obj if isinstance(obj, list) else []
            except json.JSONDecodeError:
                refs = []
            continue
        if in_gloss:
            gm = _GLOSS_LINE_RE.match(line)
            if gm:
                gloss[gm.group(1).strip()] = gm.group(2).strip()
            else:
                in_gloss = False
    return {
        "predecessors": preds,
        "glossary_introduces": gloss,
        "external_refs": refs,
        "fact_type": fact_type,
        "scope": scope,
        "evidence_refs": evidence_refs,
    }


class FactGraph:
    """Rooted at the project directory; the shared verified-fact DAG."""

    def __init__(self, root: Path) -> None:
        self.dir = Path(root) / "fact_graph"
        self.facts_dir = self.dir / "facts"
        self.revoked_dir = self.dir / "_revoked"
        self.glossary_path = self.dir / "glossary.json"
        self.revocation_log = self.dir / "revocation_log.jsonl"

    def _path(self, fact_id: str) -> Path:
        return self.facts_dir / f"{fact_id}.md"

    def _check_predecessors(self, predecessors: List[str]) -> None:
        for pid in predecessors:
            if (self.revoked_dir / f"{pid}.md").exists():
                raise ValueError(f"predecessor_revoked: {pid}")

    # ------------------------------------------------------------------ write
    def add(
        self,
        *,
        problem_id: str,
        author: str,
        statement: str,
        proof: str,
        predecessors: Optional[List[str]] = None,
        glossary_introduces: Optional[Dict[str, str]] = None,
        intuition: str = "",
        external_refs: Optional[List[Dict[str, object]]] = None,
    ) -> str:
        """Write a verified analytical fact; historical API/id behavior unchanged."""
        predecessors = [p for p in (predecessors or []) if p]
        glossary_introduces = glossary_introduces or {}
        external_refs = clean_external_refs(external_refs)
        self._check_predecessors(predecessors)
        fact_id = compute_fact_id(
            problem_id=problem_id,
            predecessors=predecessors,
            glossary_introduces=glossary_introduces,
            statement=statement,
            proof=proof,
        )
        fact = Fact(
            fact_id=fact_id, problem_id=problem_id, author=author,
            predecessors=predecessors, statement=statement, proof=proof,
            glossary_introduces=glossary_introduces, intuition=intuition,
            external_refs=external_refs,
        )
        self.facts_dir.mkdir(parents=True, exist_ok=True)
        self._path(fact_id).write_text(serialize_fact(fact), encoding="utf-8")
        self._merge_glossary(glossary_introduces)
        return fact_id

    def add_empirical(
        self,
        *,
        problem_id: str,
        author: str,
        statement: str,
        scope: Dict[str, Any],
        evidence_refs: List[str],
        evidence_summary: str,
        predecessors: Optional[List[str]] = None,
        intuition: str = "",
        external_refs: Optional[List[Dict[str, object]]] = None,
    ) -> str:
        """Write an already-verified empirical fact into the same DAG.

        Verification is deliberately outside this data-structure call, just as it
        is for ``add``. Structural requirements here are only the ones needed for
        an empirical fact to remain interpretable and traceable.
        """
        predecessors = [p for p in (predecessors or []) if p]
        refs = sorted({str(x).strip() for x in evidence_refs if str(x).strip()})
        if not statement.strip():
            raise ValueError("empirical fact requires statement")
        if not scope:
            raise ValueError("empirical fact requires non-empty scope")
        if not refs:
            raise ValueError("empirical fact requires at least one evidence_ref")
        self._check_predecessors(predecessors)
        fact_id = compute_empirical_fact_id(
            problem_id=problem_id,
            predecessors=predecessors,
            statement=statement,
            scope=scope,
            evidence_refs=refs,
        )
        fact = Fact(
            fact_id=fact_id, problem_id=problem_id, author=author,
            predecessors=predecessors, statement=statement, proof="",
            intuition=intuition, external_refs=clean_external_refs(external_refs),
            fact_type="empirical", scope=scope, evidence_refs=refs,
            evidence_summary=evidence_summary,
        )
        self.facts_dir.mkdir(parents=True, exist_ok=True)
        self._path(fact_id).write_text(serialize_fact(fact), encoding="utf-8")
        return fact_id

    def _merge_glossary(self, new: Dict[str, str]) -> None:
        if not new:
            return
        cur = self.glossary()
        cur.update({str(k): str(v) for k, v in new.items()})
        self.glossary_path.parent.mkdir(parents=True, exist_ok=True)
        self.glossary_path.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------- read
    def exists(self, fact_id: str) -> bool:
        return self._path(fact_id).exists()

    def list(self) -> List[str]:
        if not self.facts_dir.exists():
            return []
        return sorted(p.stem for p in self.facts_dir.glob("*.md"))

    def get_raw(self, fact_id: str) -> Optional[str]:
        """The fact's markdown (agents read markdown directly)."""
        p = self._path(fact_id)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def fact_type(self, fact_id: str) -> str:
        raw = self.get_raw(fact_id) or ""
        return str(parse_frontmatter(raw)["fact_type"])

    def scope(self, fact_id: str) -> Dict[str, Any]:
        raw = self.get_raw(fact_id) or ""
        return parse_frontmatter(raw)["scope"]  # type: ignore[return-value]

    def evidence_refs(self, fact_id: str) -> List[str]:
        raw = self.get_raw(fact_id) or ""
        return parse_frontmatter(raw)["evidence_refs"]  # type: ignore[return-value]

    def glossary(self) -> Dict[str, str]:
        """The accumulated project glossary (symbol -> definition)."""
        if self.glossary_path.exists():
            try:
                return json.loads(self.glossary_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def search(self, query: str, limit: int = 10) -> List[Dict[str, object]]:
        """BM25 over all verified fact bodies, analytical and empirical."""
        fids = self.list()
        if not fids:
            return []
        raws = [self.get_raw(fid) or "" for fid in fids]
        docs = [bm25.tokenize(r) for r in raws]
        scores = bm25.bm25_scores(query, docs)
        ranked: List[Dict[str, object]] = []
        for fid, raw, score in sorted(zip(fids, raws, scores), key=lambda t: -t[2]):
            if score <= 0:
                break
            ranked.append({"fact_id": fid, "score": score, "statement": statement_of(raw)})
            if len(ranked) >= limit:
                break
        return ranked

    def predecessors(self, fact_id: str) -> List[str]:
        raw = self.get_raw(fact_id) or ""
        return parse_frontmatter(raw)["predecessors"]  # type: ignore[return-value]

    def external_refs(self, fact_id: str) -> List[Dict[str, object]]:
        """The fact's structured external bibliography (``[]`` if none / absent)."""
        raw = self.get_raw(fact_id) or ""
        return parse_frontmatter(raw)["external_refs"]  # type: ignore[return-value]

    def set_external_refs(self, fact_id: str, external_refs: List[Dict[str, object]]) -> List[Dict[str, object]]:
        """Replace a fact's mutable ``external_refs`` without changing its id."""
        p = self._path(fact_id)
        if not p.exists():
            raise ValueError(f"unknown fact_id: {fact_id}")
        refs = clean_external_refs(external_refs)
        new_line = "external_refs: " + json.dumps(refs, ensure_ascii=False)
        lines = p.read_text(encoding="utf-8").splitlines()
        close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if close is None:
            raise ValueError(f"malformed fact file (no frontmatter close): {fact_id}")
        idx = next((i for i in range(1, close) if lines[i].startswith("external_refs:")), None)
        if idx is not None:
            lines[idx] = new_line
        else:
            lines.insert(close, new_line)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return refs

    def descendants(self, fact_id: str) -> List[str]:
        """All facts that (transitively) depend on ``fact_id``."""
        out: List[str] = []
        seen = set()
        frontier = [fact_id]
        while frontier:
            cur = frontier.pop()
            for fid in self.list():
                if fid in seen:
                    continue
                if cur in self.predecessors(fid):
                    out.append(fid)
                    seen.add(fid)
                    frontier.append(fid)
        return out

    # --------------------------------------------------------- glossary check
    def undefined_symbols(
        self,
        *,
        statement: str,
        proof: str,
        intuition: str = "",
        predecessors: Optional[List[str]] = None,
        glossary_introduces: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        """Analytical symbol-coverage heuristic used by the current math gate."""
        defined = _glossary.global_terms()
        defined |= set(self.glossary())
        defined |= set(glossary_introduces or {})
        for pid in (predecessors or []):
            raw = self.get_raw(pid)
            if raw:
                defined |= set(parse_frontmatter(raw)["glossary_introduces"])  # type: ignore[arg-type]
        return _glossary.undefined_symbols(
            statement=statement, proof=proof, intuition=intuition, defined=defined
        )

    # --------------------------------------------------------------- revoke
    def revoke(self, fact_id: str, reason: str) -> List[str]:
        """Cascade-revoke ``fact_id`` and every scientific fact depending on it."""
        if not self.exists(fact_id):
            raise ValueError(f"unknown fact_id: {fact_id}")
        to_revoke = [fact_id] + self.descendants(fact_id)
        self.revoked_dir.mkdir(parents=True, exist_ok=True)
        for fid in to_revoke:
            src = self._path(fid)
            if src.exists():
                shutil.move(str(src), str(self.revoked_dir / f"{fid}.md"))
            append_jsonl(
                self.revocation_log,
                {
                    "timestamp_utc": utc_now(),
                    "fact_id": fid,
                    "reason": reason,
                    "revoked_as_dependent_of": fid != fact_id and fact_id or None,
                },
            )
        return to_revoke
