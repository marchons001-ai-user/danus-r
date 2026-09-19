"""The single shared codex launcher — one uniform CALL + env contract.

Every place Danus execs codex (the proving workers in ``danus.execution.loop``,
the verify service in ``danus.verify.launcher``, and the one-shot artifact
renderers in ``danus.authoring.driver``) resolves the codex binary, the model,
the reasoning effort, the subprocess environment, and the ``exec`` command prefix
**through this module** — so the four are uniform across the three sites and there
is exactly one place to change any of them. ``CODEX_BACKEND`` selects an
``AgentAdapter`` (``danus.harness``) for prompt shaping and argv.

All config is read at CALL time (never import time), so services stay
testable/reconfigurable.

Env contract (neutral defaults + back-compat aliases):
  CODEX_BACKEND       exec backend: api | chatgpt (the codex CLI, default) | dsh
                      (DeepSeek Harness headless via bin/codex-dsh)
  DANUS_CODEX_BIN     codex binary; back-compat alias: CODEX_BIN
  DANUS_CODEX_MODEL   neutral default model (default "gpt-5.5")
  DANUS_CODEX_EFFORT  neutral default reasoning effort (default "xhigh")

Each site layers its own per-service override env names on top of the neutral
defaults via ``model(*overrides)`` / ``effort(*overrides)`` (e.g. the verify
service passes ``DANUS_VERIFY_MODEL``; the renderers pass
``DANUS_WRITE_PAPER_MODEL`` / ``DANUS_HUMAN_SUMMARY_MODEL``).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Dict, List, Sequence

from danus.harness import exec_backend, get_adapter

# danus/codex.py → repo root is one parent up; the deployment's bin/codex wrapper
# lives at <repo>/bin/codex.
_REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_MODEL = "gpt-5.5"
DEFAULT_EFFORT = "xhigh"
_SAFE_SANDBOXES = {"read-only", "workspace-write"}


def backend() -> str:
    """The exec backend selected by ``CODEX_BACKEND`` at CALL time.

    ``api`` / ``chatgpt`` (the default ``api``) both exec the codex
    CLI — they differ only in how codex authenticates, which the CLI itself
    decides. ``dsh`` execs DeepSeek Harness headless sessions instead, via
    the ``bin/codex-dsh`` shim (one ``dsh --profile headless`` run per
    ``codex exec`` call; the same argv + exit-code contract).
    """
    return exec_backend()


def _resolve_override(override: str) -> str:
    """An absolute ``DANUS_CODEX_BIN`` override is used as-is; a
    bare/relative name is resolved to its absolute path via PATH so
    subprocess_env can prepend its dir for the ``#!/usr/bin/env node``
    shebang. Falls back to the raw override (exec then surfaces a clear
    FileNotFoundError)."""
    if os.path.isabs(override):
        return override
    return shutil.which(override) or override


def resolve_bin() -> str:
    """Resolve the exec binary at CALL time. Precedence:
      1. ``DANUS_CODEX_BIN`` env,
      2. the backend's binary — ``<repo>/bin/codex-dsh`` when
         ``CODEX_BACKEND=dsh``, else ``<repo>/bin/codex`` if it exists,
      3. ``shutil.which("codex")`` (or ``"codex-dsh"``),
      4. the bare string ``"codex"`` / ``"codex-dsh"`` (so a missing
         binary raises a clear FileNotFoundError at exec time, not import time).
    """
    override = os.environ.get("DANUS_CODEX_BIN")
    if override:
        return _resolve_override(override)
    dsh = backend() == "dsh"
    name = "codex-dsh" if dsh else "codex"
    wrapper = _REPO_ROOT / "bin" / name
    if wrapper.exists():
        return str(wrapper)
    which = shutil.which(name)
    if which:
        return which
    return name


def model(*override_env_names: str, default: str = DEFAULT_MODEL) -> str:
    """The codex model: first non-empty among the given per-service override env
    vars (in order), then the neutral ``DANUS_CODEX_MODEL``, then ``default``."""
    for name in override_env_names:
        val = os.environ.get(name)
        if val:
            return val
    return os.environ.get("DANUS_CODEX_MODEL") or default


def effort(*override_env_names: str, default: str = DEFAULT_EFFORT) -> str:
    """The reasoning effort: first non-empty among the given per-service override
    env vars (in order), then the neutral ``DANUS_CODEX_EFFORT``, then
    ``default``."""
    for name in override_env_names:
        val = os.environ.get(name)
        if val:
            return val
    return os.environ.get("DANUS_CODEX_EFFORT") or default


def subprocess_env(codex_bin: str) -> Dict[str, str]:
    """A copy of ``os.environ`` with the codex binary's DIR prepended to ``PATH``
    so its ``#!/usr/bin/env node`` shebang resolves regardless of how the caller
    was launched.

    Only augments PATH when ``codex_bin`` has a directory component (a concrete
    path); the bare ``"codex"`` fallback must NOT inject the CWD into the
    subprocess PATH.
    """
    env = os.environ.copy()
    if os.path.dirname(codex_bin):
        codex_dir = os.path.dirname(os.path.abspath(codex_bin))
        if codex_dir and codex_dir != ".":
            existing = env.get("PATH", "")
            parts = existing.split(os.pathsep) if existing else []
            if codex_dir not in parts:
                env["PATH"] = codex_dir + (os.pathsep + existing if existing else "")
    return env


def _safe_exec_tail(
    tail: Sequence[str],
    *,
    use_approve_for_me: bool = True,
) -> List[str]:
    """Force every proving exec into a non-escalating filesystem sandbox."""
    cleaned: List[str] = []
    sandbox = None
    i = 0
    while i < len(tail):
        arg = tail[i]
        if arg in {"--dangerously-bypass-approvals-and-sandbox", "--full-auto"}:
            sandbox = sandbox or "workspace-write"
            i += 1
            continue
        if arg == "--sandbox":
            if i + 1 >= len(tail):
                raise ValueError("--sandbox requires a mode")
            sandbox = tail[i + 1]
            i += 2
            continue
        if arg == "--ask-for-approval":
            if i + 1 >= len(tail):
                raise ValueError("--ask-for-approval requires a policy")
            i += 2
            continue
        if arg in {"--config", "-c"} and i + 1 < len(tail):
            value = tail[i + 1].lstrip()
            if value.startswith("approval_policy=") or value.startswith("sandbox_mode="):
                i += 2
                continue
        cleaned.append(arg)
        i += 1

    if sandbox is None:
        sandbox = "workspace-write"
    if sandbox not in _SAFE_SANDBOXES:
        raise ValueError(f"unsafe codex sandbox mode refused: {sandbox}")

    # DSH does not support --approve-for-me; keep the explicit sandbox
    # plus approval_policy="never" for that backend.
    if not use_approve_for_me or sandbox == "read-only":
        return [
            "--sandbox", sandbox,
            "--config", 'approval_policy="never"',
            *cleaned,
        ]

    # Codex workspace-write: --approve-for-me implies the workspace-write
    # sandbox and routes approval requests through it.  This lets worker MCP
    # tools such as gm_add and implementation_submit_contract run.
    return [
        "--approve-for-me",
        *cleaned,
    ]


def exec_cmd(codex_bin: str, model: str, effort: str, *tail: str,
             permission_profile: str | None = None) -> List[str]:
    """The uniform ``codex exec`` command prefix + a fail-closed safe tail.

    The caller may request ``read-only`` or ``workspace-write``. Historical
    full-access flags are downgraded to ``workspace-write`` and approval is
    forced to ``never`` via Codex's current config-override surface. Any other
    sandbox mode is rejected before process launch.
    """
    if permission_profile is not None:
        if backend() == "dsh":
            raise RuntimeError("DSH has no native Codex permission-profile integration")
        return get_adapter().exec_argv(
            codex_bin, model, effort,
            "--config", f'default_permissions="{permission_profile}"',
            "--config", 'approval_policy="never"', *tail,
        )
    use_approve_for_me = backend() != "dsh"
    return get_adapter().exec_argv(
        codex_bin, model, effort,
        *_safe_exec_tail(tail, use_approve_for_me=use_approve_for_me),
    )


def prepare_prompt(
    prompt: str, *paths: Path, tools: Sequence[str] = (),
) -> str:
    """Ask the current AgentAdapter to shape the prompt.

    CodexAdapter returns ``prompt`` unchanged (the CLI loads AGENTS.md / skills
    itself). DshAdapter embeds the given files/skills and, when ``tools`` is
    non-empty, a short-name → ``mcp__danus__*`` footnote.
    """
    return get_adapter().prepare_prompt(prompt, *paths, tools=tools)


def dsh_context(prompt: str, *paths: Path, tools: Sequence[str] = ()) -> str:
    """Back-compat alias for ``prepare_prompt``."""
    return prepare_prompt(prompt, *paths, tools=tools)
