# Danus-R

**Experimental, evidence-gated multi-agent scientific research harness.**

Danus-R is an **independent research extension of
[Danus](https://github.com/frenzymath/Danus)**, originally developed for
mathematical reasoning with a verifier-gated fact graph. The original Danus
authors, paper and copyright notices are acknowledged below. This repository
is not an official release of the upstream Danus project.

The newer **V8 adaptive scientific workflow** is the main focus of ongoing
development. A lightweight Supervisor chooses the next research role
(Researcher, Reviewer or Experimenter); the Host enforces task, experiment and
verification boundaries. V8 does not claim to autonomously establish scientific
truth: each conclusion is limited by its data, assumptions and evidence.

> **Status (2026-10): research prototype, not production-ready.**
> Tested local V8 flows exist, but external reproducibility, failure recovery,
> full CI, and unattended long-horizon reliability remain under evaluation.

## What V8 adds

- **Adaptive handoffs:** the Supervisor dispatches role-specific work rather
  than blindly traversing a fixed workflow. Roles can request targeted
  information; referenced complete summaries and approved protocols are
  retrieved without making every role read all raw artifacts.
- **Frozen inputs and proposals:** the operator can stage SHA-256-pinned inputs;
  an Experimenter cannot run a new experiment without a Reviewer-approved
  exact proposal and explicit executable validation checks.
- **Separate claims from evidence:** Experimenter execution, Host integrity
  / process validation, Researcher interpretation and independent Reviewer
  assessment have distinct authority. A successful command is not proof of a
  scientific conclusion.
- **Append-only audit history:** tasks, approvals, requests, validation failures,
  corrections and final verdicts are recorded. Reviewer issues have stable
  identities and may be resolved or waived only with evidence.
- **Negative outcomes and stopping:** REVISE, REJECT, INCONCLUSIVE and bounded
  negative results are legitimate. The Supervisor may FINISH once the
  independent review and Host gates allow it; it should not prolong an
  experiment merely to obtain a positive result.

### V8 execution path

```text
Operator fixes goal and (optional) input set
                  |
            Supervisor
                  |
              Researcher
        inspect / propose plan
                  |
       Reviewer of exact proposal
           APPROVE / REVISE
                  |
          Experimenter (gated)
                  |
       Host validation of artifacts
                  |
       Researcher interprets outputs
                  |
       Reviewer independently audits
                  |
        Supervisor FINISH / HALT
```

## Three exploratory autonomous research pilots

| Pilot | Question | Bounded outcome |
| --- | --- | --- |
| PFL3 steering | Can the proposed control advantage be established? | Scientific **INCONCLUSIVE**, not positive evidence |
| Fly central-complex compass | Does the assumed connectome-constrained recurrent model retain direction during dark intervals? | **ACCEPT** of a negative, model-conditional result; not a claim about living flies |
| UCI Air Quality / CO | Does 24-hour-delayed rolling residual calibration improve weekly 90% interval-coverage stability? | **ACCEPT** of an uncertain/negative comparison; superiority not established |

These are **internal exploratory runs**, not peer-reviewed benchmarks. The
original datasets, generated runtime event logs and large experimental outputs
are not published here as benchmark bundles; the table is not an independent
reproduction certificate. We retain investigation records, fixed goal
documents and testable Host code, and state explicitly what has not been
demonstrated.

For goals and design constraints, see
[the V8 documentation](docs/V8_ADAPTIVE.md) and
[the frozen pilot descriptions](docs/pilots/). Implementation tests live
under `danus/tests/`.

## Getting started (Linux / WSL)

This codebase contains both the inherited Danus mathematics workflow and the
new V8 prototype. The two are **not interchangeable**.

```bash
git clone https://github.com/Marchons-AIuser/danus-r.git
cd danus-r
bash scripts/bootstrap.sh       # toolchain/bootstrap for a fresh installation
source scripts/env.sh
"$DANUS_PY" -m danus.v8 --help
"$DANUS_PY" -m pytest -q danus/tests/test_v8_validation.py danus/tests/test_v8_adaptive.py
```

Bootstrap requirements and provider accounts vary by backend. Configure only
local, ignored `config/*.env` files from the committed `*.env.example`
templates. **Never commit real API keys, local runtime folders, or private
experiment data.** Commands involving model providers incur real usage costs.

A fresh V8 project uses the `new` command with a goal file. If it uses
external data, `stage-inputs` must run **once before any role task**, and
the data must be legally obtained and checked by the operator:

```bash
"$DANUS_PY" -m danus.v8 new my-study --goal-file goal.md
# For a data-driven study, FIRST stage operator-provided inputs:
# "$DANUS_PY" -m danus.v8 stage-inputs my-study \
#   --source-dir /path/to/input-files --dataset-label "My dataset" --files data.csv
"$DANUS_PY" -m danus.v8 run my-study --max-steps 10 --worker-timeout 1200
"$DANUS_PY" -m danus.v8 status my-study
```

A `--max-steps 10` call is a bounded initial trial, not a promise to finish
ten tasks. After inspecting the event and evidence trail, `--max-steps 0`
runs without a step cap until FINISH, HALT, a failure, or manual interruption.
It is not a guarantee of eventual convergence. There is also an optional
Textual TUI: `"$DANUS_PY" -m danus.tui my-study --v8`.

Read the [V8 operator notes](docs/V8_ADAPTIVE.md),
[trust and security scope](docs/security-and-trust.md) and
[open-source release checklist](docs/OPEN_SOURCE_RELEASE_CHECKLIST.md)
before attempting unattended work.

### Quality status

The focused V8 regression suite passed locally in WSL (102 checks recorded
on 2026-10-15). This is **not** equivalent to the entire repository test
suite passing. The repository-wide GitHub Actions job still has outstanding
failures, including unavailable Codex sandbox dependencies and legacy
interface tests. See the release checklist rather than interpreting a small
green test selection as full CI health.

## Upstream credit and licensing

Danus-R contains code and documentation derived from the original
[Danus project (FrenzyMath)](https://github.com/frenzymath/Danus),
which is distributed under **Apache License 2.0**. Original copyright and
attribution notices are retained; see [LICENSE](LICENSE),
[CITATION.cff](CITATION.cff) and the
[historical upstream README](docs/UPSTREAM_DANUS_README.md).
The original Danus paper is
[“Danus: Orchestrating Mathematical Reasoning Agents with Fact-Graph
Memory”](https://arxiv.org/abs/2607.06447). These upstream papers and
case studies **must not** be represented as newly produced Danus-R results.

Danus-R's V8 adaptations and exploratory pilot workflow are independently
developed additions. No peer-reviewed Danus-R paper or general reliability
benchmark is claimed here.

## Development and contribution policy

This repository is still being consolidated for public release. The main
branch may lag feature branches until review and merge; use a tagged release
or explicitly named branch rather than assuming a feature described here is
in every checkout. Reproducible code and curated incident analyses are
appropriate public development records; raw provider logs, credentials and
private scientific datasets are not. See
[release and contribution notes](docs/OPEN_SOURCE_RELEASE_CHECKLIST.md).

For the inherited mathematics system's original usage guide, refer to
[upstream documentation preserved here](docs/UPSTREAM_DANUS_README.md).
