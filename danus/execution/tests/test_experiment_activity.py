"""Offline experiment activity-event regression tests."""
from __future__ import annotations

import json

from danus.execution import experiment as E


def _events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_status_transitions_append_activity_without_duplicate_complete(tmp_path):
    protocol_id = "protocol-1"
    events_path = tmp_path / "experiment_runtime" / protocol_id / "events.jsonl"

    E._write_status(
        tmp_path, protocol_id, "running", 0, 2,
        current_unit="u1", attempt=1, unit_started_at="2026-08-31T12:00:00Z",
        returncode=None, evidence_id=None,
    )
    E._write_status(
        tmp_path, protocol_id, "running", 1, 2,
        current_unit=None, attempt=None, unit_started_at=None,
        returncode=0, evidence_id=None,
    )
    E._write_status(
        tmp_path, protocol_id, "running", 1, 2,
        current_unit="u2", attempt=1, unit_started_at="2026-08-31T12:01:00Z",
        returncode=None, evidence_id=None,
    )
    E._write_status(
        tmp_path, protocol_id, "failed", 1, 2,
        current_unit="u2", attempt=1, unit_started_at="2026-08-31T12:01:00Z",
        returncode=7, evidence_id=None,
    )
    E._write_status(
        tmp_path, protocol_id, "running", 1, 2,
        current_unit="u2", attempt=2, unit_started_at="2026-08-31T12:02:00Z",
        returncode=None, evidence_id=None,
    )
    E._write_status(
        tmp_path, protocol_id, "running", 2, 2,
        current_unit=None, attempt=None, unit_started_at=None,
        returncode=0, evidence_id=None,
    )
    E._write_status(
        tmp_path, protocol_id, "finalizing", 2, 2,
        current_unit=None, attempt=None, unit_started_at=None,
        returncode=0, evidence_id=None,
    )
    E._write_status(
        tmp_path, protocol_id, "complete", 2, 2,
        current_unit=None, attempt=None, unit_started_at=None,
        returncode=0, evidence_id="evidence-1",
    )

    events = _events(events_path)
    assert [event["event"] for event in events] == [
        "UNIT_STARTED",
        "UNIT_COMPLETED",
        "UNIT_STARTED",
        "UNIT_FAILED",
        "UNIT_STARTED",
        "UNIT_COMPLETED",
        "FINALIZING",
        "COMPLETE",
    ]
    assert events[0]["unit_id"] == "u1" and events[0]["attempt"] == 1
    assert events[3]["unit_id"] == "u2" and events[3]["returncode"] == 7
    assert events[4]["unit_id"] == "u2" and events[4]["attempt"] == 2
    assert events[-1]["evidence_id"] == "evidence-1"

    E._write_status(
        tmp_path, protocol_id, "complete", 2, 2,
        current_unit=None, attempt=None, unit_started_at=None,
        returncode=0, evidence_id="evidence-1",
    )
    assert len(_events(events_path)) == len(events)
