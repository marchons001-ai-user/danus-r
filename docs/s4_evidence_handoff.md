# S4-B Experimenter evidence handoff

S4-B connects the existing immutable Evidence Store to the existing upward `INTERPRET` handoff. It deliberately does not add an Evidence-specific scheduler, MCP, Handoff kind, Assignment-Evidence ledger, or scientific verdict.

## Route

`Experimenter Assignment -> EvidenceRecord -> INTERPRET Handoff -> Researcher Assignment`.

`INTERPRET` remains an `ASSIGNMENT`-subject Handoff. Optional `evidence_refs` are immutable Evidence Store ids attached to that Handoff and embedded in the exact Researcher task so the Researcher can inspect the intended records directly.

## Rules

1. `handoff_request(kind, reason, evidence_refs=None)` accepts evidence refs only for `INTERPRET`.
2. Every supplied id must already resolve through `EvidenceStore.get_record`; unknown ids fail closed before a Handoff is opened.
3. Duplicate ids are normalized while preserving order.
4. The same Assignment may still have only one Handoff of a given upward kind. Repeating an `INTERPRET` with the same refs returns the existing Handoff; different refs are rejected rather than silently mutating or duplicating the route.
5. Empty refs preserve the historical S2 Handoff identity and keep pre-evidence interpretation requests valid.
6. Evidence refs are provenance only. The gateway does not interpret metrics, promote empirical facts, modify the Plan, or rerun verification.

## Boundary

S4-B does not decide what the evidence means. Researcher interpretation and evidence-driven empirical claims or post-hoc Plan revision belong to S4-C. `ESCALATE` remains a control-plane request and does not acquire Evidence semantics in this stage.
