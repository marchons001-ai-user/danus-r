# Security policy

Danus-R is an experimental agent harness. It can execute approved Python
scripts and invoke external model providers. Neither V8 nor the inherited
Danus system is a hardened multi-tenant service.

## Reporting a potential vulnerability

Do **not** include real credentials, private datasets, personally identifying
material or unpublished exploits in a public issue.

Repository maintainers should enable **GitHub Private Vulnerability Reporting**
in the repository Security settings before public release. Once enabled, use
the repository's "Report a vulnerability" entry for responsible disclosure.
Until that channel is enabled, do not publish exploit details or credentials
in a public issue.

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
