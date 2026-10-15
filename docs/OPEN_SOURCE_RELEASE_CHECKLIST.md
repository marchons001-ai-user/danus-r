# Danus-R public-release checklist (operator-facing)

Status: **NOT YET CLEARED FOR PUBLIC RELEASE** (2026-10-15).
This is a checkable list, not a claim that the checks have already passed.

## A. Preserve ownership, attribution and accurate claims

- [ ] Retain upstream Apache-2.0 LICENSE and the original FrenzyMath
      copyright notices; confirm any copied assets/third-party components
      carry the relevant notices. Danus-R is an independently developed
      *derivative*, not a new authorship claim over the upstream Danus paper.
- [ ] Verify README, CITATION.cff, pyproject.toml and descriptions distinguish
      upstream mathematical facts from Danus-R V8 exploratory pilot outcomes.
- [ ] Do not call three local case studies general benchmarks, scientific
      discoveries or peer-reviewed validation.
- [ ] Decide the permanent public owner/account once; avoid duplicate canonical
      repos. A second GitHub personal account may collaborate, contribute
      visibly and pin a public repository after a qualifying contribution,
      or link it in that account's profile README.

## B. Security and privacy: hard stop before toggling Public

- [ ] Fetch all branches, tags and relevant PR refs in the private local clone.
- [ ] Run a recent official [Gitleaks](https://github.com/gitleaks/gitleaks)
      scanner across ALL REACHABLE GIT HISTORY, not only current files.
      The local script scripts/prepublish_secret_scan.sh writes a REDACTED
      report under gitignored runtime/ and prints rule/path summaries only.
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

## C. Branches, CI and release behavior

- [ ] Resolve the **draft** PR #121 and its branch ancestry. Its current base
      is an intermediate fix branch, NOT main. Do not assume opening the
      repository publicly makes V8 the code visible in default main.
- [ ] Consolidate the intended V8 changes through a reviewed PR to main,
      or consciously select a tested public default branch. Retire obsolete
      branches only when their history and outstanding PRs are understood.
- [ ] Current full GitHub Actions is **NOT GREEN**. The inspected Oct 15
      Python 3.13 job reports 79 failed, 1228 passed, 15 skipped.
      Some failures need the official Codex sandbox executable; other
      failures are legacy API/test mismatches. Do not describe the full suite
      as passing merely because the 102 focused V8 checks passed locally.
- [ ] Isolate portable/offline checks in CI and clearly label tests requiring
      unavailable sandbox/agent dependencies. Fix genuine source regressions
      before making a stability promise.
- [ ] Exercise clean-install instructions in a disposable Linux/WSL workspace.
      Confirm minimum requirements and provider usage costs; V8 execution can
      spend real API tokens even if a pilot ends early.
- [ ] Decide public version (e.g. v0.x experimental), tag a verified commit
      and write release notes that distinguish code, science, and remaining risks.

## D. Documents and records

- [ ] Keep curated open incident reviews, architecture contracts, exact
      behavior/limitations and reproducible lightweight fixtures.
- [ ] Keep raw local conversations, personal decisions, private provider logs,
      credentials, runtime experiments and unpublished restricted datasets
      PRIVATE. Do not automatically publish every historic debug session.
- [ ] Explain where authoritative local runtime evidence lives, but do not
      claim it is downloadable or independently reproduced from this repository.
- [ ] Publish a route for responsible security reporting, contributor behavior
      guidelines and clear statement of experimental support scope.

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
