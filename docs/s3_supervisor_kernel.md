# S3 Supervisor kernel

S3 makes deterministic control ownership explicit without adding a second orchestration framework.

## S3-A: snapshot + action protocol

`danus/orchestration/control.py` is the single decision boundary:

`durable/runtime state -> build_supervisor_snapshot() -> reduce_supervisor_snapshot() -> actions`

The snapshot contains only state already used by Supervisor reconciliation:

- committed Research Plan and Plan changes;
- current/all Assignments and completed Plan nodes;
- Implementation Contracts and their replacement lineage;
- HandoffRequests, including resolved history needed for routing provenance;
- worker runtime rows;
- project pause state.

`reduce_supervisor_snapshot()` performs no filesystem or process mutation. Re-running it on the same snapshot must return the same result.

The S3 action envelope is a plain dictionary with:

`kind`, `plan_version`, `node_id`, `assignment_id`, `worker`, `reason`.

The base action kinds are:

- `ASSIGN_NODE` — a ready Plan leaf has no Assignment;
- `START_WORKER` — an existing valid Assignment has no running process;
- `STOP_WORKER` — existing deterministic stop condition;
- `REVIEW_ASSIGNMENT` — existing review condition;
- `WAIT` — no mutation is required.

For compatibility, `reconcile()` remains the public entrypoint and the legacy `decisions` field remains available while S3 is being completed.

## S3-B: deterministic scientific routing

Plan/Contract/Handoff route selection now belongs to `reduce_supervisor_snapshot()`. The reducer adds only the domain actions needed to express existing behavior:

- `OPEN_HANDOFF`;
- `REOPEN_HANDOFF`;
- `RESOLVE_HANDOFF`;
- `ASSIGN_HANDOFF`;
- `ASSIGN_CONTRACT`;
- `COMMIT_PLAN_CHANGE`.

`REVIEW`, `REVISION`, `RETHINK`, `INTERPRET`, and `ESCALATE` remain HandoffRequest kinds rather than becoming a second action taxonomy.

The reducer deterministically covers:

- Plan proposal review, approved commit, revision, and rethink routing;
- Implementation Contract review, approved Experimenter assignment, revision, and rethink routing;
- generic Assignment `NEEDS_REVIEW` routing, while preserving INTERPRET/ESCALATE suppression;
- OPEN Handoff dispatch, stale-attempt reopen/retry, resolvable completed handoffs, INTERPRET dispatch, and persistent ESCALATE-to-main surfacing.

`autonomy.py` executes Contract/Handoff actions and `planning.py` executes Plan actions. They retain task rendering, proposal validation/materialization, worker selection, and persistence operations, but no longer own the scientific route choice. Existing pre-S2 Plan recovery and bounded proposal repair remain narrow compatibility paths.

Ordinary task-file node dispatch still follows the existing legacy decision overlay so resource-budget behavior is unchanged.

## S3-C: bounded convergence

One `do_supervise(..., apply=True)` call now keeps applying immediately deterministic consequences under the existing project control lock until the semantic reducer output reaches a fixed point or eight passes have run. The limit is a fixed safety bound, not a new configuration surface.

Convergence compares semantic control state rather than file mtimes or runtime bookkeeping. The returned report is the final post-apply reducer view and includes:

`convergence = {passes, stable, max_passes, reason}`.

`applied` and `autonomy_applied` accumulate mutations across the bounded passes, while one outer Supervisor call still writes one reconciliation event.

A fixed point does not mean the research project is finished. Convergence stops cleanly when further progress requires a worker/LLM result, a main/control-plane decision such as `ESCALATE`, an available role worker, or another external state change. The Supervisor never waits inside the convergence loop for those events and does not invent scientific work to make the loop terminate.

Resource and quota policies remain overlays at the existing CLI boundary. A resource-budget stop suppresses new autonomous work during convergence; quota/backend policy is not reimplemented inside the reducer.

The S3-C invariants are enforced by focused E2E tests rather than a new runtime invariant framework: post-apply reports reflect durable state, repeated supervision does not duplicate Handoffs or Assignments, and a completed review can resolve its Handoff and expose the next deterministic assignment in one outer Supervisor call.

## Boundary

S3 does not change Plan, Contract, Handoff, Assignment, Gateway/MCP, quota/backend, or scientific verdict semantics. It adds no dependency, datastore, configuration, queue, scheduler, Supervisor LLM, Handoff kind, or orchestration service. Crash/process recovery beyond the existing durable S2 mechanisms remains a later recovery concern rather than being folded into convergence.
