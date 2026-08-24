"""Danus verification HTTP service.

The historical ``POST /verify {statement, proof}`` mathematical contract remains
unchanged. ``POST /verify/empirical`` first runs deterministic provenance and
artifact checks; only a clean bundle is sent to the semantic empirical verifier.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .empirical import check_empirical_evidence
from .launcher import (
    _allocate_run_id,
    _empirical_agent_home,
    run_codex_verification,
    run_empirical_verification,
)
from .prechecks import run_prechecks


class VerifyRequest(BaseModel):
    statement: str = Field(..., min_length=1)
    proof: str = Field(..., min_length=1)


class EmpiricalVerifyRequest(BaseModel):
    statement: str = Field(..., min_length=1)
    project_dir: str = Field(..., min_length=1)
    scope: Dict[str, Any] = Field(default_factory=dict)
    evidence_refs: List[str] = Field(default_factory=list)
    evidence_summary: str = ""


app = FastAPI(title="Danus verify service", version="0.2.0")


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "pid": os.getpid()}


@app.post("/verify")
def verify(request: VerifyRequest) -> Dict[str, Any]:
    """Historical mathematical proof-verification route."""
    rejected = run_prechecks(request.statement, request.proof)
    if rejected is not None:
        status_code, detail = rejected
        raise HTTPException(status_code=status_code, detail=detail)
    run_id = _allocate_run_id(request.statement)
    return run_codex_verification(run_id=run_id, statement=request.statement, proof=request.proof)


def _deterministic_empirical_reject(bundle: Dict[str, Any]) -> Dict[str, Any]:
    errors = [str(x) for x in bundle.get("errors", [])]
    return {
        "verification_type": "empirical",
        "verification_report": {
            "summary": "deterministic empirical evidence checks failed",
            "critical_errors": [
                {"location": "deterministic evidence check", "issue": error}
                for error in errors
            ],
            "gaps": [],
            "deterministic_checks": bundle.get("checks", []),
        },
        "verdict": "wrong",
        "repair_hints": "; ".join(errors) or "repair the empirical evidence record",
    }


@app.post("/verify/empirical")
def verify_empirical(request: EmpiricalVerifyRequest) -> Dict[str, Any]:
    """Verify empirical evidence integrity, then semantic claim support.

    Deterministic failure returns a normal ``verdict=wrong`` and never launches an
    LLM. Only an integrity-clean bundle reaches the semantic verifier.
    """
    bundle = check_empirical_evidence(
        Path(request.project_dir),
        scope=request.scope,
        evidence_refs=request.evidence_refs,
    )
    if not bundle.get("passed"):
        return _deterministic_empirical_reject(bundle)

    run_id = _allocate_run_id(request.statement, _empirical_agent_home())
    result = run_empirical_verification(
        run_id,
        statement=request.statement,
        scope=request.scope,
        evidence_summary=request.evidence_summary,
        deterministic_bundle=bundle,
    )
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="empirical verifier returned a non-object result")
    if result.get("verdict") not in {"correct", "wrong"}:
        raise HTTPException(status_code=500, detail="empirical verifier returned an invalid verdict")
    report = result.get("verification_report")
    if not isinstance(report, dict):
        raise HTTPException(status_code=500, detail="empirical verifier returned an invalid report")
    result = dict(result)
    result["verification_type"] = "empirical"
    result["verification_report"] = {
        **report,
        "deterministic_checks": bundle.get("checks", []),
    }
    return result
