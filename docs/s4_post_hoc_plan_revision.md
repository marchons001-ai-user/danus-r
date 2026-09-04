# S4-C — Evidence-driven post-hoc Plan revision

S4-C closes the scientific route from an evidence-bound `INTERPRET` handoff to a reviewed Research Plan revision without allowing the Supervisor to interpret experimental results.

## Route

```text
Experimenter Assignment
  -> immutable EvidenceRecord(s)
  -> INTERPRET Handoff
  -> Researcher interpretation
  -> optional autonomous Plan proposal
  -> REVIEW Handoff
  -> APPROVE / REVISE / REJECT
  -> control-plane commit and exact task materialization
```

No Plan change is automatic. The Researcher decides whether the evidence changes the coarse research route. If no structural revision is justified, the INTERPRET assignment may finish after recording the bounded interpretation through existing knowledge/fact paths.

## Provenance invariants

When the Researcher's current Assignment is bound to an `INTERPRET` Handoff carrying Evidence refs:

- a structural Plan change must use the existing autonomous proposal-file handoff rather than direct `plan_propose_change`;
- the proposal must set `post_hoc=true`;
- every Evidence ref that triggered the INTERPRET request must remain in the proposal;
- additional existing Evidence refs may be included;
- all post-hoc refs are still validated by the existing `EvidenceStore` checks.

Reviewer `REVISION` and `RETHINK` corrections inherit the same requirement from the prior post-hoc PlanChange, so a replacement proposal cannot silently remove the provenance that motivated the route change.

## Deliberate exclusions

S4-C adds no evidence verdict, interpretation datastore, Handoff kind, MCP, queue, scheduler, experiment protocol generator, or automatic follow-up experiment. The deterministic Supervisor continues to route state only; scientific interpretation remains the Researcher's responsibility.
