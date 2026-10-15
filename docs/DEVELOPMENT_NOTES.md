# Danus-R public development notes

This is a curated technical record, **not** a copy of private model
conversations or runtime event archives. Reported results refer to limited
local runs and may be independently reproduced only when raw inputs and
frozen evidence are made available through an appropriate separate release.

## Provenance

Danus-R derives from [FrenzyMath/Danus](https://github.com/frenzymath/Danus)
under its Apache-2.0 license. Original mathematics, published papers, original
fact-graph engine and original contributor credits are not Danus-R findings.

## V8 scientific harness — October 2026

The V8 branch introduced adaptive Researcher / Reviewer / Experimenter
dispatch, Supervisor policy, explicit Reviewer approval of experiment proposals,
staged source-data hashes, host-owned validation receipts, independent
interpretation/final review, open issue IDs, and append-only events.

Known engineering lessons across three *exploratory* pilots:

| Case | Observed result | Harness learning |
| --- | --- | --- |
| PFL3 correction | INCONCLUSIVE on the proposed scientific advantage | Convergence needs stable issue identities, reuse of independent evidence and correction scope control; not a numerical iteration target. |
| FlyCX compass | Independent Reviewer ACCEPTED a bounded negative result in an assumed dynamical model | Worker 3K previews and Supervisor 400-character routing previews can lose required historical evidence; targeted exact task retrieval was implemented. Checker receipts must identify an approved script, not enforce experiment-specific replay conventions. |
| UCI Air Quality / CO | Independent Reviewer ACCEPTED a negative/uncertain comparison for weekly 90% interval-coverage stability | One original experiment was checker-INCOMPLETE when run_log stored child commands under args rather than argv. The agents correctly diagnosed it and obtained approval for a checker-only repair, but that second run was avoidable Host-format work. Generic args/argv parsing and safer parser-first diagnostic guidance were added. |

Interpretation safeguards: Host PASSED corroborates defined process/integrity
checks, not the independent scientific truth of a model. The Reviewer is an
agent role and not a substitute for community peer review. The three tests
alone cannot establish robustness under many-day unattended operations,
arbitrary scientific domains, security attacks or changing model providers.

## Verified checks versus known gaps

- The operator recorded **102 focused V8 tests passing** in local WSL on
  2026-10-15, plus a read-only recheck of all nine original UCI experiment
  Host checks as PASSED. These checks are not a full repository test pass.
- Historical UCI original checker-INCOMPLETE and later checker-only PASSED
  events remain truthful, immutable history. Read-only replay of the Host
  gate does not backdate or rewrite events.
- Full GitHub Actions remains red: the inspected Python 3.13 CI run reported
  **79 failures, 1228 passes and 15 skips**, mixing missing official Codex
  sandbox dependency with legacy interface mismatches. Failures need to be
  classified; do not label the entire repository CI healthy.
- V8 pilot-scoped evidence is held in local gitignored runtime workspaces;
  no public authoritative reproduction bundle is claimed.
- Cross-experiment validation lineage and general convergence under repeated
  failures remain open research/engineering questions.

## Public issue/PR policy

Open an issue for reproducible, bounded bugs with a minimal test and expected
behavior. Record only **redacted** error traces and non-sensitive filenames.
Work that changes a validation contract should include a failing regression
test, explicit evidence, and explain the impact on previously approved
immutable scientific protocols. Scientific proposals must remain scoped to
the original question; do not chase a positive outcome by accumulating tests.

For the full internal technical design (but not private runtime logs), see
[docs/V8_ADAPTIVE.md](V8_ADAPTIVE.md) and
[docs/OPEN_SOURCE_RELEASE_CHECKLIST.md](OPEN_SOURCE_RELEASE_CHECKLIST.md).
