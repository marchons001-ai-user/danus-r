"""Danus verification service.

``service.app`` exposes the historical mathematical ``POST /verify`` route and
the empirical ``POST /verify/empirical`` route. Empirical verification performs
deterministic evidence-integrity checks before any semantic LLM audit.
"""

from __future__ import annotations

from .empirical import check_empirical_evidence
from .launcher import run_codex_verification, run_empirical_verification
from .prechecks import run_prechecks
from .service import EmpiricalVerifyRequest, VerifyRequest, app

__all__ = [
    "app",
    "VerifyRequest",
    "EmpiricalVerifyRequest",
    "run_codex_verification",
    "run_empirical_verification",
    "run_prechecks",
    "check_empirical_evidence",
]
