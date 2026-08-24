"""Offline HTTP tests for /verify/empirical. No real verifier is launched."""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from danus.core import EvidenceStore
from danus.verify import service


@contextmanager
def _env(**values):
    old = {k: os.environ.get(k) for k in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _project(root: Path):
    store = EvidenceStore(root)
    artifact = root / "runs" / "metrics.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"rho": 0.61}\n', encoding="utf-8")
    protocol_id = store.freeze_protocol(
        question="Is condition distance associated with prediction error?",
        hypothesis="Spearman rho is positive on the frozen held-out matrix.",
        matrix={"tasks": "heldout-v1"},
        primary_metrics=["spearman_rho"],
        decision_rule="support iff rho > 0",
        author="reviewer",
    )
    evidence_id = store.add_record(
        protocol_id=protocol_id,
        author="experimenter",
        code_commit="abc123",
        data_ref="heldout-v1",
        config_ref="cfg-v1",
        matrix_coverage={"planned": 600, "completed": 600},
        primary_metrics={"spearman_rho": 0.61},
        artifacts=[store.artifact_ref(artifact)],
    )
    return artifact, evidence_id


def _payload(root: Path, evidence_id: str):
    return {
        "statement": "On heldout-v1, condition distance is positively associated with prediction error.",
        "project_dir": str(root),
        "scope": {"dataset": "heldout-v1", "tasks": 600},
        "evidence_refs": [evidence_id],
        "evidence_summary": "Spearman rho = 0.61 on the frozen matrix.",
    }


def test_deterministic_failure_never_calls_semantic_verifier():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        artifact, evidence_id = _project(root)
        artifact.write_text('{"rho": 0.99}\n', encoding="utf-8")
        original = service.run_empirical_verification
        service.run_empirical_verification = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("semantic verifier must not run")
        )
        try:
            response = TestClient(service.app).post(
                "/verify/empirical", json=_payload(root, evidence_id)
            )
        finally:
            service.run_empirical_verification = original
        assert response.status_code == 200
        body = response.json()
        assert body["verdict"] == "wrong"
        assert body["verification_type"] == "empirical"
        assert any(not c["passed"] for c in body["verification_report"]["deterministic_checks"])


def test_clean_evidence_reaches_semantic_verifier_once():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _, evidence_id = _project(root)
        calls = {"n": 0}
        original = service.run_empirical_verification

        def fake(run_id, **kwargs):
            calls["n"] += 1
            assert kwargs["deterministic_bundle"]["passed"] is True
            assert kwargs["scope"]["dataset"] == "heldout-v1"
            return {
                "verification_report": {
                    "summary": "evidence supports the scoped association claim",
                    "critical_errors": [],
                    "gaps": [],
                },
                "verdict": "correct",
                "repair_hints": "",
            }

        service.run_empirical_verification = fake
        try:
            with _env(
                VERIFIER_RESULTS_DIR=str(root / "verify-runs"),
                EMPIRICAL_VERIFY_AGENT_HOME=str(root / "empirical-agent"),
            ):
                response = TestClient(service.app).post(
                    "/verify/empirical", json=_payload(root, evidence_id)
                )
        finally:
            service.run_empirical_verification = original
        assert response.status_code == 200 and calls["n"] == 1
        body = response.json()
        assert body["verdict"] == "correct"
        assert body["verification_type"] == "empirical"
        assert body["verification_report"]["deterministic_checks"]
        assert all(c["passed"] for c in body["verification_report"]["deterministic_checks"])


def test_empirical_schema_requires_statement_and_project_dir():
    client = TestClient(service.app)
    assert client.post("/verify/empirical", json={}).status_code == 422
    assert client.post(
        "/verify/empirical",
        json={"statement": "x", "project_dir": ""},
    ).status_code == 422


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [ok] {name}")
    print("ALL EMPIRICAL SERVICE TESTS PASSED")


if __name__ == "__main__":
    main()
