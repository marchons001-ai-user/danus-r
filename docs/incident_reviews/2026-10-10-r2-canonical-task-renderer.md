# Incident review: R2 canonical Implementation Contract task deadlock

Date: 2026-10-10 (Asia/Shanghai)  
Project: `fly-open10m-20261009`  
Plan: v2, `R1` and `V1` completed; `R2` blocked.  
Source: preserved local `supervisor/deadlocks.jsonl`, ProgressLedger Assignments, `tasks/v2/researcher/R2.md`, `proposals/R2_route_prerequisite.md`.

## Observed, not inferred

- Independent V1 scientific review returned **APPROVE**. R2 did **not** reject the research hypothesis or ask to execute unreviewed experiments.
- R2's committed task required that the proposed Experimenter leaf use byte-for-byte `ProgressLedger.implementation_task(...)` output, **before** an autonomous Plan revision could be submitted.
- R2 correctly refused to call `implementation_submit_contract` on a nonexistent/uncommitted Experimenter node, because that endpoint mutates contract state and requires existing Plan authority.
- R2 preserved the intended five-stage submission → independent review → experiment → interpretation → final claim-audit route and its exact contract fields in `proposals/R2_route_prerequisite.md`.
- R2 was reassigned multiple times with the same frozen task hash `6ac572b11dc643cbac79499a5a591d16340688cc228f14b62acabd5a5182da18`, but remained blocked on the same unmet runtime interface prerequisite.
- Successful Watchdog `result=assigned` meant only that a new Assignment started; it did not establish any scientific progress.

## Causal defect

`ProgressLedger.implementation_task` was already the single canonical serializer used by
`implementation_submit_contract`. However, the Gateway had no Researcher-visible,
side-effect-free method to call that serializer before proposing the Experimenter node.

This created a control-plane circular dependency:

1. Plan proposal requires exact Experimenter task text.
2. Existing canonical task was only obtainable by submitting a contract.
3. Submitting a contract requires the Experimenter node already exist in committed Plan.
4. R2 cannot add that node without the exact task text.

This is a **missing authorized read interface**, not a scientific ambiguity.

## Patch and preserved invariants

Branch: `fix/canonical-task-renderer-r2-20261010`.

- New Researcher-only Gateway tool `implementation_render_task` delegates to the original
  `ProgressLedger.implementation_task` and returns only `task` and `task_sha256`.
- Tool does not write to Plan, ProgressLedger, contracts, experiments, or evidence.
- Bound worker calls still require the current authorized Assignment/capability.
- Reviewer, Experimenter, main and verifier tool surfaces do not expose it.
- Plan reviewer approval, Implementation Contract reviewer APPROVE, exact canonical hash and dynamic-protocol authorization stay unchanged.
- Researcher bootstrap and role instructions explicitly explain when to use the tool.

## Required local validation before live recovery

1. `python -m pytest -q danus/gateway/tests/test_semantic_alignment.py`.
2. Verify the new test renders the exact task **before E1 exists** and that submitting identical fields after a reviewed Plan commit produces the same task hash.
3. Verify the previously issued worker capability cannot preview a task after its Assignment was replaced.
4. Deliberately retry R2 once under the updated Gateway; do not auto-repeat indefinitely without changed prerequisites.
5. Confirm a valid autonomous Plan proposal is written and then passes independent Reviewer approval before any experiment.
6. Record the recovery action and eventual Plan progress in the existing incident journal.

The patch was authored via GitHub; local WSL tests and real-project resume must be run separately. This document does **not** claim the project was restarted or that R2 completed.
