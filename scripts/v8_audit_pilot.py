"""Read-only V8 pilot audit. No model calls, project mutation or experiment replay.

Usage: python scripts/v8_audit_pilot.py PROJECT_ID
"""
import sys
from collections import Counter

from danus.v8 import Store, project_root, verify_assets
from danus.v8_inputs import verify_inputs


def main(project):
    store = Store(project_root(project))
    s = store.state()
    events = store.read()
    tasks = list(s["tasks"].values())
    issues = list(s["review_issues"].values())
    requests = list(s["requests"].values())
    proposals = list(s["proposals"].values())
    compact = lambda text, n=750: " ".join(str(text or "").split())[:n]

    print("=== V8 PILOT HEALTH ===")
    print("Project:", project)
    print("Done:", s["done"], "HALT:", repr(s["halt"]))
    print("Events:", len(events), "Last:", events[-1]["kind"] if events else None)
    print("Tasks:", len(tasks), "By status:", dict(Counter(t["status"] for t in tasks)))
    print("By role:", dict(Counter(t["role"] for t in tasks)))
    print("Events by kind:", dict(Counter(e["kind"] for e in events)))
    for label, fn in (("Assets", lambda: verify_assets(store)),
                      ("Inputs", lambda: verify_inputs(store, s))):
        try:
            fn()
            print(label, "PASS")
        except Exception as exc:
            print(label, "FAIL", type(exc).__name__, str(exc)[:400])

    print("\n=== PROPOSALS / EXPERIMENTS ===")
    print("Proposals:", dict(Counter(p["status"] for p in proposals)))
    for p in proposals[-5:]:
        print(p["id"], p["status"], "chars", len(p["content"]),
              "checks", [x["id"] for x in p["required_checks"]])
    for t in tasks:
        if t["purpose"] == "experiment":
            print("EXPERIMENT", t["id"], t["status"], t["proposal_id"])

    print("\n=== REVIEW ISSUES / HANDOFFS ===")
    print("Issues:", dict(Counter(i["status"] for i in issues)))
    for i in issues:
        if i["status"] == "OPEN":
            print("OPEN", i["id"], compact(i.get("problem"), 500))
    print("Requests:", dict(Counter(r["status"] for r in requests)))
    for r in requests[-6:]:
        print(r["id"], r["status"], r["from_role"], "->", r["to"],
              compact(r["question"], 400))
    for t in tasks[-8:]:
        print("TASK", t["id"], t["role"], t["purpose"], t["status"])
        print(" SUMMARY", compact(t["summary"], 1000))

    print("\n=== VALIDATION / FINAL SCIENCE ===")
    v = s["latest_validation"]
    if v:
        print("Validation:", v["task_id"], v["status"], compact(v["reason"], 700))
        for x in v["checks"]:
            print("CHECK", x["id"], x["status"], compact(x.get("detail"), 280))
    else:
        print("Validation: none (normal before any experiment)")
    final = s["final_review"]
    print("Final review:", (final or {}).get("verdict"))
    if final:
        print("Final basis:", compact(final.get("summary"), 1800))
    print("Experiment seq:", s["last_experiment_seq"],
          "Interpret seq:", s["last_interpret_seq"])

    print("\n=== LAST CONTROL / VALIDATION EVENTS ===")
    for e in [e for e in events if e["kind"] in (
            "DECISION", "HALT", "FINISH", "VALIDATION")][-12:]:
        print(e["seq"], e["kind"], e.get("action", ""), e.get("status", ""),
              compact(e.get("reason") or e.get("error"), 300))

    blocked = [t["id"] for t in tasks if t["status"] == "BLOCKED"]
    open_requests = [r["id"] for r in requests if r["status"] == "OPEN"]
    open_issues = [i["id"] for i in issues if i["status"] == "OPEN"]
    print("\n=== OPERATOR GATE (NOT A SCIENTIFIC VERDICT) ===")
    if s["halt"] or blocked:
        print("STOP_AND_INVESTIGATE: HALT or BLOCKED task", blocked)
    elif v and v["status"] in ("FAILED", "INCOMPLETE"):
        print("INSPECT_VALIDATION: latest experiment has validation debt")
    elif open_requests or open_issues:
        print("INSPECT_OPEN_WORK: live question or Reviewer correction",
              "requests", len(open_requests), "issues", len(open_issues))
    elif s["done"]:
        print("FINISHED: do not launch further iterations")
    else:
        print("NO_MECHANICAL_BLOCKER: human scientific audit still required")
    print("READ-ONLY AUDIT COMPLETE")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: v8_audit_pilot.py PROJECT_ID")
    main(sys.argv[1])
