#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"
. "$ROOT/scripts/env.sh"

PROJECT="${1:-hetero-three-smoke}"
PDIR="$ROOT/runtime/projects/$PROJECT"

if [ -e "$PDIR" ]; then
  echo "project already exists: $PDIR" >&2
  exit 2
fi

if ! bash "$ROOT/scripts/services.sh" test | grep -q 'ok   verify'; then
  echo "verifier is not ready" >&2
  exit 1
fi

cleanup() {
  "$ROOT/bin/danus" stop "$PROJECT" --force >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

"$ROOT/bin/danus" new "$PROJECT" --roles high:3 --model gpt-5.6-terra

printf 'BACKEND=chatgpt\nMODEL=gpt-5.6-sol\nREASONING_EFFORT=high\nROLE=high\nDANUS_AUTHOR=high\n' > "$PDIR/workers/high/.role"
printf 'BACKEND=chatgpt\nMODEL=gpt-5.6-terra\nREASONING_EFFORT=high\nROLE=high\nDANUS_AUTHOR=high2\n' > "$PDIR/workers/high2/.role"
printf 'BACKEND=dsh\nMODEL=deepseek-v4-flash\nREASONING_EFFORT=high\nROLE=high\nDANUS_AUTHOR=high3\n' > "$PDIR/workers/high3/.role"

"$ROOT/bin/danus" assign "$PROJECT/high" --task "Prove rigorously that for all integers a and b, if a and b are even, then a+b is even. Submit the final theorem through fact_submit."
"$ROOT/bin/danus" assign "$PROJECT/high2" --task "Prove rigorously that for all integers a and b, if a and b are even, then ab is even. Submit the final theorem through fact_submit."
"$ROOT/bin/danus" assign "$PROJECT/high3" --task "Prove rigorously that for every integer n, if n is even, then n+2 is even. Submit the final theorem through fact_submit."

DANUS_MAX_ROUNDS=1 "$ROOT/bin/danus" start "$PROJECT"

deadline=$((SECONDS + 180))
while (( SECONDS < deadline )); do
  status_json="$("$ROOT/bin/danus" status "$PROJECT" --json)"
  state="$({ printf '%s' "$status_json" | "$DANUS_PY" -c 'import json,sys; rows=json.load(sys.stdin); print("error" if any(r.get("state")=="error" for r in rows) else "done" if all((not r.get("alive")) and r.get("state")=="max_rounds" for r in rows) else "running")'; })"
  case "$state" in
    done) break ;;
    error)
      printf '%s\n' "$status_json"
      exit 1
      ;;
  esac
  sleep 2
done

status_json="$("$ROOT/bin/danus" status "$PROJECT" --json)"
if ! printf '%s' "$status_json" | "$DANUS_PY" -c 'import json,sys; rows=json.load(sys.stdin); raise SystemExit(0 if all((not r.get("alive")) and r.get("state")=="max_rounds" and r.get("last_fact_id") for r in rows) else 1)'; then
  printf '%s\n' "$status_json"
  echo "three-agent smoke did not satisfy completion/fact-id validation" >&2
  exit 1
fi

SOL_LOG="$PDIR/workers/high/logs/round_1.log"
TERRA_LOG="$PDIR/workers/high2/logs/round_1.log"
V4F_LOG="$PDIR/workers/high3/logs/round_1.log"

grep -q '^model: gpt-5.6-sol$' "$SOL_LOG"
grep -q '^provider: openai$' "$SOL_LOG"
grep -q '^model: gpt-5.6-terra$' "$TERRA_LOG"
grep -q '^provider: openai$' "$TERRA_LOG"
grep -Eq '^\[codex-dsh\].*model=deepseek-v4-flash effort=high .*gateway_role=worker$' "$V4F_LOG"

fact_count="$(find "$PDIR/fact_graph/facts" -maxdepth 1 -type f | wc -l)"
if [ "$fact_count" -ne 3 ]; then
  echo "expected 3 facts, found $fact_count" >&2
  exit 1
fi

"$ROOT/bin/danus" status "$PROJECT"
printf '\nPASS: Sol -> chatgpt, Terra -> chatgpt, V4F -> dsh; 3 verifier-accepted facts share one fact graph.\n'
trap - EXIT INT TERM
