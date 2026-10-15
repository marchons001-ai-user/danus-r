# Unified Scientific Dispatch — v1 (transition protocol)

## Goal

One decision point for **which Assignment is bound to which worker**, regardless
of where its research intent originated. Roles decide science; Supervisor alone
allocates workers. No model-produced transition is an authorization.

## Durable flow

```text
Worker research action ───→ Plan proposal / Contract / Handoff / Evidence (authoritative)
                └────────→ optional workflow_submit_result(Worker Result JSON)
                                      ↓
                              Progress + Plan + Handoff + Contract snapshot
                                      ↓
                            Deterministic Supervisor reducer
                                      ↓
                   Stage 1: apply completed-node and governance transitions
                                      ↓
                         Build a *fresh* consistent snapshot
                                      ↓
                      Stage 2: unified authorized dispatch queue
                     (HANDOFF > CONTRACT > PLAN_NODE; hints rank ties)
                                      ↓
                one atomic ProgressLedger.assign + start, guarded by Plan/Contract
```

Bootstrap/repair is an exceptional control-plane task, not a normal Plan
leaf; it shares the same `dispatch_explicit` worker-claim operation.
Plan Review/Revision uses the same authorized candidate queue as execution,
not a separate reviewer allocation path.

## Worker Result JSON (optional, gateway-authorized)

```json
{
  "schema_version": 1,
  "event_type": "WORKER_RESULT",
  "assignment_id": "<bound ID>",
  "plan_version": 2,
  "worker": "low",
  "role": "researcher",
  "node_id": "R1",
  "outcome": "COMPLETE",
  "next_intent": {
    "kind": "REVIEW_CONTRACT",
    "subject_id": "<existing-contract-id>"
  },
  "artifact_refs": ["<existing-artifact-id>"]
}
```

Scientific workers can call `workflow_submit_result(outcome, next_intent,
subject_id, artifact_refs)` **only through their scoped gateway identity**.
After the assignment's authoritative terminal Progress state is recorded, the
trusted Runtime checks for this event and, if absent, writes one JSON Result
with `next_intent=WAIT`. It never fabricates a role choice. A previously
submitted structured Result wins if its outcome matches the terminal state.
Assignment identity, role, project and Plan version are injected by the
Gateway, not supplied by the model. Events are append-only and idempotent per
Assignment. Conflicting replay is rejected; unknown/malformed kinds and paths
are rejected. The final `DANUS_OUTCOME` line remains mandatory for terminal
transitions. The Supervisor ignores Worker Result preferences until the same
Assignment ends in the matching durable terminal state.

Allowed intents: `REVIEW_PLAN`, `REVIEW_CONTRACT`, `RUN_EXPERIMENT`,
`RUN_NODE`, `REVISE`, `INTERPRET_EVIDENCE`, `WAIT`, `FINISH`.

**Crucial**: intent priority only ranks independently validated ready actions
of the *same class*. `FINISH` never marks Plan nodes DONE; `REVIEW_CONTRACT`
never approves a contract; `RUN_EXPERIMENT` never launches an unapproved
Experimenter. The finite grammar is intentionally separate from the unbounded
scientific content of tasks and artifacts.

## Scheduler invariants

1. A worker is reserved once per reconcile pass; a node is dispatched at most
   once per pass, regardless of how many sources request it.
2. Reducer decisions are re-evaluated **after** opening/resolving Handoffs.
3. The exact Plan, Handoff and Implementation Contract version/identity is
   revalidated by `ProgressLedger.assign`. Scientific content may not bypass it.
4. Experiment contracts do not allocate reviewers independently: a unique
   frozen Reviewer predecessor (e.g. V1) is the review Assignment and E1 remains
   dependency-gated until V1 closes and its approval Handoff resolves.
5. Old Supervisor decisions may not STOP/REVIEW-pause/RETASK a rebound worker
   whose Assignment ID or Plan node differs from the decision snapshot.
6. Sandbox protected files, model network policy, Verifier and EvidenceStore
   are unchanged. Optional JSON cannot write or commit Plan/Progress directly.
7. Bootstrap repair, Research Charter and Plan governance remain human-reviewable
   and auditable. The system fails closed on ambiguous review-node ownership.

## Scope and limitation of this version

This is an **incremental single-queue rollout**, not an unrestricted LLM-led
workflow engine. Legacy Worker Outcome and Plan Proposal/Contract/Handoff calls
remain supported. Bootstrap and deterministic repair still originate in
`planning.py`, but use the shared assignment primitive. Longer term we can
move those sources onto the same event stream without changing authorization.

The new queue still relies on Python reducers to decide readiness. This is
deliberate; its finite safety rules must be tested across source combinations
rather than replaced by unverified natural-language NEXT ROLE directives.

## Acceptance steps

1. Run targeted `pytest` for Worker Result schema, isolated scientific
   lifecycle, Plan approval/revision and V1/E1 Handoff routing.
2. Only if green, create **new** A2-8e native v6 from scratch. Run
   `bin/danus research` once, without manual Plan/worker intervention.
3. Use `scripts/diagnose_autonomy_block.py` if blocked; preserve the prior
   v1–v5 projects and compare their append-only evidence.
4. Audit `clean_complete`, reviewer separation, at least two autonomous Plan
   commits and post-hoc evidence-bound revision before considering the
   work complete. Do not treat TUI RUNNING or a successful model call as
   a passing state-machine test.
