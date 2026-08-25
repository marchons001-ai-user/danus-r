"""danus.core — the project truth/evidence layer.

Shared stores:

- ``FactGraph``    — one project-shared content-addressed DAG of verified
                     analytical + empirical scientific facts.
- ``EvidenceStore``— immutable frozen protocols and empirical evidence records.
- ``ResearchPlanGraph`` — coarse project research directions and dependencies.
- ``GlobalMemory`` — project-shared strongly-typed findings (awareness, not truth).
- ``LocalMemory``  — per-worker private scratch log.

Pure data-structure I/O only; *when* to publish / verify / promote stays in agent
contracts and later orchestration. See ``DATA_MODEL.md``.
"""

from __future__ import annotations

from . import bm25, glossary
from .evidence import EvidenceStore
from .factgraph import FactGraph, parse_frontmatter, serialize_fact, statement_of
from .global_memory import GlobalMemory
from .local_memory import DEFAULT_CHANNELS, LocalMemory
from .plan import ResearchPlanGraph
from .schema import (
    EXTERNAL_REF_KEYS,
    FACT_TYPES,
    GLOBAL_KINDS,
    STATUSES,
    Fact,
    clean_external_refs,
    compute_empirical_fact_id,
    compute_fact_id,
)

__all__ = [
    "FactGraph",
    "EvidenceStore",
    "ResearchPlanGraph",
    "GlobalMemory",
    "LocalMemory",
    "DEFAULT_CHANNELS",
    "Fact",
    "FACT_TYPES",
    "GLOBAL_KINDS",
    "STATUSES",
    "EXTERNAL_REF_KEYS",
    "clean_external_refs",
    "compute_fact_id",
    "compute_empirical_fact_id",
    "serialize_fact",
    "parse_frontmatter",
    "statement_of",
    "bm25",
    "glossary",
]
