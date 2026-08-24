# contracts/ — agent standing contracts

These Markdown files are standing operating contracts, not application code. They
define what each agent tier/role is responsible for, what it may rely on, and what
it must hand off. Runtime/tool permissions remain enforced separately by Danus.

## Current contracts

| File | Role/tier | Purpose |
| --- | --- | --- |
| `worker.md` | base worker | Legacy Danus mathematical worker contract plus the shared worker honesty/memory/workspace spine. |
| `researcher.md` | scientific worker | Exploratory hypothesis/mechanism generation, theoretical reasoning, experiment proposals, evidence interpretation, and bounded candidate-claim promotion. |
| `reviewer.md` | scientific worker | Conservative pre-code review: prior art, falsifiability, confounds, complexity, integrity, and minimum experiment design. |
| `experimenter.md` | scientific worker | Minimal implementation and faithful empirical execution; produces evidence-oriented handoffs and preserves negative/null outcomes. |
| `supervisor.md` | control plane | Danus-R target coordinator: role scheduling, drift/integrity/budget supervision, escalation, and user alignment. Runtime heartbeat/progress comes later. |
| `main_agent.md` | legacy main tier | Existing math-oriented Danus main-agent runtime contract, kept for compatibility while orchestration migrates toward Supervisor. |
| `verifier.md` | analytical verification | Stateless LLM proof verifier used by the analytical branch of the unified scientific verification gate. |
| `empirical_verifier.md` | empirical verification | Stateless semantic evidence auditor called only after deterministic empirical checks pass. |

Scientific **role is independent of model/backend/effort**. A `.role` may select
`ROLE=researcher`, `ROLE=reviewer`, or `ROLE=experimenter` while `MODEL`, `BACKEND`,
and `REASONING_EFFORT` remain separate execution choices.

## Composition

Scientific workers receive:

1. the common `worker.md` contract;
2. one scientific role contract;
3. the project's `RESEARCH_CHARTER.md`;
4. `TASK.md`, shared memory/facts, and existing worker tools.

Where a scientific role explicitly overrides a base-worker behavior (notably the
Experimenter's approved computation or who is responsible for empirical claim
promotion), the role contract wins. Common honesty, workspace, memory, and
analytical-verification rules remain in force.

## Scientific handoffs

- **Researcher → Reviewer:** `Research Proposal` — question, current evidence,
  hypothesis/mechanism, competing explanation, falsifier, cheapest decisive test,
  decision rule, complexity delta, value, assumptions/risks.
- **Reviewer → team:** `APPROVE / REVISE / REJECT` with blocking issues, simpler
  alternative, minimum required changes, Charter/complexity/post-hoc assessment,
  and optional non-blocking suggestions kept separate.
- **Approved plan → Experimenter:** bounded question/matrix/metrics/decision rule;
  the Experimenter does not invent missing scientific choices.
- **Experimenter → Researcher/Reviewer:** `Evidence Packet` plus immutable Evidence
  Store protocol/record ids and artifact references.
- **Researcher → Verification Layer:** `Candidate Scientific Claim`. Analytical
  claims use `statement + proof`; empirical claims use explicit `scope` and
  immutable `evidence_refs`. The Experimenter normally does not promote empirical
  claims directly.

## Scientific verification and promotion

The unified `fact_submit` tool is the sole Fact Graph write gate:

- **analytical:** existing mathematical prechecks + `verifier.md`;
- **empirical:** deterministic Evidence Store checks first; only a clean bundle is
  passed to `empirical_verifier.md` for semantic claim/evidence auditing.

The empirical semantic verifier deliberately has no Danus MCP tool bundle. It is
not allowed to redesign experiments, search the web, or re-run computation. If a
program can decide an integrity condition exactly, the program decides it before
any LLM is invoked.

A successful graph write can back-link the source Global Memory finding as
`verified`. A rejected verification does not automatically mark that finding
`refuted`: inadequate support is not evidence that the scientific claim is false.

## Project policy

Every new project is scaffolded with `RESEARCH_CHARTER.md`. The Charter is the
single project-level place for durable scientific constraints: objective, success
criteria, anti-patching rules, experiment/code discipline, domain constraints, and
user decisions. Do not duplicate project-specific rules into every role prompt.

## Shared spine retained from Danus

- Local memory is private continuity.
- Global memory is shared awareness/findings, not truth.
- One Scientific Fact Graph stores verified analytical + empirical facts.
- Evidence Store preserves frozen protocols and empirical provenance separately
  from truth claims.
- Failed paths and negative evidence are first-class research information.
- Deterministic checks precede LLM verification whenever a program can decide the
  condition reliably.
- User instructions and project policy outrank agent convenience.
