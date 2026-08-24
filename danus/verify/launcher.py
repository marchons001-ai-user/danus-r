"""Cold-start launchers for analytical and empirical verification agents.

The historical mathematical verifier remains unchanged in behavior. Empirical
claims use a separate, tool-free semantic-audit contract after deterministic
evidence checks have passed. Both paths reuse the same subprocess/result plumbing.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from danus import codex

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
VERIFICATION_FILENAMES = ("verification.json", "verificationt.json")


# --------------------------------------------------------------------------- #
# config resolution (env read at call time)                                   #
# --------------------------------------------------------------------------- #

def _agent_home() -> Path:
    return Path(os.getenv("VERIFY_AGENT_HOME", str(_HERE / "agent"))).resolve()


def _empirical_agent_home() -> Path:
    return Path(
        os.getenv("EMPIRICAL_VERIFY_AGENT_HOME", str(_HERE / "empirical_agent"))
    ).resolve()


def _relink(link: Path, target: Path) -> None:
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)


def ensure_agent_home() -> Path:
    """Provision the historical mathematical verifier home if absent."""
    home = _agent_home()
    contract = _REPO_ROOT / "agents" / "contracts" / "verifier.md"
    skills = _REPO_ROOT / "agents" / "skills" / "verify"
    agents_md = home / "AGENTS.md"
    skills_link = home / ".agents" / "skills"
    if agents_md.exists() and skills_link.exists():
        return home
    if not (contract.exists() and skills.exists()):
        return home
    (home / ".agents").mkdir(parents=True, exist_ok=True)
    _relink(agents_md, contract)
    _relink(skills_link, skills)
    return home


def ensure_empirical_agent_home() -> Path:
    """Provision the empirical semantic verifier home.

    It intentionally has no Danus MCP tools or verification skill bundle: all
    machine-checkable work was already done before this agent is called.
    """
    home = _empirical_agent_home()
    contract = _REPO_ROOT / "agents" / "contracts" / "empirical_verifier.md"
    agents_md = home / "AGENTS.md"
    if agents_md.exists():
        return home
    if not contract.exists():
        return home
    home.mkdir(parents=True, exist_ok=True)
    _relink(agents_md, contract)
    return home


def _results_root(agent_home: Optional[Path] = None) -> Path:
    """Verification run dirs; DSH keeps runs under the active agent home."""
    env = os.getenv("VERIFIER_RESULTS_DIR")
    if env:
        return Path(env).resolve()
    return codex.get_adapter().verify_results_root(
        agent_home=(agent_home or _agent_home()),
        package_runs=_HERE / "runs",
    )


def _model() -> str:
    return codex.model("DANUS_VERIFY_MODEL")


def _effort() -> str:
    return codex.effort("DANUS_VERIFY_EFFORT")


def _empirical_model() -> str:
    return codex.model("DANUS_EMPIRICAL_VERIFY_MODEL", "DANUS_VERIFY_MODEL")


def _empirical_effort() -> str:
    return codex.effort("DANUS_EMPIRICAL_VERIFY_EFFORT", "DANUS_VERIFY_EFFORT")


def _timeout() -> Optional[int]:
    return int(os.getenv("CODEX_TIMEOUT_SECONDS", "0")) or None


def _mcp_config_arg() -> str:
    return 'mcp_servers.danus={command="python3",args=["-m","danus.gateway"],env={DANUS_ROLE="verifier"}}'


# --------------------------------------------------------------------------- #
# run-dir allocation                                                          #
# --------------------------------------------------------------------------- #

def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def generate_run_id(statement: str) -> str:
    return f"{_utc_timestamp()}_{hashlib.sha256(statement.encode('utf-8')).hexdigest()[:12]}"


def _allocate_run_id(statement: str, agent_home: Optional[Path] = None) -> str:
    root = _results_root(agent_home)
    root.mkdir(parents=True, exist_ok=True)
    base = generate_run_id(statement)
    run_id, suffix = base, 1
    for _ in range(10000):
        try:
            (root / run_id).mkdir(parents=False, exist_ok=False)
            return run_id
        except FileExistsError:
            suffix += 1
            run_id = f"{base}_{suffix}"
    raise RuntimeError(f"could not allocate a unique run_id under {root} for base={base}")


def _results_dir(run_id: str, agent_home: Optional[Path] = None) -> Path:
    return _results_root(agent_home) / run_id


def _verification_path(run_id: str, agent_home: Optional[Path] = None) -> Optional[Path]:
    for filename in VERIFICATION_FILENAMES:
        path = _results_dir(run_id, agent_home) / filename
        if path.exists():
            return path
    return None


# --------------------------------------------------------------------------- #
# mathematical verifier                                                       #
# --------------------------------------------------------------------------- #

def build_prompt(run_id: str, statement: str, proof: str) -> str:
    output_path = _results_dir(run_id) / VERIFICATION_FILENAMES[0]
    prompt = (
        f"Run_id: {run_id}. "
        f"Statement: {statement}. "
        f"Proof:\n{proof}\n\n"
        "Use AGENTS.md to verify the above proof for the statement. "
        f"Write the verification JSON to this exact path: {output_path}."
    )
    return codex.prepare_prompt(
        prompt,
        _REPO_ROOT / "agents" / "contracts" / "verifier.md",
        _REPO_ROOT / "agents" / "skills" / "verify",
        tools=("search_arxiv_theorems",),
    )


def build_codex_command(run_id: str, statement: str, proof: str) -> List[str]:
    return codex.exec_cmd(
        codex.resolve_bin(), _model(), _effort(),
        "-C", str(_agent_home()),
        "--skip-git-repo-check",
        "-c", _mcp_config_arg(),
        "--dangerously-bypass-approvals-and-sandbox",
        build_prompt(run_id=run_id, statement=statement, proof=proof),
    )


# --------------------------------------------------------------------------- #
# empirical semantic verifier                                                 #
# --------------------------------------------------------------------------- #

def build_empirical_prompt(
    run_id: str,
    *,
    statement: str,
    scope: Dict[str, Any],
    evidence_summary: str,
    deterministic_bundle: Dict[str, Any],
) -> str:
    home = _empirical_agent_home()
    output_path = _results_dir(run_id, home) / VERIFICATION_FILENAMES[0]
    bundle = {
        "statement": statement,
        "scope": scope,
        "evidence_summary": evidence_summary,
        "deterministic_checks": deterministic_bundle.get("checks", []),
        "protocols": deterministic_bundle.get("protocols", []),
        "evidence_records": deterministic_bundle.get("evidence_records", []),
    }
    prompt = (
        f"Run_id: {run_id}.\n"
        "Audit the following empirical scientific claim bundle using AGENTS.md.\n"
        f"{json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2)}\n\n"
        f"Write the verification JSON to this exact path: {output_path}."
    )
    return codex.prepare_prompt(
        prompt,
        _REPO_ROOT / "agents" / "contracts" / "empirical_verifier.md",
    )


def build_empirical_codex_command(
    run_id: str,
    *,
    statement: str,
    scope: Dict[str, Any],
    evidence_summary: str,
    deterministic_bundle: Dict[str, Any],
) -> List[str]:
    return codex.exec_cmd(
        codex.resolve_bin(), _empirical_model(), _empirical_effort(),
        "-C", str(_empirical_agent_home()),
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        build_empirical_prompt(
            run_id,
            statement=statement,
            scope=scope,
            evidence_summary=evidence_summary,
            deterministic_bundle=deterministic_bundle,
        ),
    )


# --------------------------------------------------------------------------- #
# shared subprocess/result plumbing                                            #
# --------------------------------------------------------------------------- #

def _run_verification_command(run_id: str, cmd: List[str], home: Path) -> Dict[str, Any]:
    results_dir = _results_dir(run_id, home)
    results_dir.mkdir(parents=True, exist_ok=True)
    log_path = results_dir / "log.md"
    env = codex.subprocess_env(cmd[0])

    started_at = datetime.now(timezone.utc).isoformat()
    try:
        with log_path.open("w", encoding="utf-8") as log_handle:
            log_handle.write(f"started_at_utc: {started_at}\n")
            log_handle.write(f"command: {shlex.join(cmd)}\n\n")
            log_handle.flush()
            completed = subprocess.run(
                cmd, cwd=home, env=env,
                stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT,
                text=True, timeout=_timeout(), check=False,
            )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=504,
            detail=f"codex exec timed out after {exc.timeout}s. See log at {log_path}",
        ) from exc

    if completed.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"codex exec failed with exit code {completed.returncode}. See log at {log_path}",
        )

    verification_path = _verification_path(run_id, home)
    if verification_path is None:
        expected = results_dir / VERIFICATION_FILENAMES[0]
        raise HTTPException(
            status_code=500,
            detail=f"verification output was not found at {expected}. See log at {log_path}",
        )
    try:
        payload = json.loads(verification_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"verification output at {verification_path} is not valid JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=500,
            detail=f"verification output at {verification_path} must be a JSON object",
        )
    return payload


def run_codex_verification(run_id: str, statement: str, proof: str) -> Dict[str, Any]:
    """Run the historical mathematical proof verifier."""
    home = ensure_agent_home()
    cmd = build_codex_command(run_id=run_id, statement=statement, proof=proof)
    return _run_verification_command(run_id, cmd, home)


def run_empirical_verification(
    run_id: str,
    *,
    statement: str,
    scope: Dict[str, Any],
    evidence_summary: str,
    deterministic_bundle: Dict[str, Any],
) -> Dict[str, Any]:
    """Run the semantic audit only after deterministic evidence checks pass."""
    if not deterministic_bundle.get("passed"):
        raise ValueError("empirical semantic verifier requires passed deterministic checks")
    home = ensure_empirical_agent_home()
    cmd = build_empirical_codex_command(
        run_id,
        statement=statement,
        scope=scope,
        evidence_summary=evidence_summary,
        deterministic_bundle=deterministic_bundle,
    )
    return _run_verification_command(run_id, cmd, home)
