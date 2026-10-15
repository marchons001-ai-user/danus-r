# Danus-R bounded scientific end-to-end acceptance pilot

## Research question

On a small synthetic autoregressive time series, can a one-step linear ridge
predictor improve held-out mean-squared prediction error over both persistence
and zero-prediction baselines by at least 5%? The answer may be **negative**.
The scientific purpose is a bounded comparison, not producing a positive
conclusion. The engineering acceptance purpose is verifying the complete
Researcher → independent Reviewer → Experimenter → Researcher INTERPRET →
independent Reviewer workflow without manual repairs.

## Immutable scientific design

- Generator: independently generate eight univariate AR(1) trajectories
  with \(x_{t+1}=0.7x_t+\epsilon_t\),
  \(\epsilon_t\sim\mathcal N(0,0.3^2)\), starting at \(x_0=0\).
  Each trajectory contains 257 samples (256 one-step prediction pairs).
  NumPy Generator PCG64, seed 42; generate all eight in a single fixed order.
- Split **by trajectory**: indices 0–3 train, 4–5 validation,
  6–7 held-out test. Do not fit, select parameters or normalize on test.
  There is no window across a trajectory boundary.
- Models: (S) persistence \(\hat x_{t+1}=x_t\);
  (Z) zero prediction \(\hat x_{t+1}=0\);
  (R) one-step ridge regression \(\hat x_{t+1}=a x_t+b\),
  fitted on the four training trajectories. Regularize slope with
  lambda=0.001, with the intercept unregularized. Do not tune lambda
  or choose among models using held-out test data.
- One seed, one fixed run, one frozen experimental unit, CPU NumPy only.
  No torch training, GPU requirement, hyperparameter sweep, additional
  trajectories, model variants, reruns after seeing metrics or online data.
- Test metric for each model is pooled one-step MSE over the test trajectories.
  Improvement against baseline B:
  \(G_B = 1 - MSE_R / MSE_B\).
  Pre-register primary_metrics:
  `mse_ridge`, `mse_persistence`, `mse_zero`,
  `gain_vs_persistence`, `gain_vs_zero`, `advance_screen`
  (six finite numeric scalars).
  `advance_screen=1.0` only if both relative gains are >=0.05,
  otherwise `0.0`. The run is still complete if the screen fails.
- Predictions and raw data are explicitly synthetic. Make no general
  application, causality or biological interpretation.

## Mandatory governance and execution route

The Researcher proposes the smallest useful Plan with independent scientific
review before execution, and a final Reviewer evaluation after an evidence-bound
Researcher interpretation. Reviewer must audit the exact Implementation
Contract for data access, splits, methods/controls, bounded resources, metrics,
decision rule and output/provenance requirements; REVISE an incomplete task.

Only the assigned Experimenter writes minimal execution code and a standalone
`reduce_metrics(protocol, artifacts)` function using the fixed Gateway ABI,
freezes a dynamic protocol with `experiment_freeze_protocol`, and calls
`experiment_run`. No manually pre-frozen IDs and no arbitrary shell
experiment bypass. The reducer returns exactly the six primary metrics from
the single verified JSON output. The experiment writes the following
mandatory nested JSON in the frozen raw artifact `raw/pilot.json`:

- `environment`: actual `python_version`, `numpy_version`, `device`
  (CPU), and `platform`;
- `resources`: actual measured nonnegative `elapsed_seconds` and
  `peak_rss_kib` on Linux, including the measurement method; if unavailable,
  record an explicit null and explanation **outside primary metrics**;
- `splits`: actual per-trajectory split IDs and sample counts;
- `training`: ridge coefficients, penalty, train size and exact one fit;
- `metrics`: the six recorded primary metrics plus per-baseline MSE;
- `diagnostics`: number of training fits, observed test pairs, explicit
  numerical sanity checks and any missing diagnostic/provenance fields.

Retain any negative, missing or failed evidence and do not claim resource
measurements without actually collecting them. Preserve required
`code_commit`, `config_ref`, `data_ref` provenance when the runtime
allows them, otherwise mark each unavailable explicitly. Do not fabricate
fields or tamper with immutable Evidence.

On completion, Experimenter MUST request an `INTERPRET` handoff containing
the actual returned Evidence ID in `evidence_refs`. The assigned Researcher
MUST call its role-gated `experiment_evidence_get(evidence_id)` using that ID,
then call it at least twice with `artifact_index` and the actual JSON
pointers `/environment`, `/resources`, `/metrics` (select as needed).
The Researcher must explicitly report whether these Gateway reads succeeded
and quote relevant nested values. Reading merely the shared Markdown report
or TASK string **does not** satisfy the engineering access check. If the
Gateway denies authorized reads, record the exact error and ESCALATE;
do not bypass the Gateway using filesystem reads as a substitute for
this permission test.

The final Reviewer must verify the pre-specified MSE/gain/screen interpretation,
confirm that Evidence IDs, reported nested JSON and provenance are aligned,
and separate scientific conclusion from technical audit findings. Reviewer
may request a narrowly scoped CLARIFY if measurements are missing, but must not
infer a positive result or require a changed experimental design.

## Operator acceptance checks (outside the scientific hypothesis)

The operator should be able to inspect the actual Experimenter Evidence ID,
a hash-checked immutable `pilot.json`, exact primary metrics, the Researcher
`experiment_evidence_get` completed call in role logs, the final independent
Reviewer verdict and automatic Supervisor exit. Preserve every failure and
OPEN Main escalation, if any; do not silently close operator obligations.
Keep the Plan shallow and avoid adding unrelated research tasks or agents.
