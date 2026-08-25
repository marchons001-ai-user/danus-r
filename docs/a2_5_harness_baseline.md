# A2-5 Scientific Harness Baseline

Baseline for A2-5 role-specific Harness / Skills / Tools slimming.

Source baseline: `main` at `ef36f806b10ffa6f02f8ebb80b4d65f90daa8487` before A2-5 behavior changes. Measurements are deterministic byte counts; they are not model token estimates.

## Current role payload

| role | common contract B | role contract B | charter B | skills | skill B | gateway role | tools | Codex explicit prompt B | DSH injected prompt B |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| worker | 18967 | 0 | 0 | 9 | 37476 | worker | 5 | 881 | 58099 |
| researcher | 18967 | 7340 | 3233 | 9 | 37476 | worker | 5 | 11835 | 69053 |
| reviewer | 18967 | 7546 | 3233 | 9 | 37476 | worker | 5 | 12037 | 69255 |
| experimenter | 18967 | 8632 | 3233 | 9 | 37476 | worker | 5 | 13131 | 70349 |

`Codex explicit prompt B` is the string handed to the Codex adapter. Codex may additionally discover its working-directory `AGENTS.md` and skills itself, so this column is not a complete model-context measurement.

`DSH injected prompt B` is stronger evidence of static overhead: the DSH adapter explicitly concatenates the kickoff prompt, `worker.md`, every worker `SKILL.md`, and the tool-name note into the prompt.

## Main finding

Every scientific role currently inherits the same mathematical harness before its own scientific contract is added:

- `worker.md`: 18,967 B
- nine mathematical `SKILL.md` files: 37,476 B
- inherited mathematical contract + skill payload: **56,443 B**
- all three scientific roles are still exposed through gateway role `worker`, therefore each sees the same five worker MCP tools.

This is the A2-5 target. The goal is not to delete legacy mathematical Danus behavior; it is to stop Researcher, Reviewer, and Experimenter from inheriting it by default.

## Worker skill inventory

| skill | SKILL.md bytes |
| --- | ---: |
| construct-counterexamples | 3157 |
| construct-toy-examples | 2163 |
| direct-proving | 4737 |
| identify-key-failures | 2398 |
| obtain-immediate-conclusions | 2345 |
| propose-subgoal-decomposition-plans | 2320 |
| query-memory | 3153 |
| search-math-results | 12009 |
| verify-proof | 5194 |
| **Total** | **37476** |

## Reproduction

Run from the repository root:

```bash
source scripts/env.sh && "$DANUS_PY" scripts/audit_scientific_harness.py
```

The audit uses only the Python standard library and repository code. It does not start an agent, call a model, access the network, or modify a project.

After A2-5 slimming, run the same audit again and compare the role payloads before any behavior/token smoke is attempted.
