# Security policy

Danus-R is an experimental agent harness. It can execute approved Python
scripts and invoke external model providers. Neither V8 nor the inherited
Danus system is a hardened multi-tenant service.

## Reporting a potential vulnerability

Do **not** include real credentials, private datasets, personally identifying
material or unpublished exploits in a public issue.

For responsible disclosure, use the repository's
[private vulnerability reporting](https://github.com/marchons001-ai-user/danus-r/security/advisories/new)
channel (GitHub Security → Report a vulnerability). Do not publish exploit
details, credentials or private research data in a public issue. If this
channel is unavailable, contact a maintainer privately rather than posting
a public reproduction containing sensitive details.

Please include the affected version/commit, a redacted reproduction, the
relevant trust boundary, and whether the failure permits unauthorized
execution, silent modification of frozen evidence or bypass of independent
validation.

## Operational boundaries

- Use an isolated Linux/WSL environment with least-privilege credentials.
- Keep real provider keys only in local ignored config files or a managed secret
  store. Do not commit raw runtime logs, prompt transcripts or secret reports.
- Researcher/Reviewer approval and Host PASSED do not guarantee that
  Experimenter-generated scripts are safe or that scientific conclusions are
  universally valid.
- The V8 system is a prototype. Do not run untrusted scientific proposals with
  sensitive credentials or unrestricted network/device access.
- Open source distribution does not include private datasets or provider logs.

Read [docs/security-and-trust.md](docs/security-and-trust.md) and the
[release checklist](docs/OPEN_SOURCE_RELEASE_CHECKLIST.md).
