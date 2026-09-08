#!/usr/bin/env python3
"""Create the minimal deterministic A2-7p pre-pilot integration-soak fixture.

This is test setup only. It reuses Danus-R's existing project, Plan, Evidence and
Progress APIs and does not start workers, contact a model, or change production
control behavior.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from danus.core import EvidenceStore, ProgressLedger, ResearchPlanGraph
from danus.execution import layout as L
from danus.execution.scaffold import atomic_write, do_new

_SIGMAS_E1 = (0.01, 0.05, 0.10, 0.20, 0.40)

_RUN_CASE = r'''#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: run_case.py SIGMA OUTPUT_JSON")
    sigma = float(sys.argv[1])
    output = Path(sys.argv[2])
    n = 4000
    width = 5
    seed = 20260829 + int(round(sigma * 1000))
    rng = random.Random(seed)

    clean = [math.sin(0.08 * t) + 0.3 * math.sin(0.017 * t) for t in range(n + 1)]
    observed = [x + rng.gauss(0.0, sigma) for x in clean]

    raw_sq = 0.0
    filtered_sq = 0.0
    count = 0
    for i in range(width - 1, n):
        target = clean[i + 1]
        raw = observed[i]
        filtered = sum(observed[i - width + 1:i + 1]) / width
        raw_sq += (raw - target) ** 2
        filtered_sq += (filtered - target) ** 2
        count += 1

    raw_rmse = math.sqrt(raw_sq / count)
    filtered_rmse = math.sqrt(filtered_sq / count)
    payload = {
        "sigma": sigma,
        "seed": seed,
        "window": width,
        "raw_rmse": raw_rmse,
        "filtered_rmse": filtered_rmse,
        "filtered_minus_raw": filtered_rmse - raw_rmse,
        "improved": filtered_rmse < raw_rmse,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

_REDUCER = r'''from __future__ import annotations

import json
from pathlib import Path


def reduce_metrics(protocol: dict, artifacts: list[Path]) -> dict[str, float]:
    rows = [json.loads(path.read_text(encoding="utf-8")) for path in artifacts]
    deltas = [float(row["filtered_minus_raw"]) for row in rows]
    return {
        "improved_cells": float(sum(bool(row["improved"]) for row in rows)),
        "max_filtered_minus_raw": max(deltas),
        "min_filtered_minus_raw": min(deltas),
    }
'''

_CHARTER = """# Research Charter — A2-7p Integration Soak

This project exists to test Danus-R integration behavior, not to establish a new
scientific result. The scientific question is deliberately small and deterministic.

- Do not browse the web or expand the literature search.
- Reuse the supplied fixture. Do not install dependencies or modify fixture code.
- Preserve every planned cell and every negative/null result.
- Do not tune the five-sample moving-average window after seeing results.
- Do not add experiment cells except through the reviewed Plan revision specified by the task.
- Treat the missing optional_input.csv as an expected, non-blocking absence; inspect it at most once.
- Use the existing Evidence Store, Experiment Runtime, Progress Ledger and Plan-change tools.
- Result-conditioned structural change must be marked post-hoc and cite the motivating Evidence Record.
- If control state, protocol identity, evidence, or an approved task is inconsistent, stop and surface it rather than repairing around it.
"""


def _role(project: str, worker: str, role: str, *, backend: str = "") -> None:
    lines = [
        "MODEL=gpt-5.6-terra",
        "REASONING_EFFORT=low",
        f"ROLE={role}",
        f"DANUS_AUTHOR={worker}",
    ]
    if backend:
        lines.append(f"BACKEND={backend}")
    atomic_write(L.worker_dir(project, worker) / L.ROLE_FILE, "\n".join(lines) + "\n")


def _initial_plan(project_dir: Path) -> ResearchPlanGraph:
    graph = ResearchPlanGraph.create(
        project_dir,
        node_id="P0",
        title="Pre-pilot soak",
        summary="Exercise one falsification, review, revision, and follow-up evidence loop.",
    )
    operations = [
        {"op": "add_node", "node_id": "R1", "title": "Initial hypothesis", "summary": "Frame the universal filtering hypothesis and submit the exact E1 contract.", "parent": "P0", "depends_on": []},
        {"op": "add_node", "node_id": "V1", "title": "Pre-code review", "summary": "Review the E1 implementation contract before experiment execution.", "parent": "P0", "depends_on": ["R1"]},
        {"op": "add_node", "node_id": "E1", "title": "Falsification experiment", "summary": "Execute the frozen five-cell universal-improvement protocol.", "parent": "P0", "depends_on": ["V1"]},
        {"op": "add_node", "node_id": "V2", "title": "Evidence audit", "summary": "Audit E1 evidence and the universal hypothesis without changing Plan.", "parent": "P0", "depends_on": ["E1"]},
        {"op": "add_node", "node_id": "R2", "title": "Evidence response", "summary": "Respond to E1 evidence and propose only a necessary post-hoc Plan change.", "parent": "P0", "depends_on": ["V2"]},
        {"op": "add_node", "node_id": "V3", "title": "Revision review", "summary": "Review the Researcher's result-conditioned Plan proposal.", "parent": "P0", "depends_on": ["R2"]},
    ]
    change_id = graph.propose_change(
        base_plan_version=1,
        proposer="soak-fixture",
        reason="Create the fixed initial integration-soak route.",
        operations=operations,
    )
    graph.review_change(
        change_id,
        reviewer="soak-fixture",
        approve=True,
        rationale="Fixture setup only; this establishes the test route before any result exists.",
    )
    graph.commit_change(change_id, committer="soak-fixture")
    return graph


def _write_tasks(project: str, project_dir: Path, protocol_id: str) -> dict[str, str]:
    task_dir = project_dir / "soak" / "tasks"
    task_dir.mkdir(parents=True, exist_ok=True)

    goal = (
        f"Execute frozen protocol {protocol_id} through the existing Danus Experiment Runtime "
        f"for control project {project_dir}, and return its immutable evidence without modifying the fixture.\nEXPERIMENT_PROTOCOL_ID={protocol_id}"
    )
    preserve = [
        f"Run exactly protocol {protocol_id}; use only its five sigma cells and hash-pinned reducer.",
        "Use raw-current persistence as baseline and the five-sample trailing moving average as treatment.",
        "Use the deterministic seeds and existing run_case.py/reduce_metrics.py unchanged; retain every cell.",
    ]
    forbidden = [
        "Do not tune the window, add or remove cells, modify fixture code, install dependencies, or drop unfavorable results.",
        "Do not replace RMSE or reinterpret the universal-improvement hypothesis after seeing results.",
    ]
    acceptance = [
        f"Call Gateway experiment_run(protocol_id={protocol_id}) successfully.",
        "The Experiment Runtime checkpoint records all five completed units and one immutable Evidence Record.",
        "Report the evidence_id, primary metrics, full matrix coverage, failures, and any negative cells without making a broader claim.",
    ]
    e1_task = ProgressLedger.implementation_task(
        goal=goal,
        must_preserve_invariants=preserve,
        forbidden_behaviors=forbidden,
        acceptance_conditions=acceptance,
    )

    tasks = {
        "R1": f"""A2-7p soak node R1. Do not browse externally and do not run the experiment.

Research question: on the supplied deterministic signal, does a five-sample trailing moving average reduce one-step prediction RMSE versus raw-current persistence at every frozen noise level in protocol {protocol_id}?

First, deliberately attempt to read `workspace/optional_input.csv` exactly once. Its absence is expected and non-blocking. If it is absent, record that once and never retry or create it.

Inspect the frozen protocol and supplied fixture, then write a concise Research Proposal to shared memory. Submit exactly one implementation contract for node E1 at the current plan version using these exact fields:

Goal:
{goal}

Must preserve:
- {preserve[0]}
- {preserve[1]}
- {preserve[2]}

Forbidden:
- {forbidden[0]}
- {forbidden[1]}

Acceptance:
- {acceptance[0]}
- {acceptance[1]}
- {acceptance[2]}

Do not execute E1. Finish with DANUS_OUTCOME: COMPLETE only after the proposal and contract are persisted.
""",
        "V1": """A2-7p soak node V1. Perform the independent pre-code review only.

Read the R1 proposal and `implementation_pending_contracts`. There should be exactly one current contract for E1. APPROVE it only if it preserves the frozen five-cell protocol, existing fixture, RMSE metric, all outcomes, and prohibits tuning/expansion. Any material mismatch must be REVISE or REJECT rather than silently fixed. Persist a concise review finding, then finish with DANUS_OUTCOME: COMPLETE. Do not run E1.
""",
        "E1_APPROVED_TASK": e1_task,
        "V2": f"""A2-7p soak node V2. Audit the completed E1 evidence for protocol {protocol_id}.

Read the Experiment Runtime checkpoint, immutable Evidence Record, and raw five-cell artifacts. Mechanically verify coverage and the sign of filtered_rmse - raw_rmse for every cell. Decide whether the pre-specified universal-improvement hypothesis survived. Persist the evidence_id and a concise audit finding in shared memory. Do not alter the Research Plan and do not invent a replacement hypothesis. Finish with DANUS_OUTCOME: COMPLETE.
""",
        "R2": """A2-7p soak node R2. Respond to the audited E1 evidence without running experiments.

Read V2 and the E1 Evidence Record. If the universal-improvement hypothesis was falsified, propose exactly one minimal post-hoc Plan change against the current plan version. Use the E1 evidence_id as evidence_refs and add exactly these nodes under P0:

1. E2 — title `Boundary follow-up`; summary `Localize the noise crossover with the existing fixture.`; depends_on [`V3`].
2. V4 — title `Final evidence audit`; summary `Audit the bounded interpretation against both evidence records.`; depends_on [`E2`].
3. R3 — title `Final bounded claim`; summary `Form the final scoped claim without adding more experiments.`; depends_on [`V4`].

Reason: the result-conditioned follow-up is needed only to localize the observed sign change; it must not broaden the matrix beyond that purpose. Mark the proposal post_hoc=true. If E1 unexpectedly supports the universal claim or evidence is inconsistent, do not force this revision; finish NEEDS_REVIEW instead. Otherwise persist the proposal and finish COMPLETE.
""",
        "V3": """A2-7p soak node V3. Review the one pending post-hoc Plan change.

Approve only if it cites the E1 Evidence Record, is based on the current plan version, and adds exactly E2 -> V4 -> R3 with E2 depending on V3, without unrelated nodes or changed scientific machinery. Reject or request revision for expansion, missing evidence provenance, or altered conclusions. Do not commit the change yourself. Persist the review outcome and finish COMPLETE.
""",
        "E2": f"""A2-7p soak node E2. This is a routine approved follow-up that must reuse the existing fixture; do not modify code or install anything.

Using EvidenceStore, freeze one new post-hoc protocol with parent_protocol_id={protocol_id}, revision_reason=`Localize the E1 sign change with the reviewer-approved boundary matrix.`, the same question/baseline/treatment/RMSE reducer, and exactly sigma values 0.14, 0.16, 0.18, 0.20, 0.22. Pin the existing run_case.py and reduce_metrics.py by content reference. Use one unit per sigma and retain all outputs. Run the new protocol once through `python -m danus.execution.experiment`, record its protocol_id/evidence_id and full matrix coverage in shared memory, and report direct observations only. Do not tune the window or add cells. Finish COMPLETE.
""",
        "V4": """A2-7p soak node V4. Independently audit E1 and E2 evidence.

Verify both Evidence Records and raw artifacts, confirm that E2 used exactly the reviewed boundary matrix and unchanged fixture, and state the narrowest result supported by both runs. Preserve negative cells and distinguish observation from a general mechanism claim. Persist the audit with both evidence_ids. Do not request more experiments unless evidence is internally inconsistent. Finish COMPLETE when the bounded interpretation is reviewable.
""",
        "R3": """A2-7p soak node R3. Form the final bounded scientific handoff from the reviewed E1 and E2 evidence.

Use both evidence_ids and V4's audit. State only the scope actually tested, identify where filtering helped versus hurt, and explicitly state what is not established outside this deterministic fixture. Persist the bounded claim and evidence links in shared memory. Do not run another experiment, do not propose another Plan change, and do not call fact_submit in this soak fixture. Finish COMPLETE.
""",
    }
    paths: dict[str, str] = {}
    for name, text in tasks.items():
        path = task_dir / f"{name}.md"
        atomic_write(path, text.rstrip() + "\n")
        paths[name] = str(path)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the A2-7p pre-pilot soak fixture")
    parser.add_argument("--project", default="a2-7p-soak")
    parser.add_argument("--workspace", default=None)
    args = parser.parse_args()

    project = args.project.strip()
    if not project:
        raise SystemExit("project name is empty")
    project_dir = L.project_dir(project)
    workspace = (
        Path(args.workspace).expanduser().resolve()
        if args.workspace
        else (L.repo_root() / "runtime" / "soak" / project).resolve()
    )
    if project_dir.exists():
        raise SystemExit(f"project already exists: {project_dir}")
    if workspace.exists():
        raise SystemExit(f"workspace already exists: {workspace}")

    workspace.mkdir(parents=True)
    run_case = workspace / "run_case.py"
    reducer = workspace / "reduce_metrics.py"
    atomic_write(run_case, _RUN_CASE)
    atomic_write(reducer, _REDUCER)

    do_new(project, roles="low:3", model="gpt-5.6-terra", workspace=str(workspace))
    _role(project, "low", "researcher")
    _role(project, "low2", "reviewer", backend="auto")
    _role(project, "low3", "experimenter")
    atomic_write(L.research_charter(project), _CHARTER)

    graph = _initial_plan(project_dir)
    store = EvidenceStore(project_dir)
    runner_ref = {**store.artifact_ref(run_case), "path": "run_case.py"}
    reducer_ref = {**store.artifact_ref(reducer), "path": "reduce_metrics.py"}
    units = []
    for sigma in _SIGMAS_E1:
        tag = f"{int(round(sigma * 100)):02d}"
        artifact = f"runs/e1_sigma_{tag}.json"
        units.append({
            "id": f"sigma-{tag}",
            "argv": [sys.executable, "run_case.py", f"{sigma:.2f}", artifact],
            "artifacts": [artifact],
            "inputs": [runner_ref],
        })
    protocol_id = store.freeze_protocol(
        question="Does five-sample trailing averaging improve one-step RMSE at every frozen noise level?",
        hypothesis="The five-sample trailing moving average has lower RMSE than raw-current persistence in every E1 cell.",
        matrix={"units": units},
        primary_metrics=["improved_cells", "max_filtered_minus_raw", "min_filtered_minus_raw"],
        decision_rule="Accept universal improvement only if all five cells have filtered_rmse < raw_rmse.",
        author="soak-fixture",
        metric_reducer={**reducer_ref, "function": "reduce_metrics"},
        execution_files=["run_case.py"], source_root=workspace,
    )
    tasks = _write_tasks(project, project_dir, protocol_id)

    manifest = {
        "project": project,
        "project_dir": str(project_dir),
        "workspace": str(workspace),
        "plan_version": graph.snapshot()["plan_version"],
        "e1_protocol_id": protocol_id,
        "workers": {"researcher": "low", "reviewer": "low2", "experimenter": "low3"},
        "tasks": tasks,
    }
    manifest_path = project_dir / "soak" / "fixture.json"
    atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
