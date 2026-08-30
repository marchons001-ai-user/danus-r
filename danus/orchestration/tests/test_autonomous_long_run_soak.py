"""A2-8e autonomous long-run soak fixture/audit regressions. No LLM or network."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from danus.execution import layout as L


def _load_script(name: str):
    path = L.repo_root() / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_setup_creates_only_inputs_not_plan_or_assignments(tmp_path: Path):
    root = tmp_path / "projects"
    workspace = tmp_path / "workspace"
    env = os.environ.copy()
    env["DANUS_AGENTS_ROOT"] = str(root)
    proc = subprocess.run(
        [
            sys.executable,
            str(L.repo_root() / "scripts" / "a2_8e_soak_setup.py"),
            "--project",
            "P",
            "--workspace",
            str(workspace),
        ],
        cwd=L.repo_root(),
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    manifest = json.loads(proc.stdout)
    pdir = root / "P"

    assert manifest["plan_created_by_fixture"] is False
    assert manifest["assignments_created_by_fixture"] == 0
    assert not (pdir / "plan" / "current.json").exists()
    assert not (pdir / "progress" / "ledger.jsonl").exists()
    assert (pdir / "RESEARCH_BRIEF.md").is_file()
    assert (pdir / "soak" / "fixture.json").is_file()
    assert (workspace / "run_case.py").is_file()
    assert (workspace / "reduce_metrics.py").is_file()
    assert "E1 exact task text" in (pdir / "RESEARCH_BRIEF.md").read_text(encoding="utf-8")
    assert "ROLE=researcher" in (pdir / "workers" / "low" / ".role").read_text()
    assert "ROLE=reviewer" in (pdir / "workers" / "low2" / ".role").read_text()
    assert "ROLE=experimenter" in (pdir / "workers" / "low3" / ".role").read_text()


def test_repair_token_estimate_uses_terminal_assignment_report():
    audit = _load_script("a2_8e_soak_audit.py")
    repairs = [
        {
            "action": "PLAN_REPAIR",
            "assignment_id": "repair-1",
            "attempt": 1,
            "worker": "low",
            "result": "assigned",
        }
    ]
    records = [
        {
            "decisions": [
                {
                    "action": "REVIEW",
                    "assignment_id": "repair-1",
                    "reason": "completion_not_recorded",
                    "worker": "low",
                }
            ],
            "budget": {
                "context": [
                    {"worker": "low", "round_usage_tokens": 144108},
                    {"worker": "low2", "round_usage_tokens": None},
                ]
            },
        }
    ]
    assert audit._repair_token_estimates(records, repairs) == [
        {
            "assignment_id": "repair-1",
            "worker": "low",
            "attempt": 1,
            "round_usage_tokens": 144108,
        }
    ]
