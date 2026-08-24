"""Offline checks for worker-local backend selection."""

from __future__ import annotations

import os
from pathlib import Path

from danus.execution import layout as L
from danus.execution import loop


def _worker(tmp_path: Path, role_text: str) -> L.WorkerLayout:
    wl = L.WorkerLayout(tmp_path / "proj" / "workers" / "high")
    wl.dir.mkdir(parents=True)
    wl.role.write_text(role_text, encoding="utf-8")
    return wl


def _run_once(wl: L.WorkerLayout, monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setenv("DANUS_ROUND_BEAT", "0")
    monkeypatch.setenv("DANUS_MAX_ROUNDS", "1")
    monkeypatch.setattr(loop.signal, "signal", lambda *args: None)
    monkeypatch.setattr(loop.scaffold, "write_codex_config", lambda *args: None)

    def fake_kickoff(*args):
        seen.append(os.environ["CODEX_BACKEND"])
        return "prompt"

    def fake_round(*args, **kwargs):
        seen.append(os.environ["CODEX_BACKEND"])
        return 0

    monkeypatch.setattr(loop, "kickoff", fake_kickoff)
    monkeypatch.setattr(loop, "run_round", fake_round)
    assert loop.main(str(wl.dir)) == 0
    return seen


def test_role_backend_overrides_global_backend(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_BACKEND", "chatgpt")
    wl = _worker(
        tmp_path,
        "BACKEND=dsh\nMODEL=deepseek-v4-flash\nREASONING_EFFORT=high\n",
    )
    assert _run_once(wl, monkeypatch) == ["dsh", "dsh"]


def test_role_without_backend_keeps_global_backend(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_BACKEND", "chatgpt")
    wl = _worker(tmp_path, "MODEL=gpt-5.5\nREASONING_EFFORT=high\n")
    assert _run_once(wl, monkeypatch) == ["chatgpt", "chatgpt"]
