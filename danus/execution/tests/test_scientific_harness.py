"""Offline A2-5 tests for role-selected worker harnesses and tool surfaces."""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from danus.execution import layout as L
from danus.execution import loop, scaffold
from danus.gateway.roles import tools_for


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
    assert L.harness_skills_dir("researcher") is None
    assert L.harness_skills_dir("reviewer") is None
    experimenter_skills = L.harness_skills_dir("experimenter")
    assert experimenter_skills is not None and experimenter_skills.is_dir()
    assert sorted(p.parent.name for p in experimenter_skills.glob("**/SKILL.md")) == [
        "ponytail", "scientific-performance",
    ]
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

    scaffold.sync_worker_harness(wl, "experimenter")
    assert (wl.dir / "AGENTS.md").resolve() == L.scientific_worker_md().resolve()
    assert (wl.dir / ".agents" / "skills").resolve() == L.harness_skills_dir("experimenter").resolve()

    scaffold.sync_worker_harness(wl, "worker")
    assert (wl.dir / "AGENTS.md").resolve() == L.worker_md().resolve()
    assert (wl.dir / ".agents" / "skills").resolve() == L.worker_skills_dir().resolve()


def test_role_specific_gateway_config(tmp: Path):
    wl = L.WorkerLayout(tmp / "P" / "workers" / "w")
    wl.dir.mkdir(parents=True)
    expected = {
        "worker": "worker",
        "researcher": "researcher",
        "reviewer": "reviewer",
        "experimenter": "experimenter",
    }
    for role, gateway_role in expected.items():
        scaffold.write_codex_config(wl, role)
        cfg = wl.codex_config.read_text(encoding="utf-8")
        assert f'DANUS_ROLE = "{gateway_role}"' in cfg
    assert scaffold.gateway_role("legacy") == "worker"


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
        researcher = loop.kickoff("P", "w", "researcher")
        reviewer = loop.kickoff("P", "w", "reviewer")
        experimenter = loop.kickoff("P", "w", "experimenter")

    assert "LEGACY_WORKER_CONTRACT_MARKER" in legacy
    assert "LEGACY_MATH_SKILL_MARKER" in legacy
    assert "LEGACY_WORKER_CONTRACT_MARKER" not in researcher
    assert "LEGACY_MATH_SKILL_MARKER" not in researcher
    assert "# Danus-R Scientific Worker Spine" in researcher
    assert "# Danus-R Researcher Contract" in researcher
    assert "TEST_CHARTER" in researcher

    assert "# Ponytail" not in researcher
    assert "# Ponytail" not in reviewer
    assert "# Scientific Performance" not in researcher
    assert "# Scientific Performance" not in reviewer
    assert "# Ponytail" in experimenter
    assert "# Scientific Performance" in experimenter

    fact_submit_note = "fact_submit -> mcp__danus__fact_submit"
    arxiv_note = "search_arxiv_theorems -> mcp__danus__search_arxiv_theorems"
    assert fact_submit_note in researcher and arxiv_note in researcher
    assert fact_submit_note not in reviewer and arxiv_note in reviewer
    assert fact_submit_note not in experimenter and arxiv_note not in experimenter


def test_scientific_tool_sets():
    assert tools_for("researcher") == [
        "gm_add", "gm_search", "fact_submit", "fact_search", "handoff_request",
        "implementation_pending_contracts", "implementation_render_task",
        "implementation_submit_contract", "experiment_protocol_get",
        "experiment_evidence_get", "workflow_submit_result", "plan_pending_changes",
        "plan_propose_change", "search_arxiv_theorems",
    ]
    assert tools_for("reviewer") == [
        "gm_add", "gm_search", "fact_search", "handoff_request",
        "implementation_pending_contracts", "implementation_review_contract",
        "experiment_protocol_get", "workflow_submit_result",
        "plan_pending_changes", "plan_review_change", "search_arxiv_theorems",
    ]
    assert tools_for("experimenter") == [
        "gm_add", "gm_search", "fact_search", "handoff_request",
        "experiment_freeze_protocol", "experiment_run",
        "experiment_protocol_get", "workflow_submit_result",
    ]
    assert "fact_submit" not in tools_for("reviewer")
    assert "fact_submit" not in tools_for("experimenter")
    assert "handoff_request" not in tools_for("main")
    assert "plan_commit_change" in tools_for("main")


def main() -> None:
    test_role_harness_selection()
    print("  [ok] test_role_harness_selection")
    with tempfile.TemporaryDirectory() as d:
        test_sync_worker_harness_switches_only_managed_links(Path(d))
    print("  [ok] test_sync_worker_harness_switches_only_managed_links")
    with tempfile.TemporaryDirectory() as d:
        test_role_specific_gateway_config(Path(d))
    print("  [ok] test_role_specific_gateway_config")
    with tempfile.TemporaryDirectory() as d:
        test_scientific_dsh_prompt_excludes_legacy_math_harness(Path(d))
    print("  [ok] test_scientific_dsh_prompt_excludes_legacy_math_harness")
    test_scientific_tool_sets()
    print("  [ok] test_scientific_tool_sets")
    print("ALL SCIENTIFIC HARNESS TESTS PASSED")


if __name__ == "__main__":
    main()
