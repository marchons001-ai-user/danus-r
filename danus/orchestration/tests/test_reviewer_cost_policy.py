"""A2-7l cross-model Reviewer cost-policy tests. Offline; no model/network calls."""
from __future__ import annotations

import json
import signal
from pathlib import Path

from danus.execution import layout as L
from danus.execution import loop
from danus.orchestration import cli
from danus.orchestration.control import reviewer_cost_policy


def _worker(
    tmp_path: Path,
    monkeypatch,
    role_text: str = (
        "ROLE=reviewer\nBACKEND=auto\nMODEL=deepseek-v4-flash\nREASONING_EFFORT=high\n"
    ),
) -> L.WorkerLayout:
    root = tmp_path / "projects"
    monkeypatch.setenv("DANUS_AGENTS_ROOT", str(root))
    monkeypatch.setenv("CODEX_BACKEND", "chatgpt")
    cli.do_new("P", roles="high:1")
    wl = L.WorkerLayout(root / "P" / "workers" / "high")
    wl.role.write_text(role_text, encoding="utf-8")
    return wl


def _quota(state: str = "NORMAL", block: bool = False) -> dict:
    return {"state": state, "reason": state.lower(), "block_new_codex_rounds": block}


def test_reviewer_is_dsh_first_across_codex_quota_states():
    for state, block in [
        ("NORMAL", False),
        ("UNKNOWN", False),
        ("PRESSURE", False),
        ("PRESSURE", True),
        ("EXHAUSTED", True),
    ]:
        decision = reviewer_cost_policy(
            _quota(state, block),
            dsh_available=True,
            dsh_quota_exhausted=False,
        )
        assert decision["selected_backend"] == "dsh"
        assert decision["policy"] == "dsh_first_terra_fallback"


def test_only_explicit_dsh_quota_exhaustion_selects_terra():
    normal = reviewer_cost_policy(
        _quota("NORMAL", False),
        dsh_available=True,
        dsh_quota_exhausted=False,
    )
    exhausted = reviewer_cost_policy(
        _quota("NORMAL", False),
        dsh_available=True,
        dsh_quota_exhausted=True,
    )
    assert normal["selected_backend"] == "dsh"
    assert exhausted["selected_backend"] == "chatgpt"
    assert exhausted["reason"] == "dsh_quota_exhausted_fallback_terra"


def test_missing_dsh_does_not_silently_consume_terra():
    decision = reviewer_cost_policy(
        _quota("NORMAL", False),
        dsh_available=False,
        dsh_quota_exhausted=False,
    )
    assert decision["selected_backend"] == "dsh"
    assert decision["reason"] == "dsh_primary_unavailable"


def test_dsh_quota_machine_code_is_not_confused_with_other_failures(tmp_path: Path):
    log = tmp_path / "round.log"
    for text in [
        "dsh: RATE_LIMIT: slow down\n",
        "dsh: AUTH: bad credential\n",
        "dsh: SERVER: unavailable\n",
        "quota exceeded in ordinary prose\n",
    ]:
        log.write_text(text, encoding="utf-8")
        assert loop._dsh_quota_exhausted(log) is False
    log.write_text("dsh: QUOTA: account quota exhausted\n", encoding="utf-8")
    assert loop._dsh_quota_exhausted(log) is True


def test_dsh_quota_failure_is_bound_to_current_assignment(tmp_path: Path, monkeypatch):
    wl = _worker(tmp_path, monkeypatch)
    wl.status.write_text(json.dumps({
        "reviewer_backend_selected": "dsh",
        "reviewer_model_selected": "deepseek-v4-flash",
    }), encoding="utf-8")
    monkeypatch.setattr(loop.scaffold, "sync_worker_harness", lambda *a, **k: None)
    monkeypatch.setattr(loop.scaffold, "write_codex_config", lambda *a, **k: None)
    monkeypatch.setattr(loop, "kickoff", lambda *a, **k: "prompt")
    monkeypatch.setenv("DANUS_ROUND_BEAT", "0")

    events = []

    class Ledger:
        def current(self, worker, refresh_stale=True):
            return {
                "assignment_id": "assignment-1",
                "state": "RUNNING",
                "outcome": None,
                "task_sha256": loop.ProgressLedger.task_sha256(wl.task.read_text(encoding="utf-8")),
            }

        def runtime_event(self, **event):
            events.append(event)

        def update(self, **_kwargs):
            return None

    monkeypatch.setattr(loop, "_progress_ledger", lambda _wl: Ledger())

    def fake_round(_wl, _role, _prompt, log_path, _hard_timeout, context_budget=False):
        assert context_budget is True
        log_path.write_text("dsh: QUOTA: account quota exhausted\n", encoding="utf-8")
        return 1

    monkeypatch.setattr(loop, "run_round", fake_round)
    monkeypatch.setattr(loop.C, "pop_rollover_request", lambda *_: None)
    monkeypatch.setattr(loop.C, "write_checkpoint", lambda *a, **k: None)

    old = signal.getsignal(signal.SIGTERM)
    try:
        assert loop.main(str(wl.dir)) == 0
    finally:
        signal.signal(signal.SIGTERM, old)

    status = json.loads(wl.status.read_text(encoding="utf-8"))
    assert status["state"] == "stopped"
    assert status["reviewer_dsh_quota_exhausted"] is True
    assert status["reviewer_dsh_quota_assignment_id"] == "assignment-1"
    assert any(event.get("note") == "dsh_quota_exhausted" for event in events)


def test_current_assignment_falls_back_to_terra_but_new_assignment_retries_dsh(
    tmp_path: Path,
    monkeypatch,
):
    wl = _worker(tmp_path, monkeypatch)
    dsh = tmp_path / "dsh"
    dsh.write_text("stub\n", encoding="utf-8")
    monkeypatch.setenv("DANUS_DSH_BIN", str(dsh))
    wl.status.write_text(json.dumps({
        "reviewer_backend_selected": "dsh",
        "reviewer_model_selected": "deepseek-v4-flash",
        "reviewer_dsh_quota_exhausted": True,
        "reviewer_dsh_quota_assignment_id": "assignment-1",
    }), encoding="utf-8")

    rows = [{
        "worker": "high",
        "alive": False,
        "progress_state": "RUNNING",
        "assignment_id": "assignment-1",
    }]
    current = cli._reviewer_cost_rows("P", rows, _quota(), apply=True)
    assert current[0]["selected_backend"] == "chatgpt"
    assert current[0]["selected_model"] == "gpt-5.6-terra"

    rows[0]["assignment_id"] = "assignment-2"
    new = cli._reviewer_cost_rows("P", rows, _quota(), apply=True)
    assert new[0]["selected_backend"] == "dsh"
    assert new[0]["selected_model"] == "deepseek-v4-flash"
    status = json.loads(wl.status.read_text(encoding="utf-8"))
    assert "reviewer_dsh_quota_exhausted" not in status
    assert "reviewer_dsh_quota_assignment_id" not in status


def test_terra_fallback_still_obeys_codex_quota_gate(tmp_path: Path, monkeypatch):
    wl = _worker(tmp_path, monkeypatch)
    dsh = tmp_path / "dsh"
    dsh.write_text("stub\n", encoding="utf-8")
    monkeypatch.setenv("DANUS_DSH_BIN", str(dsh))
    wl.status.write_text(json.dumps({
        "reviewer_backend_selected": "dsh",
        "reviewer_model_selected": "deepseek-v4-flash",
        "reviewer_dsh_quota_exhausted": True,
        "reviewer_dsh_quota_assignment_id": "assignment-1",
    }), encoding="utf-8")
    rows = [{
        "worker": "high",
        "alive": False,
        "progress_state": "RUNNING",
        "assignment_id": "assignment-1",
    }]
    quota = _quota("PRESSURE", True)

    selected = cli._reviewer_cost_rows("P", rows, quota, apply=True)
    assert selected[0]["selected_backend"] == "chatgpt"
    assert selected[0]["selected_model"] == "gpt-5.6-terra"
    applied = cli._apply_quota_gate("P", rows, quota)
    assert applied == [{"worker": "high", "result": "pause-at-round-boundary"}]
    assert wl.pause.read_text(encoding="utf-8").startswith("quota:pressure:")


def test_pinned_reviewer_backend_is_never_managed(tmp_path: Path, monkeypatch):
    wl = _worker(
        tmp_path,
        monkeypatch,
        "ROLE=reviewer\nBACKEND=dsh\nMODEL=deepseek-v4-flash\n",
    )
    rows = [{"worker": "high", "alive": False, "progress_state": "RUNNING"}]

    assert cli._reviewer_cost_rows("P", rows, _quota(), apply=True) == []
    assert cli._selected_worker_backend(wl) == "dsh"
    assert "reviewer_backend_selected" not in json.loads(wl.status.read_text(encoding="utf-8"))


def test_direct_start_resolves_auto_reviewer_to_dsh_before_spawn(tmp_path: Path, monkeypatch):
    wl = _worker(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_dsh_available", lambda: True)
    monkeypatch.setattr(cli, "quota_policy", lambda _snapshot: _quota("NORMAL", False))
    spawned = []
    monkeypatch.setattr(cli, "spawn_loop", lambda path, lock_fd=None: spawned.append(Path(path)) or 424242)

    assert cli._start_one(wl) == "started"
    assert spawned == [wl.dir]
    status = json.loads(wl.status.read_text(encoding="utf-8"))
    assert status["reviewer_backend_selected"] == "dsh"
    assert status["reviewer_model_selected"] == "deepseek-v4-flash"


def test_pending_stop_skips_context_migration_turn(tmp_path: Path, monkeypatch):
    wl = _worker(tmp_path, monkeypatch)
    monkeypatch.setattr(loop.scaffold, "sync_worker_harness", lambda *a, **k: None)
    monkeypatch.setattr(loop.scaffold, "write_codex_config", lambda *a, **k: None)
    monkeypatch.setattr(loop, "kickoff", lambda *a, **k: "prompt")
    monkeypatch.setenv("DANUS_ROUND_BEAT", "0")

    def fake_round(wl, role, prompt, log_path, hard_timeout, context_budget=False):
        log_path.write_text(json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5},
        }) + "\n", encoding="utf-8")
        wl.stop.touch()
        return 0

    monkeypatch.setattr(loop, "run_round", fake_round)
    monkeypatch.setattr(loop.C, "pop_rollover_request", lambda *_: None)
    monkeypatch.setattr(loop.C, "budget_reason", lambda *a, **k: "context_tokens")
    monkeypatch.setattr(loop.C, "write_checkpoint", lambda *a, **k: None)

    def forbidden(*_a, **_k):
        raise AssertionError("pending safe-boundary stop must not launch migration")

    monkeypatch.setattr(loop, "_run_context_migration", forbidden)
    old = signal.getsignal(signal.SIGTERM)
    try:
        assert loop.main(str(wl.dir)) == 0
    finally:
        signal.signal(signal.SIGTERM, old)

    assert not wl.stop.exists()
    assert json.loads(wl.status.read_text(encoding="utf-8"))["state"] == "stopped"
