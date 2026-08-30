"""A2-8f adversarial reliability regressions. No model or network calls."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from danus.core import EvidenceStore, ProgressLedger, ResearchPlanGraph
from danus.execution import context as C
from danus.execution import layout as L
from danus.execution import loop
from danus.orchestration import cli, planning


@contextmanager
def _agents_root(tmp_path: Path):
    old = os.environ.get("DANUS_AGENTS_ROOT")
    os.environ["DANUS_AGENTS_ROOT"] = str(tmp_path / "projects")
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("DANUS_AGENTS_ROOT", None)
        else:
            os.environ["DANUS_AGENTS_ROOT"] = old


def _planned_project(tmp_path: Path, roles: str = "low:2") -> Path:
    cli.do_new("P", roles=roles)
    pdir = L.project_dir("P")
    ResearchPlanGraph.create(pdir, node_id="R1", title="Research")
    return pdir


def test_evidence_torn_content_addressed_record_recovers_atomically(tmp_path: Path):
    store = EvidenceStore(tmp_path)
    protocol_id = store.freeze_protocol(
        question="q",
        hypothesis="h",
        matrix={"units": []},
        primary_metrics=["m"],
        decision_rule="r",
        author="researcher",
    )
    path = store.protocols_dir / f"{protocol_id}.json"
    path.write_text('{"protocol_id":', encoding="utf-8")

    retry_id = store.freeze_protocol(
        question="q",
        hypothesis="h",
        matrix={"units": []},
        primary_metrics=["m"],
        decision_rule="r",
        author="researcher",
    )

    assert retry_id == protocol_id
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["protocol_id"] == protocol_id
    assert record["status"] == "frozen"


def test_evidence_valid_conflict_is_never_silently_overwritten(tmp_path: Path):
    store = EvidenceStore(tmp_path)
    protocol_id = store.freeze_protocol(
        question="q",
        hypothesis="h",
        matrix={"units": []},
        primary_metrics=["m"],
        decision_rule="r",
        author="researcher",
    )
    path = store.protocols_dir / f"{protocol_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["status"] = "tampered"
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="immutable evidence record conflict"):
        store.freeze_protocol(
            question="q",
            hypothesis="h",
            matrix={"units": []},
            primary_metrics=["m"],
            decision_rule="r",
            author="researcher",
        )


def test_concurrent_assign_cannot_create_two_active_assignments_for_one_node(tmp_path: Path):
    with _agents_root(tmp_path):
        pdir = _planned_project(tmp_path)
        ledger = ProgressLedger(pdir)
        gate = threading.Barrier(2)

        def assign(worker: str):
            gate.wait(timeout=2)
            return ledger.assign(
                worker=worker,
                role="researcher",
                node_id="R1",
                plan_version=1,
                task=f"Task for {worker}\n",
            )

        outcomes = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(assign, worker) for worker in ("low", "low2")]
            for future in futures:
                try:
                    outcomes.append(("ok", future.result(timeout=5)))
                except ValueError as exc:
                    outcomes.append(("error", str(exc)))

        assert [kind for kind, _ in outcomes].count("ok") == 1
        assert [kind for kind, _ in outcomes].count("error") == 1
        assert "already has active assignment" in next(value for kind, value in outcomes if kind == "error")
        active = [a for a in ledger.assignments() if a["state"] not in {"DONE", "STALE"}]
        assert len(active) == 1
        assert active[0]["node_id"] == "R1"


def test_supervisor_passes_are_serialized_per_project(tmp_path: Path, monkeypatch):
    with _agents_root(tmp_path):
        _planned_project(tmp_path)
        guard = threading.Lock()
        active = 0
        maximum = 0

        def fake_unlocked(project: str, **kwargs):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.05)
            with guard:
                active -= 1
            return {"project_state": "RUNNING"}

        monkeypatch.setattr(cli, "_do_supervise_unlocked", fake_unlocked)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(cli.do_supervise, "P", apply=True) for _ in range(2)]
            assert [f.result(timeout=5)["project_state"] for f in futures] == ["RUNNING", "RUNNING"]
        assert maximum == 1


def test_plan_repair_bound_survives_changing_validation_reasons(tmp_path: Path, monkeypatch):
    with _agents_root(tmp_path):
        pdir = _planned_project(tmp_path)
        planning._set_role(L.WorkerLayout(L.worker_dir("P", "low")), "researcher")
        planning._set_role(L.WorkerLayout(L.worker_dir("P", "low2")), "reviewer")
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        task = planning._repair_task(pdir)
        ledger = ProgressLedger(pdir)

        results = []
        for attempt, reason in enumerate(("error A", "error B", "error C"), start=1):
            planning._repair_error_path(pdir).parent.mkdir(parents=True, exist_ok=True)
            planning._repair_error_path(pdir).write_text(
                json.dumps({"plan_version": 1, "reason": reason}), encoding="utf-8"
            )
            item = planning._assign_role("P", "researcher", task, "R1")
            results.append(item)
            if item.get("result") == "assigned":
                ledger.update(
                    worker="low",
                    state="DONE",
                    actor="runtime",
                    outcome="COMPLETE",
                )

        assert [item["result"] for item in results] == ["assigned", "assigned", "repair_exhausted"]
        assert [item.get("attempt") for item in results[:2]] == [1, 2]
        repair_assignments = [
            a for a in ledger.assignments()
            if a.get("task_sha256") == ProgressLedger.task_sha256(task)
        ]
        assert len(repair_assignments) == 2


def test_pid_record_rejects_reused_pid_identity(tmp_path: Path, monkeypatch):
    wl = L.WorkerLayout(tmp_path / "P" / "workers" / "low")
    wl.dir.mkdir(parents=True)
    wl.pid.write_text("123 old-start\n", encoding="utf-8")
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(cli, "_proc_state_start", lambda pid: ("S", "new-start"))

    assert cli._read_pid(wl) == 123
    assert cli._worker_alive(wl) is False

    wl.pid.write_text("123\n", encoding="utf-8")
    assert cli._worker_alive(wl) is True


def test_current_human_guidance_is_injected_into_scientific_round_context(tmp_path: Path):
    with _agents_root(tmp_path):
        cli.do_new("P", roles="low:1")
        (L.project_dir("P") / "HUMAN_GUIDANCE.md").write_text(
            "# Current Human Guidance\n\nUse the narrow route.\n", encoding="utf-8"
        )
        text = loop._scientific_context("P", "researcher")
        assert "<CURRENT_HUMAN_GUIDANCE>" in text
        assert "Use the narrow route." in text
        assert "</CURRENT_HUMAN_GUIDANCE>" in text


def test_live_log_cursor_consumes_only_append_delta(tmp_path: Path):
    path = tmp_path / "round.log"
    first = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.started", "item": {"id": "call-1", "type": "mcp_tool_call"}},
    ]
    path.write_text("".join(json.dumps(x) + "\n" for x in first), encoding="utf-8")
    cursor = C.LogCursor()

    a = C.inspect_log(path, cursor=cursor)
    first_offset = cursor.offset
    assert a["thread_id"] == "t"
    assert a["tool_calls"] == 1

    second = [
        {"type": "item.completed", "item": {"id": "call-1", "type": "mcp_tool_call"}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20}},
    ]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(x) + "\n" for x in second))

    b = C.inspect_log(path, cursor=cursor)
    assert cursor.offset > first_offset
    assert b["tool_calls"] == 1
    assert b["tokens"] == 120
    assert b["uncached_tokens"] == 80


def test_process_tree_timeout_escalates_term_to_kill(monkeypatch):
    sent = []

    class Proc:
        pid = 123

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout)

        def terminate(self):
            raise AssertionError("process-group path should be used")

        def kill(self):
            raise AssertionError("process-group path should be used")

    monkeypatch.setattr(loop.os, "getpgid", lambda pid: 456)
    monkeypatch.setattr(loop.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))

    loop._terminate_process_tree(Proc(), grace=0)
    assert sent == [(456, signal.SIGTERM), (456, signal.SIGKILL)]
