# V8 validation-gate autonomous pilot (synthetic, CPU only)

Research goal: Investigate, without assuming the outcome, whether sample median
or sample mean estimates a scalar location more accurately under contamination.

Use a fixed true scalar location of zero, 25 observations per episode, three
prespecified contamination probabilities (0, 0.05, 0.20), paired observations
for both estimators, 100 independent episodes per condition, and ONE fixed
random seed. No model training or external data. Existing standard library and
NumPy only; keep numeric execution under 120 seconds. Report squared location
error aggregated across episodes for each of the six condition/estimator cells.
Negative or inconclusive findings are legitimate.

Researcher formulates the exact falsifiable claim, identifies limitations,
and returns a standalone proposal with these exact machine-executable
required_checks bound to the approved proposal ID:

[
  {"id":"files","kind":"asset_integrity"},
  {"id":"six_cells","kind":"csv_rows",
   "path":"evidence_gate_pilot/metrics.csv","expected_rows":6},
  {"id":"mean_mse_recompute","kind":"csv_mean",
   "path":"evidence_gate_pilot/metrics.csv","column":"mse",
   "report_path":"evidence_gate_pilot/report.json",
   "report_key":"mean_mse","abs_tolerance":1e-10}
]

The experimenter's metrics.csv must have six data rows (three contamination
conditions times two estimators), with a numeric mse column. report.json must
contain a numeric mean_mse equal to the unweighted arithmetic mean of those
six cells; also report individual per-condition values. Preserve the executable
experiment source, outputs and logs as new registered artifacts. Reviewer
independently approves the exact proposed plan, and after execution checks
what the deterministic gate DID and DID NOT establish. A PASSED numeric gate
does not by itself verify the random number generator or justify a broad
scientific generalization.

Supervisor autonomously coordinates. If any approved check is missing or fails,
diagnose and arrange an independently authorized corrective or validation-only
experiment instead of declaring ordinary scientific INCONCLUSIVE. Never
overwrite prior artifacts, fake checks, or alter the fixed objective to obtain
a positive result. Finish only with the required independent final review.
