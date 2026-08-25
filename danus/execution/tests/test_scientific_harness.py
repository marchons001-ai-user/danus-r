"""Offline A2-5b tests for role-selected worker harnesses."""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from danus.execution import layout as L
from danus.execution import loop, scaffold


@contextmanager
def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_role_harness_selection():
    assert L.harness_contract("worker") == L.worker_md()
    assert L.harness_skills_dir("worker") == L.worker_skills_dir()
    for role in ("researcher", "reviewer", "experimenter"):
        assert L.harness_contract(role) == L.scientific_worker_md()
        assert L.harness_skills_dir(role) is None
    assert L.scientific_worker_md().is_file()


def test_sync_worker_harness_switches_only_managed_links(tmp: Path):
    wl = L.WorkerLayout(tmp / "P" / "workers" / "w")
    (wl.dir / ".agents").mkdir(parents=True)

    scaffold.sync_worker_harness(wl, "worker")
    assert (wl.dir / "AGENTS.md").resolve() == L.worker_md().resolve()
    assert (wl.dir / ".agents" / "skills").resolve() == L.worker_skills_dir().resolve()

    scaffold.sync_worker_harness(wl, "researcher")
    assert (wl.dir / "AGENTS.md").resolve() == L.scientific_worker_md().resolve()
    assert not (wl.dir / ".agents" / "skills").exists()
    assert not (wl.dir / ".agents" / "skills").is_symlink()

    scaffold.sync_worker_harness(wl, "worker")
    assert (wl.dir / "AGENTS.md").resolve() == L.worker_md().resolve()
    assert (wl.dir / ".agents" / "skills").resolve() == L.worker_skills_dir().resolve()


def test_scientific_dsh_prompt_excludes_legacy_math_harness(tmp: Path):
    legacy_contract = tmp / "legacy_worker.md"
    legacy_contract.write_text("LEGACY_WORKER_CONTRACT_MARKER\n", encoding="utf-8")
    legacy_skills = tmp / "legacy_skills" / "math"
    legacy_skills.mkdir(parents=True)
    (legacy_skills / "SKILL.md").write_text("LEGACY_MATH_SKILL_MARKER\n", encoding="utf-8")

    agents_root = tmp / "projects"
    pdir = agents_root / "P"
    pdir.mkdir(parents=True)
    (pdir / L.RESEARCH_CHARTER_FILE).write_text("# Research Charter\nTEST_CHARTER\n", encoding="utf-8")

    with _env(
        DANUS_AGENTS_ROOT=agents_root,
        DANUS_WORKER_CONTRACT=legacy_contract,
        DANUS_WORKER_SKILLS=legacy_skills.parent,
        CODEX_BACKEND="dsh",
    ):
        legacy = loop.kickoff("P", "w", "worker")
        scientific = loop.kickoff("P", "w", "researcher")

    assert "LEGACY_WORKER_CONTRACT_MARKER" in legacy
    assert "LEGACY_MATH_SKILL_MARKER" in legacy
    assert "LEGACY_WORKER_CONTRACT_MARKER" not in scientific
    assert "LEGACY_MATH_SKILL_MARKER" not in scientific
    assert "# Danus-R Scientific Worker Spine" in scientific
    assert "# Danus-R Researcher Contract" in scientific
    assert "TEST_CHARTER" in scientific


def main() -> None:
    test_role_harness_selection()
    print("  [ok] test_role_harness_selection")
    with tempfile.TemporaryDirectory() as d:
        test_sync_worker_harness_switches_only_managed_links(Path(d))
    print("  [ok] test_sync_worker_harness_switches_only_managed_links")
    with tempfile.TemporaryDirectory() as d:
        test_scientific_dsh_prompt_excludes_legacy_math_harness(Path(d))
    print("  [ok] test_scientific_dsh_prompt_excludes_legacy_math_harness")
    print("ALL SCIENTIFIC HARNESS TESTS PASSED")


if __name__ == "__main__":
    main()
