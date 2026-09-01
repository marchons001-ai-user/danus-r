"""Offline tests for danus.codex — the single shared codex launcher.

Covers binary resolution, model/effort precedence, subprocess environment, backend
selection, prompt shaping, and the shared fail-closed exec policy. Zero network /
API spend. Runs standalone (``python -m danus.tests.test_codex``) and under pytest.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from danus import codex


@contextlib.contextmanager
def env(**kv):
    """Temporarily set env vars (None deletes), restore after."""
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


_ALL = dict(
    CODEX_BACKEND=None,
    DANUS_CODEX_BIN=None, CODEX_BIN=None,
    DANUS_CODEX_MODEL=None, DANUS_CODEX_EFFORT=None,
    DANUS_VERIFY_MODEL=None, DANUS_VERIFY_EFFORT=None,
    DANUS_WRITE_PAPER_MODEL=None, DANUS_WRITE_PAPER_EFFORT=None,
    DANUS_HUMAN_SUMMARY_MODEL=None, DANUS_HUMAN_SUMMARY_EFFORT=None,
)


# --- resolve_bin precedence ------------------------------------------------- #

def test_resolve_bin_prefers_danus_codex_bin_over_alias():
    with env(**{**_ALL, "DANUS_CODEX_BIN": "/x/primary", "CODEX_BIN": "/x/alias"}):
        assert codex.resolve_bin() == "/x/primary"


def test_resolve_bin_falls_back_to_wrapper_then_which_then_bare():
    with env(**_ALL):
        got = codex.resolve_bin()
        import shutil
        wrapper = Path(codex.__file__).resolve().parents[1] / "bin" / "codex"
        if wrapper.exists():
            assert got == str(wrapper)
        else:
            assert got == (shutil.which("codex") or "codex")


def test_resolve_bin_bare_when_nothing_available(monkeypatch=None):
    import shutil
    real_which = shutil.which
    shutil.which = lambda *a, **k: None  # type: ignore[assignment]
    try:
        with env(**_ALL):
            wrapper = Path(codex.__file__).resolve().parents[1] / "bin" / "codex"
            if not wrapper.exists():
                assert codex.resolve_bin() == "codex"
    finally:
        shutil.which = real_which  # type: ignore[assignment]


# --- model / effort precedence ---------------------------------------------- #

def test_model_override_wins_then_neutral_then_default():
    with env(**{**_ALL, "DANUS_VERIFY_MODEL": "override-m", "DANUS_CODEX_MODEL": "neutral-m"}):
        assert codex.model("DANUS_VERIFY_MODEL") == "override-m"
    with env(**{**_ALL, "DANUS_CODEX_MODEL": "neutral-m"}):
        assert codex.model("DANUS_VERIFY_MODEL") == "neutral-m"
    with env(**_ALL):
        assert codex.model("DANUS_VERIFY_MODEL") == codex.DEFAULT_MODEL == "gpt-5.5"


def test_effort_override_wins_then_neutral_then_default():
    with env(**{**_ALL, "DANUS_VERIFY_EFFORT": "override-e", "DANUS_CODEX_EFFORT": "neutral-e"}):
        assert codex.effort("DANUS_VERIFY_EFFORT") == "override-e"
    with env(**{**_ALL, "DANUS_CODEX_EFFORT": "neutral-e"}):
        assert codex.effort("DANUS_VERIFY_EFFORT") == "neutral-e"
    with env(**_ALL):
        assert codex.effort("DANUS_VERIFY_EFFORT") == codex.DEFAULT_EFFORT == "xhigh"


def test_first_override_in_order_wins():
    with env(**{**_ALL, "DANUS_VERIFY_MODEL": "primary", "DANUS_WRITE_PAPER_MODEL": "other"}):
        assert codex.model("DANUS_VERIFY_MODEL", "DANUS_WRITE_PAPER_MODEL") == "primary"


# --- subprocess_env --------------------------------------------------------- #

def test_subprocess_env_prepends_dir_for_concrete_path():
    with env(**{**_ALL, "PATH": "/usr/bin:/bin"}):
        out = codex.subprocess_env("/opt/codex/bin/codex")
        assert out["PATH"].split(os.pathsep)[0] == "/opt/codex/bin"
        assert "/usr/bin" in out["PATH"]


def test_subprocess_env_never_injects_cwd_for_bare_codex():
    with env(**{**_ALL, "PATH": "/usr/bin:/bin"}):
        out = codex.subprocess_env("codex")
        assert out["PATH"] == "/usr/bin:/bin"
        assert "" not in out["PATH"].split(os.pathsep)
        assert "." not in out["PATH"].split(os.pathsep)


def test_subprocess_env_idempotent_when_dir_already_on_path():
    with env(**{**_ALL, "PATH": "/opt/codex/bin:/usr/bin"}):
        out = codex.subprocess_env("/opt/codex/bin/codex")
        assert out["PATH"] == "/opt/codex/bin:/usr/bin"


# --- exec safety ------------------------------------------------------------ #

def test_exec_cmd_defaults_to_non_escalating_workspace_sandbox():
    cmd = codex.exec_cmd("/x/codex", "the-model", "xhigh", "-C", "/home", "-")
    assert cmd == [
        "/x/codex", "exec",
        "--model", "the-model",
        "--config", 'model_reasoning_effort="xhigh"',
        "--sandbox", "workspace-write",
        "--ask-for-approval", "never",
        "-C", "/home", "-",
    ]


def test_exec_cmd_empty_tail_is_still_sandboxed():
    cmd = codex.exec_cmd("codex", "m", "e")
    assert cmd == [
        "codex", "exec", "--model", "m", "--config", 'model_reasoning_effort="e"',
        "--sandbox", "workspace-write", "--ask-for-approval", "never",
    ]


def test_exec_cmd_downgrades_historical_full_access_flag():
    cmd = codex.exec_cmd(
        "codex", "m", "e", "--dangerously-bypass-approvals-and-sandbox", "task"
    )
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert cmd[cmd.index("--ask-for-approval") + 1] == "never"


def test_exec_cmd_preserves_read_only_and_overrides_approval():
    cmd = codex.exec_cmd(
        "codex", "m", "e", "--sandbox", "read-only",
        "--ask-for-approval", "on-request", "task",
    )
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert cmd[cmd.index("--ask-for-approval") + 1] == "never"
    assert "on-request" not in cmd


def test_exec_cmd_refuses_unsafe_sandbox_mode():
    try:
        codex.exec_cmd("codex", "m", "e", "--sandbox", "danger-full-access", "task")
        assert False, "danger-full-access must be refused"
    except ValueError as exc:
        assert "unsafe codex sandbox mode refused" in str(exc)


# --- backend selection + dsh prompt embedding ------------------------------- #

def test_backend_defaults_to_api():
    with env(**_ALL):
        assert codex.backend() == "api"


def test_backend_reads_codex_backend_env():
    with env(**{**_ALL, "CODEX_BACKEND": "dsh"}):
        assert codex.backend() == "dsh"
    with env(**{**_ALL, "CODEX_BACKEND": "chatgpt"}):
        assert codex.backend() == "chatgpt"


def test_resolve_bin_dsh_backend_prefers_shim():
    with env(**{**_ALL, "CODEX_BACKEND": "dsh"}):
        got = codex.resolve_bin()
        shim = Path(codex.__file__).resolve().parents[1] / "bin" / "codex-dsh"
        assert got == str(shim)


def test_resolve_bin_dsh_backend_honors_override():
    with env(**{**_ALL, "CODEX_BACKEND": "dsh", "DANUS_CODEX_BIN": "/x/fake-dsh"}):
        assert codex.resolve_bin() == "/x/fake-dsh"


def test_dsh_context_unchanged_for_codex_backends():
    prompt = "solve it"
    with env(**_ALL):
        assert codex.dsh_context(prompt) == prompt


def test_dsh_context_embeds_files_and_skills_for_dsh_backend():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        contract = root / "contract.md"
        contract.write_text("CONTENT-CONTRACT", encoding="utf-8")
        skill = root / "skills" / "a" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("CONTENT-SKILL-A", encoding="utf-8")
        with env(**{**_ALL, "CODEX_BACKEND": "dsh"}):
            out = codex.dsh_context("solve it", contract, root / "skills")
        assert out.startswith("solve it")
        assert "CONTENT-CONTRACT" in out
        assert "CONTENT-SKILL-A" in out
        assert out.count("<instructions") == 2
        with env(**_ALL):
            out = codex.dsh_context("solve it", contract, root / "skills")
        assert out == "solve it"


def main() -> None:
    tests = [
        test_resolve_bin_prefers_danus_codex_bin_over_alias,
        test_resolve_bin_falls_back_to_wrapper_then_which_then_bare,
        test_resolve_bin_bare_when_nothing_available,
        test_model_override_wins_then_neutral_then_default,
        test_effort_override_wins_then_neutral_then_default,
        test_first_override_in_order_wins,
        test_subprocess_env_prepends_dir_for_concrete_path,
        test_subprocess_env_never_injects_cwd_for_bare_codex,
        test_subprocess_env_idempotent_when_dir_already_on_path,
        test_exec_cmd_defaults_to_non_escalating_workspace_sandbox,
        test_exec_cmd_empty_tail_is_still_sandboxed,
        test_exec_cmd_downgrades_historical_full_access_flag,
        test_exec_cmd_preserves_read_only_and_overrides_approval,
        test_exec_cmd_refuses_unsafe_sandbox_mode,
        test_backend_defaults_to_api,
        test_backend_reads_codex_backend_env,
        test_resolve_bin_dsh_backend_prefers_shim,
        test_resolve_bin_dsh_backend_honors_override,
        test_dsh_context_unchanged_for_codex_backends,
        test_dsh_context_embeds_files_and_skills_for_dsh_backend,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL CODEX LAUNCHER TESTS PASSED")


if __name__ == "__main__":
    main()
