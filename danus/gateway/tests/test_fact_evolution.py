from __future__ import annotations

import json

import pytest

from danus.core import EvidenceStore, FactGraph
from danus.gateway import server
from danus.gateway.roles import tools_for


def _fact(fg: FactGraph, statement: str, predecessors=None) -> str:
    return fg.add(
        problem_id="p",
        author="test",
        statement=statement,
        proof="verified proof",
        predecessors=predecessors or [],
    )


def test_empirical_search_reaches_evidence_and_raw_artifact(tmp_path):
    store = EvidenceStore(tmp_path)
    artifact = tmp_path / "results.csv"
    artifact.write_text("metric,value\nrmse,0.12\n", encoding="utf-8")
    artifact_ref = store.artifact_ref(artifact)
    protocol_id = store.freeze_protocol(
        question="Does method A reduce RMSE?",
        hypothesis="Method A reduces RMSE.",
        matrix={"condition": ["nominal"]},
        primary_metrics=["rmse"],
        decision_rule="lower is better",
        author="reviewer",
    )
    evidence_id = store.add_record(
        protocol_id=protocol_id,
        author="experimenter",
        primary_metrics={"rmse": 0.12},
        artifacts=[artifact_ref],
    )

    fg = FactGraph(tmp_path)
    fact_id = fg.add_empirical(
        problem_id="p",
        author="researcher",
        statement="Method A reaches RMSE 0.12 under the nominal condition.",
        scope={"condition": "nominal"},
        evidence_refs=[evidence_id],
        evidence_summary="Frozen experiment record.",
    )

    result = next(row for row in fg.search("RMSE nominal") if row["fact_id"] == fact_id)
    assert result["fact_type"] == "empirical"
    assert result["scope"] == {"condition": "nominal"}
    assert result["evidence_refs"] == [evidence_id]

    record = store.get_record(evidence_id)
    assert record is not None
    assert record["protocol_id"] == protocol_id
    assert record["primary_metrics"] == {"rmse": 0.12}
    assert record["artifacts"] == [artifact_ref]
    assert record["artifacts"][0]["path"] == "results.csv"
    assert record["artifacts"][0]["sha256"] == artifact_ref["sha256"]


def test_verified_replacement_archives_old_fact_and_descendants(tmp_path):
    fg = FactGraph(tmp_path)
    old = _fact(fg, "Old claim")
    child = _fact(fg, "Claim derived from old", predecessors=[old])
    replacement = _fact(fg, "Corrected claim")

    revoked = fg.revoke(old, reason="superseded by new verified result", replacement_fact_id=replacement)
    assert revoked == [old, child]
    assert fg.exists(replacement)
    assert not fg.exists(old)
    assert not fg.exists(child)
    assert (fg.revoked_dir / f"{old}.md").is_file()
    assert (fg.revoked_dir / f"{child}.md").is_file()

    events = [json.loads(line) for line in fg.revocation_log.read_text(encoding="utf-8").splitlines()]
    target = next(event for event in events if event["fact_id"] == old)
    dependent = next(event for event in events if event["fact_id"] == child)
    assert target["superseded_by"] == replacement
    assert target["revoked_as_dependent_of"] is None
    assert dependent["superseded_by"] is None
    assert dependent["revoked_as_dependent_of"] == old

    assert all(row["fact_id"] != old for row in fg.search("Old claim"))


def test_replacement_must_be_active_and_independent(tmp_path):
    fg = FactGraph(tmp_path)
    old = _fact(fg, "Old")
    child = _fact(fg, "Dependent", predecessors=[old])

    with pytest.raises(ValueError, match="active fact"):
        fg.revoke(old, reason="bad replacement", replacement_fact_id="missing")
    assert fg.exists(old) and fg.exists(child)

    with pytest.raises(ValueError, match="depends on"):
        fg.revoke(old, reason="circular replacement", replacement_fact_id=child)
    assert fg.exists(old) and fg.exists(child)


def test_gateway_revoke_keeps_supervisor_only_authority(tmp_path, monkeypatch):
    fg = FactGraph(tmp_path)
    old = _fact(fg, "Old gateway claim")
    replacement = _fact(fg, "New gateway claim")
    monkeypatch.setenv("DANUS_PROJECT_DIR", str(tmp_path))

    result = server.fact_revoke(
        old,
        reason="reviewed supersession",
        replacement_fact_id=replacement,
    )
    assert result == {"revoked": [old], "replacement_fact_id": replacement}

    assert "fact_revoke" in tools_for("main")
    assert "fact_revoke" not in tools_for("researcher")
    assert "fact_revoke" not in tools_for("reviewer")
    assert "fact_revoke" not in tools_for("experimenter")
