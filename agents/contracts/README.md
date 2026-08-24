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
| `supervisor.md` | control plane | Danus-R target coordinator: role scheduling, drift/integrity/budget supervision, escalation, and user alignment. Heartbeat/progress runtime comes later. |
| `main_agent.md` | legacy main tier | Existing math-oriented Danus main-agent runtime contract, retained for compatibility while orchestration migrates toward `supervisor.md`. |
| `verifier.md` | analytical verification | Stateless mathematical proof verifier. It remains the analytical backend of the Scientific Verification Layer. |
| `empirical_verifier.md` | empirical verification | Stateless semantic evidence auditor. It runs only after deterministic evidence/provenance checks pass and judges claim support, scope, post-hoc status, failures/deviations, and overclaiming. |

Scientific **role is independent of model/backend/effort**. A `.role` may select
`ROLE=researcher`, `ROLE=reviewer`, or `ROLE=experimenter` while `MODEL`, `BACKEND`,
and `REASONING_EFFORT` remain separate execution choices.

## Scientific worker composition

Scientific workers receive:

1. the common `worker.md` contract;
2. one scientific role contract;
3. the project's `RESEARCH_CHARTER.md`;
4. `TASK.md`, shared memory/facts, and existing worker tools.

Where a scientific role explicitly overrides a base-worker behavior (notably the
Experimenter's approved computation), the role contract wins. Common honesty,
workspace, memory, and analytical-verification rules remain in force.

## Scientific handoffs

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
- **Researcher → Verification Layer:** `Candidate Scientific Claim` with scope,
  supporting evidence, limitations, what is not established, and
  confirmatory/exploratory status.

## A2-4 verification split

The Scientific Verification Layer has two paths:

- **analytical:** existing mathematical prechecks + `verifier.md`;
- **empirical:** deterministic Evidence Store checks first; only a clean bundle is
  passed to `empirical_verifier.md` for semantic claim/evidence auditing.

The empirical semantic verifier deliberately has no Danus MCP tool bundle. It is
not allowed to redesign experiments, search the web, or re-run computation. If a
program can decide an integrity condition exactly, the program decides it before
any LLM is invoked.

## Project policy

Every new project is scaffolded with `RESEARCH_CHARTER.md`. The Charter is the
single project-level place for durable scientific constraints: objective, success
criteria, anti-patching rules, experiment/code discipline, domain constraints, and
user decisions. Do not duplicate project-specific rules into every role prompt.

## Shared spine retained from Danus

- Local memory is private continuity.
- Global memory is shared awareness/findings, not truth.
- The Scientific Fact Graph is the verified-truth store for analytical and
  empirical facts.
- Failed paths and negative evidence are first-class research information.
- User instructions and project policy outrank agent convenience.
