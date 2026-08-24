"""Offline tests for A2-4a scientific knowledge schema.

No verifier, model, network, or experiment runner is involved. These tests only
exercise deterministic storage/identity/provenance behavior.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from danus.core import EvidenceStore, FactGraph, compute_fact_id, parse_frontmatter


def test_analytical_backward_compatibility() -> None:
    # Fixed regression value from the pre-A2-4 analytical hash algorithm.
    fid = compute_fact_id(
        problem_id="P", predecessors=[], glossary_introduces={"X": "a manifold"},
        statement="A holds", proof="proof of A",
    )
    assert fid == "eae0251a61c51aad"

    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d))
        written = fg.add(
            problem_id="P", author="w", statement="A holds", proof="proof of A",
            glossary_introduces={"X": "a manifold"},
        )
        assert written == fid
        raw = fg.get_raw(fid) or ""
        # Old analytical serialization stays old: absent fact_type means analytical.
        assert "fact_type:" not in raw
        assert "## proof" in raw and "## evidence" not in raw
        assert fg.fact_type(fid) == "analytical"
        fm = parse_frontmatter(raw)
        assert fm["scope"] == {} and fm["evidence_refs"] == []


def test_protocol_freeze_and_post_hoc_lineage() -> None:
    with tempfile.TemporaryDirectory() as d:
        store = EvidenceStore(Path(d))
        p1 = store.freeze_protocol(
            question="Does UQ track prediction error?",
            hypothesis="Higher UQ is associated with higher error.",
            matrix={"noise": [0.0, 0.1]},
            primary_metrics=["spearman_rho"],
            decision_rule="support if rho > 0.3",
            author="researcher",
        )
        r1 = store.get_protocol(p1)
        assert r1 and r1["status"] == "frozen" and r1["post_hoc"] is False

        p2 = store.freeze_protocol(
            question="Does UQ track prediction error?",
            hypothesis="The association persists after adding a high-noise cell.",
            matrix={"noise": [0.0, 0.1, 0.2]},
            primary_metrics=["spearman_rho"],
            decision_rule="support if rho > 0.3",
            author="researcher",
            parent_protocol_id=p1,
            post_hoc=True,
            revision_reason="The frozen v1 results exposed a high-noise failure mode.",
        )
        r2 = store.get_protocol(p2)
        assert r2 and r2["parent_protocol_id"] == p1 and r2["post_hoc"] is True
        assert r2["revision_reason"]

        try:
            store.freeze_protocol(
                question="q", hypothesis="h", matrix={}, primary_metrics=["m"],
                decision_rule="d", author="r", post_hoc=True,
            )
            assert False, "post-hoc protocol without a parent must be rejected"
        except ValueError:
            pass

        try:
            store.freeze_protocol(
                question="q", hypothesis="h", matrix={}, primary_metrics=["m"],
                decision_rule="d", author="r", parent_protocol_id=p1,
            )
            assert False, "a revision without a reason must be rejected"
        except ValueError:
            pass


def test_evidence_record_artifact_reference_and_empirical_fact() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        store = EvidenceStore(root)
        protocol_id = store.freeze_protocol(
            question="Does condition distance track error?",
            hypothesis="Larger condition distance is associated with larger error.",
            matrix={"tasks": [1, 2, 3]},
            primary_metrics=["spearman_rho"],
            decision_rule="support if rho > 0",
            author="researcher",
        )

        artifact = root / "runs" / "summary.csv"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("task,error\n1,0.1\n2,0.2\n3,0.3\n", encoding="utf-8")
        aref = store.artifact_ref(artifact)
        assert aref["path"] == "runs/summary.csv"
        assert len(aref["sha256"]) == 64 and aref["size"] == artifact.stat().st_size

        evidence_id = store.add_record(
            protocol_id=protocol_id,
            author="experimenter",
            code_commit="abc123",
            data_ref="heldout-v1",
            config_ref="config-v1",
            matrix_coverage={"completed": 3, "planned": 3},
            primary_metrics={"spearman_rho": 1.0},
            failures=[],
            deviations=[],
            artifacts=[aref],
        )
        record = store.get_record(evidence_id)
        assert record and record["protocol_id"] == protocol_id
        assert record["primary_metrics"]["spearman_rho"] == 1.0

        # Same scientific record deduplicates; created_at is not part of identity.
        evidence_id_2 = store.add_record(
            protocol_id=protocol_id,
            author="experimenter",
            code_commit="abc123",
            data_ref="heldout-v1",
            config_ref="config-v1",
            matrix_coverage={"completed": 3, "planned": 3},
            primary_metrics={"spearman_rho": 1.0},
            artifacts=[aref],
        )
        assert evidence_id_2 == evidence_id

        fg = FactGraph(root)
        base = fg.add(
            problem_id="P", author="researcher", statement="Task split is fixed",
            proof="The split is defined by immutable project input.",
        )
        empirical = fg.add_empirical(
            problem_id="P",
            author="researcher",
            statement="On heldout-v1 tasks 1-3, condition distance is positively associated with error.",
            scope={"dataset": "heldout-v1", "tasks": [1, 2, 3]},
            evidence_refs=[evidence_id],
            evidence_summary="Frozen protocol and complete 3/3 run report rho=1.0.",
            predecessors=[base],
        )
        assert fg.fact_type(empirical) == "empirical"
        assert fg.scope(empirical) == {"dataset": "heldout-v1", "tasks": [1, 2, 3]}
        assert fg.evidence_refs(empirical) == [evidence_id]
        raw = fg.get_raw(empirical) or ""
        assert "## evidence" in raw and "## proof" not in raw
        assert fg.search("condition distance")[0]["fact_id"] == empirical

        # Unified DAG semantics: an empirical fact can depend on an analytical fact.
        revoked = fg.revoke(base, reason="test invalidation")
        assert set(revoked) == {base, empirical}


def test_empirical_fact_requires_scope_and_evidence() -> None:
    with tempfile.TemporaryDirectory() as d:
        fg = FactGraph(Path(d))
        try:
            fg.add_empirical(
                problem_id="P", author="w", statement="claim", scope={},
                evidence_refs=["e1"], evidence_summary="summary",
            )
            assert False, "empty scope must be rejected"
        except ValueError:
            pass
        try:
            fg.add_empirical(
                problem_id="P", author="w", statement="claim", scope={"set": "S"},
                evidence_refs=[], evidence_summary="summary",
            )
            assert False, "missing evidence refs must be rejected"
        except ValueError:
            pass


def main() -> None:
    test_analytical_backward_compatibility()
    print("  [ok] analytical fact id + serialization backward compatibility")
    test_protocol_freeze_and_post_hoc_lineage()
    print("  [ok] frozen protocol + explicit post-hoc revision lineage")
    test_evidence_record_artifact_reference_and_empirical_fact()
    print("  [ok] evidence record + artifact hash + empirical fact + unified DAG")
    test_empirical_fact_requires_scope_and_evidence()
    print("  [ok] empirical fact requires explicit scope + evidence refs")
    print("ALL SCIENTIFIC SCHEMA TESTS PASSED")


if __name__ == "__main__":
    main()
