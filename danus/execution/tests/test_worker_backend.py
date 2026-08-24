"""Offline checks for worker-local backend selection."""

from __future__ import annotations

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


def _run_once(wl: L.WorkerLayout) -> list[str]:
    seen: list[str] = []
    original_kickoff = loop.kickoff
    original_run_round = loop.run_round
    original_sigterm = signal.getsignal(signal.SIGTERM)

    def fake_kickoff(*args):
        seen.append(os.environ["CODEX_BACKEND"])
        return "prompt"

    def fake_round(*args, **kwargs):
        seen.append(os.environ["CODEX_BACKEND"])
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
    return seen


def test_role_backend_overrides_global_backend(tmp: Path):
    wl = _worker(
        tmp,
        "BACKEND=dsh\nMODEL=deepseek-v4-flash\nREASONING_EFFORT=high\n",
    )
    with _env(CODEX_BACKEND="chatgpt"):
        assert _run_once(wl) == ["dsh", "dsh"]


def test_role_without_backend_keeps_global_backend(tmp: Path):
    wl = _worker(tmp, "MODEL=gpt-5.5\nREASONING_EFFORT=high\n")
    with _env(CODEX_BACKEND="chatgpt"):
        assert _run_once(wl) == ["chatgpt", "chatgpt"]


def main() -> None:
    tests = [test_role_backend_overrides_global_backend,
             test_role_without_backend_keeps_global_backend]
    for test in tests:
        with tempfile.TemporaryDirectory() as directory:
            test(Path(directory))
        print(f"  [ok] {test.__name__}")
    print("ALL WORKER BACKEND TESTS PASSED")


if __name__ == "__main__":
    main()
