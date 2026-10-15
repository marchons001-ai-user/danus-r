#!/usr/bin/env bash
# Operator-only. Read Git history, save redacted Gitleaks findings in gitignored
# runtime/, and print filenames/rules/counts only. Never upload the report.
set -uo pipefail

cd "$(dirname "$0")/.."
git rev-parse --is-inside-work-tree >/dev/null || exit 2
if [[ "$(git rev-parse --is-shallow-repository)" != "false" ]]; then
  echo "NOT SCANNED: shallow clone. Run 'git fetch --unshallow --all --tags'." >&2
  exit 2
fi

out="$PWD/runtime/prepublish-security"
mkdir -p "$out"
chmod 700 "$out"
report="$out/gitleaks-history-redacted.json"
rm -f "$report"

scanner=""
if command -v gitleaks >/dev/null 2>&1; then
  scanner="local"
elif command -v docker >/dev/null 2>&1; then
  scanner="docker"
else
  echo "NOT SCANNED: install official Gitleaks or Docker; repository remains PRIVATE." >&2
  echo "Official scanner: https://github.com/gitleaks/gitleaks" >&2
  exit 2
fi

echo "Scanning reachable Git history in local refs only. Scanner: $scanner"
echo "Report stays local under runtime/prepublish-security/ (gitignored)."
echo "Before release also fetch branches/tags and check PR refs/Actions logs."

set +e
if [[ "$scanner" == "local" ]]; then
  gitleaks git --no-banner --redact=100 --log-opts="--all" \
    --report-format=json --report-path="$report" .
  status=$?
else
  docker run --rm --network none \
    --user "$(id -u):$(id -g)" \
    -v "$PWD:/repo:ro" -v "$out:/result" -w /repo \
    ghcr.io/gitleaks/gitleaks:latest \
    git --no-banner --redact=100 --log-opts="--all" \
    --report-format=json --report-path=/result/gitleaks-history-redacted.json .
  status=$?
fi
set -e

if [[ "$status" != 0 && "$status" != 1 ]]; then
  echo "SCAN ERROR ($status); do NOT infer zero findings." >&2
  exit 2
fi
if [[ ! -f "$report" ]]; then
  if [[ "$status" == 0 ]]; then
    printf '[]\n' > "$report"
  else
    echo "SCAN ERROR: findings exit but no report written." >&2
    exit 2
  fi
fi

python - "$report" "$status" <<'PY'
import collections
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    findings = json.load(stream)
if not isinstance(findings, list):
    raise SystemExit("INVALID report; not cleared")
summary = collections.Counter((str(item.get("File", "?")),
                               str(item.get("RuleID", "?")))
                              for item in findings)
print("=== REDACTED FINDINGS SUMMARY ===")
print("Total:", len(findings))
for (path, rule), n in sorted(summary.items()):
    print(f"{path} | {rule} | {n}")
print("Original findings report remains PRIVATE and untracked.")
if findings:
    print("BLOCK PUBLIC RELEASE: inspect findings, revoke any actual secrets.")
else:
    print("No Gitleaks signature matches in scanned reachable local history.")
    print("NOT a guarantee: check PR refs, provider logs, PII, custom secrets.")
PY
if [[ "$status" == 1 ]]; then
  exit 1
fi
