# Danus-R public release status and operator checklist

Updated: 2026-10-15. This page replaces the historical **pre-publication**
checklist; the original checklist remains available in Git history. It records
verified release facts separately from ongoing operational safeguards.

## Public distribution

- [x] Canonical repository: [marchons001-ai-user/danus-r](https://github.com/marchons001-ai-user/danus-r), publicly accessible.
- [x] Experimental [v0.1.0 pre-release](https://github.com/marchons001-ai-user/danus-r/releases/tag/v0.1.0), tag `e4b0aaf92cab7e77f4158caded5cf4a19bb978cb`.
- [x] Public `main` and `develop` were initialized from the independently
      sanitized `develop` history; neither imported old PR, issue or
      GitHub-hosted Actions records.
- [x] The old repository is retained separately as a **private** audit archive;
      do not change its visibility. Historic private logs, credentials,
      datasets and raw experiments are not release assets.
- [x] The upstream FrenzyMath/Danus authorship, Apache-2.0 license and
      derivative-project attribution are preserved in
      [LICENSE](../LICENSE), [NOTICE](../NOTICE),
      [CITATION.cff](../CITATION.cff) and the
      [upstream README](UPSTREAM_DANUS_README.md).
- [x] README and package citation point to the canonical new repository.

## Validation and scope

- [x] At release commit `e4b0aaf9`, public GitHub Actions completed six
      successful jobs: V8 core tests, full-repository sandbox suite and
      clean-install offline demonstration on Python 3.10 and 3.13.
      Check the [Actions page](https://github.com/marchons001-ai-user/danus-r/actions)
      for exact run results.
- [x] The deterministic [offline demo](OFFLINE_DEMO.md) executes without
      model-provider credentials, network access or private data.
- [x] Three pilot reports are clearly labeled exploratory. Their descriptions
      do not claim independently verified science or general benchmark scores.
- [ ] An independent operator's end-to-end WSL/bootstrap setup and an
      external real-agent replication have **not** been established by CI.
      Do not claim these are complete.
- [ ] Unattended long-duration reliability, robustness against untrusted
      programs and production-grade isolation are **not** certified.

## Privacy and security boundaries

- [x] Before publishing, the operator created a separate Git-history copy
      and ran git-filter-repo across its two included branches. Operator
      output reported zero occurrences of the specific Gmail, Windows-user
      path and workstation-name patterns targeted by the sanitizer.
- [x] Two personal machine environment snapshots were omitted from the
      public tree and sanitized history.
- [x] No old PR, issue or Actions archive was copied into the public repo.
- [x] In the original private repository, Gitleaks reported zero signature
      matches during the pre-release scan. This alone is **not** proof that
      all possible tokens, PII or custom secrets are absent.
- [ ] Continue reviewing nonstandard secrets, dataset rights, code-generated
      output and source attribution when adding files or external contributors.
- [ ] Check any future secret-scanning, Dependabot and CodeQL findings and
      remediate or document relevant results. Enabling scanning is not itself
      evidence of a clean scan.
- [ ] Verify that the repository's GitHub security controls are configured
      as intended: private vulnerability reporting, dependency alerts, secret
      scanning, push protection and least-privilege Actions permissions.

## Post-release housekeeping

- [ ] Maintain branch protection/rulesets for `main`: prevent deletion and
      force-push, and require green CI before merging future work. Configure
      according to the actual development workflow and available GitHub plan.
- [ ] Review external contributor permissions, fork-based Actions safety and
      workflow token scopes before accepting untrusted pull requests.
- [ ] Publish a useful release-description body, not just a link to an
      uploaded Markdown attachment, and keep release notes tied to tagged code.
- [ ] Fill repository About description/topics and pin the repository on
      the maintainer's GitHub profile if desired.
- [ ] Verify the site in a logged-out/private browser session after significant
      changes; keep the historical private archive inaccessible.

## Important limitations

Danus-R is experimental scientific-agent orchestration software. Passing
Host checks and automated tests does not prove the scientific correctness of
LLM conclusions, safety of arbitrary generated scripts, or reliability of
autonomous research. Use isolated hosts and least-privilege provider keys;
model-provider runs may incur charges. See [SECURITY.md](../SECURITY.md)
and [the trust model](security-and-trust.md).
