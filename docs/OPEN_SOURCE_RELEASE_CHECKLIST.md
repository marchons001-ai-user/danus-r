# Danus-R public-release checklist (operator-facing)

Status: **NOT YET CLEARED FOR PUBLIC RELEASE** (2026-10-15).
This is a checkable list, not a claim that the checks have already passed.

## A. Preserve ownership, attribution and accurate claims

- [ ] Retain upstream Apache-2.0 LICENSE and the original FrenzyMath
      copyright notices; confirm any copied assets/third-party components
      carry the relevant notices. Danus-R is an independently developed
      *derivative*, not a new authorship claim over the upstream Danus paper.
- [x] README, architecture intro, CITATION.cff and pyproject.toml now
      distinguish upstream Danus from the derivative Danus-R software.
      The public maintainer identity is collective until account transfer.
- [x] The three local studies are labeled exploratory, not general
      benchmarks or peer-reviewed scientific discoveries.
- [x] Transferred the single canonical repository to `marchons001-ai-user`;
      original owner `Marchons-AIuser` retains write access. No duplicate repo.

## B. Security and privacy: hard stop before toggling Public

- [x] Operator fetched remote branches, tags and accessible PR-head refs
      before the 2026-10-15 scan. Re-fetch and re-scan if refs change.
- [x] Operator ran official [Gitleaks](https://github.com/gitleaks/gitleaks)
      over 970 reachable Git commits, with zero signature matches.
      Re-run after history-changing operations; this is not a PII/hosted-log audit.
- [ ] Examine each finding; do not just suppress findings or publish the raw
      finding JSON. Rotate/revoke any genuine credential **before** cleaning.
- [ ] Manually inspect unusual high-entropy configuration files and Git
      histories for personally identifying paths, private research, credentials,
      session transcripts, LLM outputs, external service URLs and hardcoded
      endpoint secrets. This scan also needs to review PR descriptions/comments,
      historical GitHub Actions logs and artifacts: local file scanners do
      NOT inspect GitHub-hosted comments or workflow log bodies.
- [ ] Confirm no secrets in git-tracked env files or old commits, including
      deleted files. Current .gitignore helps *future* commits but does not
      delete any historical token. Secrets in inaccessible remote/shallow,
      orphaned or deleted refs may require additional investigation.
- [ ] Do not commit or upload the scanner's finding report. Do not send
      unredacted keys to an assistant or a public issue. Refer only to
      filenames, rule IDs and counts when requesting help.
- [ ] If leaks exist, revoke/rotate first, then carefully decide whether to
      rewrite Git history with git-filter-repo or publish a separate sanitized
      distribution repository. Rewriting history can invalidate existing PR
      refs, signatures, clones and review links. GitHub Support may be needed
      to clear cached PR references to truly sensitive bytes.
- [ ] Check large/binary tracked files, hardcoded local paths and all branches
      that will become visible. Local runtime/ is gitignored and should remain
      untracked.

- [x] Operator accepts the historical author Gmail addresses and will not
      rewrite commits solely for email privacy. This does not approve other PII.
- [ ] Review tracked `docs/environment_baseline_raw.txt` and
      `docs/environment_baseline.md`: these contain a Windows username,
      workstation details and paths, including in preserved Git history.

## C. Branches, CI and release behavior

- [x] PR #121 and 14 other stacked integrated PRs were closed as superseded
      with individual explanations following #122. PR #113's 5 extra
      commits are an intentionally rejected broader legacy evidence-reading
      design; PR #101's 7 extra commits were selectively ported, not wholesale
      merged. Branches and commits were preserved.
- [x] V8 consolidation PR #122 merged as commit `b80bf467` into default
      `main`; no squash/force push, old development branches retained.
- [x] Default `main` commit `b80bf467` passed GitHub Actions #498:
      V8 core and full-repository checks on Python 3.10 and 3.13.
      Previous migration intermittency tracked separately in open Issue #123.
- [x] CI separates offline V8 correctness from the full suite, which
      installs the official Codex sandbox and runs a sandbox probe first.
- [x] Actions #503 passed clean-install and offline-demo jobs on Python 3.10
      and 3.13 at `6848a1fb` (in addition to V8 core and full sandbox jobs).
      Real model-provider runs can incur usage costs.
- [ ] Independently exercise fresh-checkout bootstrap instructions on WSL.
- [ ] Prepare the first experimental release as `v0.1.0` (matching
      `pyproject.toml`), tag a verified commit and write bounded release notes.

## D. Documents and records

- [x] Kept curated incidents, contract documents, transparent limitations
      and a zero-model synthetic V8 gate demonstration with a regression test.
      The demo is explicitly not autonomous scientific research.
- [ ] Keep raw local conversations, personal decisions, private provider logs,
      credentials, runtime experiments and unpublished restricted datasets
      PRIVATE. Do not automatically publish every historic debug session.
- [x] Public docs distinguish locally held authoritative runtime evidence
      from the synthetic offline demo; neither claims public raw pilot data.
- [x] Added SECURITY.md, CONTRIBUTING.md and README scope limitations.
      Still enable GitHub private vulnerability reporting before Public.

## E. Actual public switch (only after A-D)

In GitHub, with the intended owner account:
Repository > Settings > General > Danger Zone > Change repository
visibility > Make public. Read GitHub's visibility consequences BEFORE
confirming. This may expose commit history, all public-accessible branches,
issues, pull-request metadata, Actions logs and old repository links.

After switching:
- [ ] Open the repository in an unauthenticated/incognito window;
      verify default README, LICENSE, links, release version and CI.
- [ ] Turn on available GitHub secret scanning/push protection and appropriate
      Actions approvals for outside fork pull requests.
- [ ] From the showcase GitHub account, add the canonical repo to pinned
      repositories if eligible; otherwise use its profile README link and
      make a genuine contribution/PR to the canonical repo.
- [ ] Watch issue reports, alerts and costs. Do not expose provider API keys
      in CI of untrusted PRs or enable privileged pull_request_target workflows
      that execute fork-controlled code.

Note: local secret scanning is **necessary but not sufficient**. Gitleaks can
miss custom secrets, GitHub Actions log leaks and personal information.
The owner makes the final visibility decision after reviewing findings.


## 2026-10-15 private-main integration

- [x] PR #122 merged into private `main` as `b80bf467`.
- [x] Main CI #498: four jobs succeeded, V8 core and full suites on
      Python 3.10 and 3.13.
- [x] 17 historical open PRs individually classified and closed; Git branches
      and history remain available for later audit/disk cleanup.
- [x] Danus-R/upstream citation metadata revised, plus explanatory architecture
      and offline-demo docs. **New docs/demo PR needs its own green CI
      before merging**; the earlier main CI does not cover these changes.
- [ ] Run an actual fresh-checkout bootstrap/smoke test and review third-party
      attribution and modified-file notices.
- [ ] Audit GitHub-hosted PR/Action logs and personal paths beyond Gitleaks.
- [x] Transferred to `marchons001-ai-user`, original account remains a writer;
      remote branches reduced to `main` and `develop`.
- [x] Actions #503 passed six jobs at `6848a1fb`; README, citation and package
      repository URL changes are prepared on `develop` for review.
- [ ] Tag the verified release after final privacy/security checks, then
      explicitly approve Public visibility.
