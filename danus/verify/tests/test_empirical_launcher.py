"""Offline command/home tests for the empirical semantic verifier launcher."""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from danus.verify import launcher


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


def _bundle():
    return {
        "passed": True,
        "checks": [{"name": "artifact", "passed": True, "detail": "ok"}],
        "protocols": [{"protocol_id": "p1", "status": "frozen"}],
        "evidence_records": [{"evidence_id": "e1", "primary_metrics": {"rho": 0.61}}],
    }


def test_empirical_command_is_tool_free_and_uses_own_contract():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        home = root / "empirical-agent"
        with _env(
            DANUS_CODEX_BIN="/abs/codex",
            EMPIRICAL_VERIFY_AGENT_HOME=str(home),
            VERIFIER_RESULTS_DIR=str(root / "runs"),
            DANUS_EMPIRICAL_VERIFY_MODEL="m-emp",
            DANUS_EMPIRICAL_VERIFY_EFFORT="low",
            DANUS_CODEX_MODEL=None,
            DANUS_CODEX_EFFORT=None,
        ):
            rid = launcher._allocate_run_id("claim", home)
            cmd = launcher.build_empirical_codex_command(
                rid,
                statement="rho is positive on heldout-v1",
                scope={"dataset": "heldout-v1"},
                evidence_summary="rho=0.61",
                deterministic_bundle=_bundle(),
            )
        assert cmd[0] == "/abs/codex" and cmd[1] == "exec"
        assert cmd[cmd.index("--model") + 1] == "m-emp"
        assert 'model_reasoning_effort="low"' in cmd
        assert "-C" in cmd and str(home.resolve()) in cmd
        assert not any("mcp_servers.danus" in arg for arg in cmd)
        assert "rho=0.61" in cmd[-1] and "heldout-v1" in cmd[-1]
        assert "verification.json" in cmd[-1]


def test_empirical_home_provisions_only_contract():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d) / "empirical-agent"
        with _env(EMPIRICAL_VERIFY_AGENT_HOME=str(home)):
            got = launcher.ensure_empirical_agent_home()
        assert got == home.resolve()
        assert (home / "AGENTS.md").exists()
        assert (home / "AGENTS.md").resolve() == (
            launcher._REPO_ROOT / "agents" / "contracts" / "empirical_verifier.md"
        ).resolve()
        assert not (home / ".agents" / "skills").exists()


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [ok] {name}")
    print("ALL EMPIRICAL LAUNCHER TESTS PASSED")


if __name__ == "__main__":
    main()
