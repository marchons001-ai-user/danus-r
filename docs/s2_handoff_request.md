# S2 HandoffRequest contract

`HandoffRequest` is the single durable role-to-role routing object. It is not a Research Plan node, scientific verdict, or execution state.

Flow:

`Research Plan -> Assignment -> HandoffRequest -> Assignment`

## v1 schema

Required routing fields: `handoff_id`, `kind`, `status`, `plan_version`, `node_id`, `subject_type`, `subject_id`, `requested_by`, `source_role`, `target_role`, `request`, `request_sha256`, `reason`, `attempt`, `created_at`, `updated_at`.

Optional provenance fields: `source_assignment_id`, `parent_handoff_id`, `assigned_assignment_id`, `resolution_assignment_id`, `resolution_summary`.

Kinds and fixed targets:

- `REVIEW -> reviewer`
- `REVISION -> researcher`
- `RETHINK -> researcher`
- `INTERPRET -> researcher`
- `ESCALATE -> main`

Subject types in v1: `PLAN_CHANGE`, `IMPLEMENTATION_CONTRACT`, `ASSIGNMENT`, `PLAN_NODE`.

## state machine

`OPEN -> ASSIGNED -> RESOLVED`

Recovery may do `ASSIGNED -> OPEN`; each new assignment increments `attempt`. `OPEN` or `ASSIGNED` may become `STALE` or `CANCELLED`. `RESOLVED`, `STALE`, and `CANCELLED` are terminal.

A worker crash reopens the same handoff; it must not create a new logical request. `handoff_id` is deterministic from the semantic request, not creation time.

`RESOLVED` means the routing request was handled, not that its scientific subject was approved. Plan/Contract verdicts remain in their own stores.

## S2-A boundary

S2-A adds only append-only persistence, replay, validation, idempotent open, and legal lifecycle transitions on the existing `progress/ledger.jsonl`.

## S2-B binding

`ProgressLedger.assign(..., handoff_id=...)` is the only binding path. The normal `ASSIGNED` ledger event optionally carries `handoff_id`; Handoff replay derives `OPEN -> ASSIGNED` from that same event, so binding does not require a second write.

A binding must match the handoff's plan version, node, target role, and exact request hash. Repeating the same bind while the handoff is already assigned returns the existing assignment. Recovery reopens the handoff using its current `assignment_id`; the next bind may replace that previous attempt, including on another worker, and increments `attempt` without creating a new handoff.

## S2-C Plan governance

Autonomous Plan governance uses HandoffRequest for new role-to-role work:

- `PlanChange(PROPOSED) -> REVIEW -> Reviewer Assignment`;
- `REVISE -> resolved REVIEW -> child REVISION -> Researcher Assignment`;
- `REJECT -> resolved REVIEW -> child RETHINK -> Researcher Assignment`.

The existing `ResearchPlanGraph` remains the scientific verdict store. `APPROVE` commits deterministically after the REVIEW handoff is resolved; it does not create a COMMIT handoff. REVISION/RETHINK resolve only after a valid replacement proposal is captured from their completed assignment. An invalid replacement makes that correction handoff `STALE` before the existing bounded deterministic repair path runs.

New Plan REVIEW/REVISION/RETHINK assignments use `assignment.handoff_id` as identity instead of task hashes. Task-hash lookup remains only as a compatibility fallback for pre-S2-C Plan review assignments and for the existing bounded proposal-repair loop.

## S2-D Implementation and generic review

Implementation Contracts keep their existing scientific verdict store and exact-task gate. New governance is routed durably:

- `ImplementationContract(PROPOSED) -> REVIEW -> Reviewer Assignment`;
- `APPROVE -> resolved REVIEW -> deterministic exact Contract task assignment to Experimenter`;
- `REVISE -> resolved REVIEW -> child REVISION -> Researcher Assignment`;
- `REJECT -> resolved REVIEW -> child RETHINK -> Researcher Assignment`.

Contract replacement lineage is derived from the Researcher's bound REVISION/RETHINK handoff and persisted as `replaces_contract_id`; no extra model-facing gateway argument is required. Retried submission of the same logical contract returns the existing Contract. If a Contract REVIEW handoff exists, Experimenter assignment remains fail-closed until that handoff is `RESOLVED` as well as the Contract verdict being `APPROVE`.

A non-Reviewer assignment that ends `BLOCKED + NEEDS_REVIEW` creates one durable `REVIEW` handoff with `subject_type=ASSIGNMENT` and `subject_id=source_assignment_id`, then dispatches a Reviewer. Repeated Supervisor passes reuse the same handoff and assignment recovery path. Reviewer `COMPLETE` resolves only the review request; the blocked source assignment is deliberately left unchanged because generic NEEDS_REVIEW has no universal APPROVE/REVISE/REJECT disposition. Reviewer-to-Reviewer recursion is not created in S2-D.

## S2-E upward handoff and recovery

Scientific workers get one bounded MCP verb, `handoff_request(kind, reason)`. Routing identity is derived from the authenticated role and its current Assignment; the caller cannot choose another worker, target role, Plan node, subject, or source assignment. One source Assignment may create at most one handoff of a given upward kind even if the model repeats the request with different wording.

Allowed upward requests are intentionally narrow:

- Experimenter: `INTERPRET` or `ESCALATE`;
- Researcher: `ESCALATE`;
- Reviewer: `ESCALATE`.

`INTERPRET` creates an Assignment-subject handoff to the Researcher, is dispatched through the existing Handoff -> Assignment path, and resolves when that Researcher Assignment reports `COMPLETE`. Resolution means only that interpretation was handled; it does not close or reopen the source Experimenter Assignment or mutate Plan/Contract state.

`ESCALATE` creates an Assignment-subject handoff targeted at `main`. S2-E deliberately leaves it `OPEN`: there is no synthetic main worker and no Supervisor-LLM decision in S2. The pending handoff remains visible to Supervisor reconciliation and blocks false project `COMPLETE` until a later control-plane stage resolves it.

If a source Assignment already has an `OPEN` or `ASSIGNED` `INTERPRET`/`ESCALATE`, generic `NEEDS_REVIEW` does not create a second Reviewer handoff. Process death does not reopen a logical handoff: a live Assignment is restarted by the existing `worker_not_running` path. Only a genuinely `STALE` Assignment uses the existing `ASSIGNED -> OPEN -> new Assignment` handoff recovery and increments `attempt`. Cold-start behavior is reconstructed only from persisted Plan/ledger/worker state.

Any `OPEN` or `ASSIGNED` handoff prevents project `COMPLETE`. S2 ends here: Evidence routing, generic post-review scientific disposition, ESCALATE resolution policy, and Supervisor-LLM routing belong to S3/S4 rather than the handoff layer.
