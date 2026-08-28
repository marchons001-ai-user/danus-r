"""Offline checks for worker-local backend selection."""

from __future__ import annotations

import json
import os
import signal
import tempfile
from contextlib import contextmanager
from pathlib import Path

from danus.execution import layout as L
from danus.execution import loop


@contextmanager
def _env(**values):
    old = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _worker(tmp: Path, role_text: str) -> L.WorkerLayout:
    wl = L.WorkerLayout(tmp / "proj" / "workers" / "high")
    wl.dir.mkdir(parents=True)
    wl.role.write_text(role_text, encoding="utf-8")
    return wl


def _run_once(wl: L.WorkerLayout) -> tuple[list[str], list[str]]:
    backends: list[str] = []
    models: list[str] = []
    original_kickoff = loop.kickoff
    original_run_round = loop.run_round
    original_sigterm = signal.getsignal(signal.SIGTERM)

    def fake_kickoff(*args):
        backends.append(os.environ["CODEX_BACKEND"])
        return "prompt"

    def fake_round(_wl, role, *_args, **_kwargs):
        backends.append(os.environ["CODEX_BACKEND"])
        models.append(role["MODEL"])
        return 0

    loop.kickoff = fake_kickoff
    loop.run_round = fake_round
    try:
        with _env(DANUS_ROUND_BEAT="0", DANUS_MAX_ROUNDS="1"):
            assert loop.main(str(wl.dir)) == 0
    finally:
        loop.kickoff = original_kickoff
        loop.run_round = original_run_round
        signal.signal(signal.SIGTERM, original_sigterm)
    return backends, models


def test_role_backend_overrides_global_backend(tmp: Path):
    wl = _worker(
        tmp,
        "BACKEND=dsh\nMODEL=deepseek-v4-flash\nREASONING_EFFORT=high\n",
    )
    with _env(CODEX_BACKEND="chatgpt"):
        backends, models = _run_once(wl)
    assert backends == ["dsh", "dsh"]
    assert models == ["deepseek-v4-flash"]


def test_role_without_backend_keeps_global_backend(tmp: Path):
    wl = _worker(tmp, "MODEL=gpt-5.5\nREASONING_EFFORT=high\n")
    with _env(CODEX_BACKEND="chatgpt"):
        backends, models = _run_once(wl)
    assert backends == ["chatgpt", "chatgpt"]
    assert models == ["gpt-5.5"]


def test_auto_reviewer_uses_persisted_dsh_selection(tmp: Path):
    wl = _worker(
        tmp,
        "BACKEND=auto\nROLE=reviewer\nMODEL=deepseek-v4-flash\nREASONING_EFFORT=high\n",
    )
    wl.status.write_text(json.dumps({
        "reviewer_backend_selected": "dsh",
        "reviewer_model_selected": "deepseek-v4-flash",
    }), encoding="utf-8")
    with _env(CODEX_BACKEND="chatgpt"):
        backends, models = _run_once(wl)
    assert backends == ["dsh", "dsh"]
    assert models == ["deepseek-v4-flash"]


def test_auto_reviewer_without_selection_defaults_to_dsh(tmp: Path):
    wl = _worker(
        tmp,
        "BACKEND=auto\nROLE=reviewer\nMODEL=deepseek-v4-flash\nREASONING_EFFORT=high\n",
    )
    with _env(CODEX_BACKEND="chatgpt"):
        backends, models = _run_once(wl)
    assert backends == ["dsh", "dsh"]
    assert models == ["deepseek-v4-flash"]


def test_auto_reviewer_terra_fallback_uses_chatgpt_backend_and_terra_model(tmp: Path):
    wl = _worker(
        tmp,
        "BACKEND=auto\nROLE=reviewer\nMODEL=deepseek-v4-flash\nREASONING_EFFORT=high\n",
    )
    wl.status.write_text(json.dumps({
        "reviewer_backend_selected": "chatgpt",
        "reviewer_model_selected": "gpt-5.6-terra",
    }), encoding="utf-8")
    with _env(CODEX_BACKEND="dsh"):
        backends, models = _run_once(wl)
    assert backends == ["chatgpt", "chatgpt"]
    assert models == ["gpt-5.6-terra"]


def main() -> None:
    tests = [
        test_role_backend_overrides_global_backend,
        test_role_without_backend_keeps_global_backend,
        test_auto_reviewer_uses_persisted_dsh_selection,
        test_auto_reviewer_without_selection_defaults_to_dsh,
        test_auto_reviewer_terra_fallback_uses_chatgpt_backend_and_terra_model,
    ]
    for test in tests:
        with tempfile.TemporaryDirectory() as directory:
            test(Path(directory))
        print(f"  [ok] {test.__name__}")
    print("ALL WORKER BACKEND TESTS PASSED")


if __name__ == "__main__":
    main()
