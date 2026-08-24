# contracts/ — agent standing contracts

These Markdown files are standing operating contracts, not application code. They
define what each agent tier/role is responsible for, what it may rely on, and what
it must hand off. Runtime/tool permissions remain enforced separately by Danus.

## Current contracts

| File | Role/tier | Purpose |
| --- | --- | --- |
| `worker.md` | base worker | Legacy Danus mathematical worker contract: memory discipline, tools, mathematical fact verification, and common worker invariants. |
| `researcher.md` | scientific worker | Exploratory hypothesis/mechanism generation, theoretical reasoning, experiment proposals, and evidence interpretation. |
| `reviewer.md` | scientific worker | Conservative pre-code review: prior art, falsifiability, confounds, complexity, integrity, and minimum experiment design. |
| `experimenter.md` | scientific worker | Minimal implementation and faithful empirical execution; produces evidence-oriented handoffs and preserves negative/null outcomes. |
| `supervisor.md` | control plane | Danus-R target coordinator: role scheduling, drift/integrity/budget supervision, escalation, and user alignment. Defined in A2-3; heartbeat/progress runtime comes later. |
| `main_agent.md` | legacy main tier | Existing math-oriented Danus main-agent runtime contract. Kept unchanged in A2-3 for compatibility; Danus-R will migrate orchestration toward `supervisor.md` rather than rewriting it prematurely. |
| `verifier.md` | mathematical verification | Current stateless LLM proof verifier used by legacy `fact_submit`. It remains the analytical-verification backend until A2-4 extends scientific fact/evidence verification. |

Scientific **role is independent of model/backend/effort**. A `.role` may select
`ROLE=researcher`, `ROLE=reviewer`, or `ROLE=experimenter` while `MODEL`, `BACKEND`,
and `REASONING_EFFORT` remain separate execution choices.

## Composition in A2-3

Scientific workers receive:

1. the common `worker.md` contract;
2. one scientific role contract;
3. the project's `RESEARCH_CHARTER.md`;
4. `TASK.md`, shared memory/facts, and existing worker tools.

Role contracts are intentionally **semantic**, not tied to the legacy Fact Graph's
`statement + proof` storage format. They define research handoffs now; A2-4 will
define how analytical/empirical claims and evidence are represented and verified.

Where a scientific role explicitly overrides a base-worker behavior (notably the
Experimenter's approved computation, or the temporary rule not to send empirical
results through the mathematical verifier), the role contract wins. Common honesty,
workspace, memory, and existing analytical-verification rules remain in force.

## Scientific handoffs (semantic, not yet a storage schema)

- **Researcher → Reviewer:** `Research Proposal` — question, current evidence,
  hypothesis/mechanism, competing explanation, falsifier, cheapest decisive test,
  decision rule, complexity delta, value, assumptions/risks.
- **Reviewer → team:** `APPROVE / REVISE / REJECT` with blocking issues, simpler
  alternative, minimum required changes, Charter/complexity/post-hoc assessment,
  and optional non-blocking suggestions kept separate.
- **Approved plan → Experimenter:** bounded question/matrix/metrics/decision rule;
  the Experimenter does not invent missing scientific choices.
- **Experimenter → Researcher/Reviewer:** `Evidence Packet` — what ran,
  provenance, matrix coverage, primary metrics, negative/null results, anomalies,
  deterministic checks, artifact references, and unresolved issues.
- **Researcher → future Verification Layer:** `Candidate Scientific Claim` with
  scope, supporting evidence, alternatives, limitations, what is not established,
  and confirmatory/exploratory status.

A2-3 freezes these scientific meanings only. It deliberately does **not** freeze a
JSON/Markdown/Fact-Graph serialization format.

## Project policy

Every new project is scaffolded with `RESEARCH_CHARTER.md`. The Charter is the
single project-level place for durable scientific constraints: objective, success
criteria, anti-patching rules, experiment/code discipline, domain constraints, and
user decisions. Do not duplicate project-specific rules into every role prompt.

## Shared spine retained from Danus

- Local memory is private continuity.
- Global memory is shared awareness/findings, not truth.
- The Fact Graph remains the current verified-truth store.
- The legacy verifier remains the mathematical correctness gate until A2-4.
- Failed paths and negative evidence are first-class research information.
- User instructions and project policy outrank agent convenience.
