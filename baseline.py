#!/usr/bin/env python3
"""
Capture the decisions every current scenario produces, so a later change can
be diffed against them.

    python3 baseline.py                 # writes baseline.json
    python3 baseline.py --out before.json
    python3 baseline.py --diff baseline.json    # rerun and compare

What is recorded is the *decision*, not the probabilities: verdict, selected
sources, computed classification, capability tier, and the endpoint chosen
under each network scenario. Probabilities move whenever `state` changes — a
new user field or a new catalog entry is enough — so comparing them would
report noise. Comparing decisions reports regressions.

API cost: one call per scenario, plus a second for the ones that pass the
gate. Routing is then evaluated for every network scenario from that one
call-2 answer, since congestion is not something the model is asked about.
"""

import argparse
import json
import sys

import jevdemo as J
from scenarios import CALL1_QUESTIONS, CALL2_QUESTIONS, NETWORK, SCENARIOS


def decisions_for(scenario):
    """Run one scenario end to end and return just the decisions."""
    state = scenario["state"]
    q1 = scenario.get("questions1") or CALL1_QUESTIONS
    q2 = scenario.get("questions2") or CALL2_QUESTIONS

    data1, _ = J.call_api(state, q1)
    answers1 = data1.get("answers", {})
    gate = J.gate_after_call1(state, answers1)

    record = {
        "name": scenario["name"],
        "query": state["query"],
        "access_level": state.get("user", {}).get("access_level"),
        "region": state.get("user", {}).get("region"),
        "verdict": gate["verdict"],
        "selected": sorted(gate["selected"]),
        "classification": gate["classification"],
        "capability": gate["capability"],
        "source_regions": gate["source_regions"],
        "routing": None,
        "call1_question_count": len(answers1),
    }

    if gate["verdict"] != "pass":
        return record

    data2, _ = J.call_api(state, q2)
    answers2 = data2.get("answers", {})
    record["call2_question_count"] = len(answers2)
    record["routing"] = {}
    for net in NETWORK:
        r = J.pick_endpoint(gate, answers2, net)
        record["routing"][net] = {
            "chosen": (r["chosen"] or {}).get("id"),
            "skipped": r.get("skipped", False),
            "rejected_count": len(r.get("rejected", [])),
        }
    return record


def capture():
    out = []
    for i, scenario in enumerate(SCENARIOS, 1):
        sys.stderr.write("  [%d/%d] %s ... " % (i, len(SCENARIOS), scenario["name"]))
        sys.stderr.flush()
        try:
            record = decisions_for(scenario)
            sys.stderr.write("%s\n" % record["verdict"])
        except Exception as e:
            record = {"name": scenario["name"], "error": str(e)}
            sys.stderr.write("ERROR %s\n" % e)
        out.append(record)
    return out


def diff(before, after):
    """Report decisions that changed. Probabilities are not compared."""
    by_name = {r["name"]: r for r in before}
    changed = 0
    for now in after:
        was = by_name.get(now["name"])
        if was is None:
            print("  NEW      %s" % now["name"])
            changed += 1
            continue
        for field in ("verdict", "selected", "classification", "capability"):
            if was.get(field) != now.get(field):
                print("  CHANGED  %s . %s" % (now["name"], field))
                print("             was %r" % (was.get(field),))
                print("             now %r" % (now.get(field),))
                changed += 1
        wr, nr = was.get("routing") or {}, now.get("routing") or {}
        for net in sorted(set(wr) | set(nr)):
            a = (wr.get(net) or {}).get("chosen")
            b = (nr.get(net) or {}).get("chosen")
            if a != b:
                print("  CHANGED  %s . routing[%s]  %s -> %s" % (now["name"], net, a, b))
                changed += 1
    gone = set(by_name) - {r["name"] for r in after}
    for name in sorted(gone):
        print("  REMOVED  %s" % name)
        changed += 1
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="baseline.json")
    ap.add_argument("--diff", metavar="FILE",
                    help="rerun and compare against this file instead of writing")
    args = ap.parse_args()

    J.API_KEY = J.env("JEV_API_KEY")
    if not J.API_KEY:
        sys.exit("JEV_API_KEY not found. Put it in .env next to this file, or export it.")
    ua = J.env("JEV_USER_AGENT")
    if ua:
        J.USER_AGENT = ua

    sys.stderr.write("Running %d scenarios against the live API\n" % len(SCENARIOS))
    after = capture()

    failed = [r["name"] for r in after if "error" in r]
    if failed:
        sys.stderr.write("\n%d scenario(s) failed: %s\n" % (len(failed), ", ".join(failed)))
        sys.stderr.write("Fix those before trusting the baseline.\n")

    if args.diff:
        with open(args.diff, encoding="utf-8") as fh:
            before = json.load(fh)
        print("\nDecision diff against %s" % args.diff)
        n = diff(before, after)
        print("\n%s" % ("no decisions changed" if n == 0 else "%d decision(s) changed" % n))
        sys.exit(1 if n else 0)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(after, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    sys.stderr.write("\nWrote %s\n" % args.out)

    print("%-24s %-8s %-14s %s" % ("scenario", "verdict", "classification", "endpoint (baseline)"))
    print("-" * 78)
    for r in after:
        if "error" in r:
            print("%-24s %s" % (r["name"][:24], "ERROR"))
            continue
        ep = ((r.get("routing") or {}).get("baseline") or {}).get("chosen") or "—"
        print("%-24s %-8s %-14s %s" % (r["name"][:24], r["verdict"],
                                       r["classification"], ep))


if __name__ == "__main__":
    main()
