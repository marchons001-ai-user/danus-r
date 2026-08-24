# Research Charter

This file contains **project-level scientific policy**. It is read by Danus-R's
scientific roles every round. Edit it before substantive research begins and keep
project-specific constraints concise and explicit.

## Main research objective

Describe the single main scientific question and why it matters.

> TODO: project owner fills this in.

## Success criteria

State what evidence or result would count as meaningful completion. Distinguish
necessary from optional outcomes.

> TODO: project owner fills this in.

## Scientific integrity

- Keep established facts, observations, hypotheses, and speculation distinct.
- Preserve negative, null, failed, and inconvenient results.
- Distinguish planned/confirmatory analyses from exploratory/post-hoc analyses.
  A result-dependent change may be useful, but the same observation must not be
  presented as independent confirmation of the change it motivated.
- Do not silently change hypotheses, metrics, baselines, task/seed sets, exclusion
  rules, or decision criteria after seeing results.
- State the tested scope; do not generalize beyond it without evidence.

## Complexity discipline

Prefer a stronger core mechanism over an accumulation of local patches.
A new loss, gate, threshold, deadband, memory state, scheduler branch, hand-tuned
weight, exception case, dependency, or other mechanism must have a scientific
reason beyond improving one benchmark case.

Before adding such a mechanism:

1. state the failure or uncertainty it is meant to explain;
2. explain why existing mechanisms or a simpler known method are insufficient;
3. define a falsifiable prediction specific to the mechanism;
4. obtain Reviewer pre-code approval for nontrivial empirical changes;
5. define the relevant contrast/ablation before using new results as confirmation.

Prefer replacement or simplification over additive patching when both can explain
the evidence.

## Experiment discipline

- Use the smallest experiment matrix that can change the scientific decision.
- Prefer a cheap pilot before a large matrix when the pilot can eliminate branches.
- Do not add experiment dimensions merely because they are easy to run.
- Freeze the intended question, primary metrics, comparisons, and decision rule
  before confirmatory execution; later Danus-R stages will make this machine-
  tracked.
- Use deterministic checks instead of LLM judgment whenever a program can decide
  the condition exactly.

## Code discipline for empirical work

1. Maximize efficiency.
2. Do not write code until the required capability is confirmed missing.
3. Reuse existing code, the standard library, platform capability, and existing
   dependencies first.
4. Only then add the minimum implementation; do not add abstractions, config,
   dependencies, or future-proofing for elegance.

## Project-specific constraints

Add domain-specific rules here. Examples include limits on model/controller
complexity, required baselines, forbidden data leakage, physical constraints,
compute budgets, or terminology that must remain fixed.

> TODO: project owner fills this in.

## User decisions / overrides

Record durable user decisions that should constrain later agent sessions.

> None yet.
