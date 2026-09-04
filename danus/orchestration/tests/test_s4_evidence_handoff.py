from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from danus.core import EvidenceStore, ProgressLedger
from danus.core.handoff import HandoffRequests
from danus.execution import layout as L
from danus.gateway import server
from danus.orchestration import autonomy as A
from danus.orchestration import cli, planning


@contextmanager
def _root(tmp_path: Path):
    old = os.environ.get("DANUS_AGENTS_ROOT")
    os.environ["DANUS_AGENTS_ROOT"] = str(tmp_path / "projects")
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("DANUS_AGENTS_ROOT", None)
        else:
            os.environ["DANUS_AGENTS_ROOT"] = old


@contextmanager
def _gateway(pdir: Path):
    values = {
        "DANUS_PROJECT_DIR": str(pdir),
        "DANUS_AUTHOR": "low3",
        "DANUS_ROLE": "experimenter",
    }
    old = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _setup(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
    planning.initialize_research("P", brief="Study one bounded question.")
    pdir = L.project_dir("P")
    ledger = ProgressLedger(pdir)
    ledger.update(worker="low", state="DONE", actor="runtime", outcome="COMPLETE")
    cli.do_assign("P/low3", "run bounded experiment\n", node_id="P0")
    ledger.update(worker="low3", state="RUNNING", actor="runtime")
    return pdir, ledger


def _evidence(pdir: Path, *, score: float) -> str:
    store = EvidenceStore(pdir)
    protocol_id = store.freeze_protocol(
        question="Does the bounded experiment change the metric?",
        hypothesis="The metric is measurable under the frozen protocol.",
        matrix={"case": [1]},
        primary_metrics=["score"],
        decision_rule="record the frozen score",
        author="reviewer",
    )
    return store.add_record(
        protocol_id=protocol_id,
        author="experimenter",
        matrix_coverage={"planned": 1, "completed": 1},
        primary_metrics={"score": score},
    )


def _apply(project: str):
    rows = [
        {"worker": worker, "alive": False, "paused": False}
        for worker in L.list_workers(project)
    ]
    return A.apply_autonomy(
        project,
        [],
        rows=rows,
        assign=cli.do_assign,
        start=lambda wl: "started",
        role_of=cli._read_worker_role,
    )


def test_interpret_persists_evidence_and_dispatches_exact_refs(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, _ = _setup(tmp_path, monkeypatch)
        evidence_id = _evidence(pdir, score=1.0)

        with _gateway(pdir):
            opened = server.handoff_request(
                "INTERPRET",
                "interpret the frozen result",
                evidence_refs=[evidence_id, evidence_id],
            )

        assert opened["evidence_refs"] == [evidence_id]
        handoff = HandoffRequests(ProgressLedger(pdir)).get(opened["handoff_id"])
        assert handoff is not None
        assert handoff["evidence_refs"] == [evidence_id]
        assert evidence_id in handoff["request"]

        applied = _apply("P")
        routed = next(item for item in applied if item.get("action") == "INTERPRET")
        assert routed["result"] == "assigned" and routed["worker"] == "low"
        assert evidence_id in L.WorkerLayout(L.worker_dir("P", "low")).task.read_text(encoding="utf-8")


def test_interpret_rejects_unknown_evidence(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, _ = _setup(tmp_path, monkeypatch)
        with _gateway(pdir):
            with pytest.raises(ValueError, match="unknown evidence_refs"):
                server.handoff_request(
                    "INTERPRET",
                    "interpret result",
                    evidence_refs=["missing-evidence"],
                )
        assert HandoffRequests(ProgressLedger(pdir)).all() == []


def test_interpret_evidence_dedupe_is_fail_closed_on_changed_refs(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, _ = _setup(tmp_path, monkeypatch)
        first_id = _evidence(pdir, score=1.0)
        second_id = _evidence(pdir, score=2.0)

        with _gateway(pdir):
            first = server.handoff_request(
                "INTERPRET",
                "interpret result",
                evidence_refs=[first_id],
            )
            repeated = server.handoff_request(
                "INTERPRET",
                "different wording does not create a new route",
                evidence_refs=[first_id],
            )
            assert repeated["handoff_id"] == first["handoff_id"]
            with pytest.raises(ValueError, match="different evidence_refs"):
                server.handoff_request(
                    "INTERPRET",
                    "new evidence cannot mutate an existing route",
                    evidence_refs=[second_id],
                )

        matching = [
            item
            for item in HandoffRequests(ProgressLedger(pdir)).all()
            if item.get("kind") == "INTERPRET"
        ]
        assert len(matching) == 1
        assert matching[0]["evidence_refs"] == [first_id]


def test_evidence_refs_are_not_added_to_escalate(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        pdir, _ = _setup(tmp_path, monkeypatch)
        evidence_id = _evidence(pdir, score=1.0)
        with _gateway(pdir):
            with pytest.raises(ValueError, match="only for INTERPRET"):
                server.handoff_request(
                    "ESCALATE",
                    "control-plane attention required",
                    evidence_refs=[evidence_id],
                )
