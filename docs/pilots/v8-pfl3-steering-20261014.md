# Real-data-grounded V8 pilot: fly PFL3 steering law for lightweight heading control

## Scientific objective

Investigate an application-oriented but narrowly bounded research question:

**Does the published, experimentally constrained Drosophila EPG–FC2–PFL3
heading-vs-goal steering law offer a meaningful robustness or recovery
advantage over a well-calibrated conventional circular-angle feedback law
in the SAME lightweight simulated heading-control plant, when the goal
changes and heading sensing is perturbed?**

A negative, tied, or scientifically inconclusive result is valid.
Do NOT assume biological inspiration is superior. Do NOT claim that simulated
robustness has been measured in live flies or actual robots.

Scientific anchors:
- Pires et al., *Converting an allocentric goal into an egocentric steering
  signal*, Nature 626, 808–818 (2024), DOI:10.1038/s41586-023-07006-3.
- Authors' public source: https://github.com/MaimonLab/Converting_allo_goal_to_ego_steering
  Files analysis/model.py and MATLAB/ModelLAL.m. Their L/R PFL3 drive and
  LAL subtraction model uses neural tuning parameters fitted to experimental
  data. The code is a published model, not recorded neural activity.
- Small *published source-data* workbook:
  https://zenodo.org/records/10145317/files/source_data.xlsx?download=1
  Zenodo DOI:10.5281/zenodo.10145317 (source_data.xlsx is ~527 kB).
  The Zenodo 27 GB processed_data.zip is explicitly PROHIBITED.

## Host-staged offline inputs: exact file set

The operator must stage with Danus-R v8 stage-inputs **before dispatch**:

- input/source_data.xlsx (Zenodo source-data workbook; MD5
  df2a84a05aaa703fbcf558553581191f, from publication record)
- input/model.py (author's analysis/model.py, public Git blob
  501fe7c88a176c3f8ee46cb5752c212d872323ec)
- input/ModelLAL.m (author's MATLAB/ModelLAL.m, public Git blob
  9a2dd03254821a271e6444291d2b2e141fcc573b)

Read input/PROVENANCE.json and verify staged input hashes before making
scientific claims. The manifest proves byte identity with locally staged
files, not upstream dataset authentication. Cite DOI and authors correctly.

Do not download additional data or source repositories, install dependencies,
run notebooks' historical environments, or read AIS/ship trajectories.
Do not train large networks, use GPU, invoke proprietary MATLAB, or process
whole-brain connectomes. Python standard library preferred: math, csv,
json, zipfile and xml.etree.ElementTree suffice for a small XLSX schema
inspection; use existing installed packages only if already available.
Source-data workbook is *experimental figure data*, not guaranteed
time-resolved closed-loop trajectories. Inspect its actual sheets/labels
before deciding which biological assertions can be independently checked.
If a relevant sheet or metric is absent, report that limitation and do NOT
fabricate source-data time series or pretend the paper model is observed
physiology. Published model parameters are still a real literature-grounded
computational object, but their simulated application is not new in-vivo
experimental evidence.

## Research autonomy, meaningful methods, and constraints

Researcher independently selects one small defensible numerical comparison
and pre-specifies a meaningful outcome measure, avoiding a broad search
over parameters. Use the published PFL3 steering model, preserving the
actual L/R neural tuning and direction conventions, as one candidate.
Compare to at least one genuinely calibrated conventional circular-heading
error feedback law (not an intentionally weak zero-gain controller).
Identify the minimal plant model and all controller gains and saturation
limits. Keep the plant, initial states, perturbations, goals, controller
actuator budget, and calibration/evaluation partition identical; avoid
data leakage and use an appropriate held-out disturbance/goal subset.

Consider goal steps, bounded heading sensor corruption and physical
actuator saturation as applicable. Do not prescribe a large benchmark
matrix: Reviewer should demand only checks that could change the narrow
scientific conclusion. Handle wrap-around at ±180 degrees correctly.
Keep separate (i) paper's empirical neuroscience evidence,
(ii) independently reconstructed published model outputs, and
(iii) new synthetic closed-loop controller trajectories.

Runtime boundaries:
- Total new downloaded payload < 2 MiB; only the three staged files above.
- CPU-only; no new pip/conda installs; no new model training.
- Target <= 5 minutes of total numerical experiment computation and
  <= 15 MiB newly generated experiment output, excluding the staged inputs,
  event logs and Codex logs. Prefer a few hundred to a few thousand short
  scalar-control episodes, not exhaustive sweeps.
- Seed once for stochastic disturbances, if needed. Any further seeds must
  be scientifically justified, not required just to increase task length.
- Keep all new Experimenter files under workspace/pfl3_steering_v1/.
  Never overwrite registered artifacts or edit reference input files.

## Reviewer authority and stopping

Follow V8's existing dynamically assigned Researcher, Reviewer, Experimenter
roles; do NOT artificially demand multiple reviews or iterations.
The approved protocol, with its executable required_checks, goes to
Reviewer and Experimenter. The final Reviewer must compare actual results
and artifacts against the full approved protocol; record substantive
missing tests and their impact as structured REVISE issues for the
appropriate role, or explicitly justify scientifically redundant omissions
with independent evidence and logical ablation. Supervisor routes work,
it does not decide neuroscientific adequacy.

Machine checks should at least independently verify output asset integrity
and one simple arithmetic consistency check tied to an actual results CSV
(choose specific columns and report keys only after the protocol fixes
the artifact schema). A model's claimed PASS cannot replace host checks.
Scientific adequacy needs an independent Reviewer, and a generic mean
checksum alone cannot establish causal control robustness.

Deliver a runnable self-contained analysis implementation, one small CSV
containing matched per-condition metrics for BOTH controllers, report.json
with verified empirical-vs-model provenance, fair calibration and control
conditions, honest effect magnitudes/limitations, and a run log with actual
command, exit code and elapsed time. An optional small SVG visualization
may improve interpretability but is not required; figures must not substitute
numerical evidence. Avoid extra package dependencies.

Scientific stop/REVISE decisions must follow substantive problems:
mismatched data schema, model sign convention ambiguity, unequal controller
tuning/constraints, numerical failures, questionable inference, or evidence
that cannot support a claim. If an issue cannot be resolved without
additional large downloads, report it and conclude within the available
evidence (possibly INCONCLUSIVE). Do not invent data or spin extra
experiments merely to use more Supervisor steps.
