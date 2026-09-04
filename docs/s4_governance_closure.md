# S4-A Governance closure

S4-A intentionally adds no new runtime state machine. S2 made role handoffs durable and S3 made the deterministic Supervisor reducer the routing owner; the current production paths already satisfy the governance-closure requirement.

## Closed modern paths

New autonomous work uses one route for each governance subject:

- Plan change: `PlanChange -> REVIEW Handoff -> Reviewer Assignment -> verdict -> Handoff RESOLVED -> commit or child REVISION/RETHINK Handoff`.
- Implementation contract: `Contract -> REVIEW Handoff -> Reviewer Assignment -> verdict -> Handoff RESOLVED -> Experimenter Assignment or child REVISION/RETHINK Handoff`.
- Generic `NEEDS_REVIEW`: `source Assignment -> REVIEW Handoff -> Reviewer Assignment -> Handoff RESOLVED`; the source Assignment remains blocked because generic review has no universal scientific verdict.

The reducer decides these routes. `planning.py` and `autonomy.py` only execute reducer-authorized durable mutations and worker operations.

## Compatibility boundary

`planning.py` still contains the pre-S2 task-hash review lookup used only to recover historical projects that already created a Plan review before HandoffRequest existed. S4-A does not delete that recovery path because doing so would make old durable state unrecoverable. New autonomous Plan reviews never create that form and always create a `PLAN_CHANGE` REVIEW Handoff.

No equivalent handoff-free production path exists for new Implementation Contract review or generic `NEEDS_REVIEW` routing.

## Existing regression coverage

S4-A reuses the existing S2/S3 tests rather than duplicating them:

- `test_s2_plan_handoffs.py` covers Plan REVIEW, REVISION, RETHINK, replacement review, and no duplicate reviewer assignment.
- `test_s2_contract_and_generic_handoffs.py` covers Contract REVIEW/correction, exact Experimenter release after resolved review, replacement lineage, and generic `NEEDS_REVIEW` dedupe.
- `test_s3_supervisor_routing.py` proves those route choices come from the deterministic reducer.
- `test_s3_supervisor_convergence.py` proves the reducer/executors converge without duplicate durable mutations.

## Invariants

1. A new scientific governance route has one durable HandoffRequest identity.
2. Reviewer/Researcher work for that route is bound to the Handoff through `Assignment.handoff_id`.
3. A scientific verdict never substitutes for Handoff completion; execution gates wait for the review Handoff to resolve.
4. Repeated Supervisor passes do not create duplicate logical Handoffs or Assignments.
5. Generic `NEEDS_REVIEW` resolves the review request only; it does not silently resolve or reopen the source Assignment.

S4-B starts where this boundary ends: bind Experimenter output to immutable Evidence Records and make that evidence discoverable through the existing upward INTERPRET path.
