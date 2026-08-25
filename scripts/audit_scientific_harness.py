#!/usr/bin/env python3
"""Deterministic A2-5 static audit of the current worker harness.

No model, network, subprocess, or project state is used. The script measures the
actual role-selected contracts/skills exposed by the current checkout and asks both
Codex and DSH prompt adapters to build one synthetic kickoff prompt per role. It is
intended for before/after A2-5 comparisons, not runtime orchestration.
"""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from danus.execution import layout as L
from danus.execution import scaffold
from danus.execution.loop import kickoff
from danus.gateway.roles import tools_for

ROLES = ("worker", "researcher", "reviewer", "experimenter")
PROJECT = "A2_5_AUDIT"


@contextmanager
def _env(**values: str):
    old = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _nbytes(path: Path | None) -> int:
    return len(path.read_bytes()) if path is not None and path.is_file() else 0


def _skills(role: str) -> list[Path]:
    root = L.harness_skills_dir(role)
    return [] if root is None else sorted(root.glob("**/SKILL.md"))


def main() -> None:
    charter_template = L.research_charter_template()
    charter_bytes = _nbytes(charter_template)
    rows = []

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdir = root / PROJECT
        pdir.mkdir()
        (pdir / L.RESEARCH_CHARTER_FILE).write_bytes(charter_template.read_bytes())

        for role in ROLES:
            scientific = role in L.SCIENTIFIC_ROLES
            common_contract_bytes = _nbytes(L.harness_contract(role))
            skill_paths = _skills(role)
            skill_bytes = sum(_nbytes(path) for path in skill_paths)
            role_contract_bytes = _nbytes(L.scientific_role_md(role)) if scientific else 0
            worker = f"audit_{role}"
            with _env(DANUS_AGENTS_ROOT=str(root), CODEX_BACKEND="api"):
                codex_prompt_bytes = len(kickoff(PROJECT, worker, role).encode("utf-8"))
            with _env(DANUS_AGENTS_ROOT=str(root), CODEX_BACKEND="dsh"):
                dsh_prompt_bytes = len(kickoff(PROJECT, worker, role).encode("utf-8"))
            gateway_role = scaffold.gateway_role(role)
            rows.append(
                {
                    "role": role,
                    "common": common_contract_bytes,
                    "role_contract": role_contract_bytes,
                    "charter": charter_bytes if scientific else 0,
                    "skills": len(skill_paths),
                    "skill_bytes": skill_bytes,
                    "gateway_role": gateway_role,
                    "tools": len(tools_for(gateway_role)),
                    "codex_prompt": codex_prompt_bytes,
                    "dsh_prompt": dsh_prompt_bytes,
                }
            )

    print("| role | common contract B | role contract B | charter B | skills | skill B | gateway role | tools | Codex explicit prompt B | DSH injected prompt B |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |")
    for row in rows:
        print(
            f"| {row['role']} | {row['common']} | {row['role_contract']} | "
            f"{row['charter']} | {row['skills']} | {row['skill_bytes']} | "
            f"{row['gateway_role']} | {row['tools']} | {row['codex_prompt']} | "
            f"{row['dsh_prompt']} |"
        )

    legacy_skills = sorted(L.worker_skills_dir().glob("**/SKILL.md"))
    legacy_skill_bytes = sum(_nbytes(path) for path in legacy_skills)
    legacy_contract_bytes = _nbytes(L.worker_md())
    print("\nLegacy mathematical harness retained:")
    print(f"- worker.md: {legacy_contract_bytes} B")
    print(f"- worker SKILL.md files: {len(legacy_skills)} files / {legacy_skill_bytes} B")
    print(f"- combined: {legacy_contract_bytes + legacy_skill_bytes} B")


if __name__ == "__main__":
    main()
