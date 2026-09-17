#!/usr/bin/env python3
"""Update live 2026 election results (House / Senate / Governor).

Reads the canonical race slate (config/results_races.json) and writes
data/results/{senate,governor,house}.json for the results page. For each race
whose state has a wired results source in SOURCES, it fills candidates/votes/
reporting/called; races without a wired source stay "not yet reported".

Live results have no single free national API, so results are wired per state
from official Secretary-of-State election-night feeds near Election Day (the
rollout routine adds SOURCES entries). Until then this simply refreshes the
slate (empty results) — it NEVER fabricates results.

A source function: parse_<usps>() -> { race_id: {candidates:[{name,party,votes}],
reporting_pct, called} }. Register it in SOURCES[usps].

Run:  python scripts/results_update.py [--force]
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SLATE_PATH = os.path.join(ROOT, "config", "results_races.json")
CHAMBERS_PATH = os.path.join(ROOT, "config", "chambers.json")
MEASURES_PATH = os.path.join(ROOT, "config", "measures.json")
OUT_DIR = os.path.join(ROOT, "data", "results")

# SOURCES_MEASURES[usps] -> {measure_id: {yes, no, reporting, called}} (wired near election)
SOURCES_MEASURES = {}


def write_measures():
    try:
        slate = load(MEASURES_PATH).get("measures", [])
    except (OSError, ValueError):
        slate = []
    passed = failed = und = 0
    live = {}
    for usps, fn in SOURCES_MEASURES.items():
        try:
            live[usps] = fn() or {}
        except Exception:  # noqa: BLE001
            live[usps] = {}
    out = []
    for m in slate:
        r = dict(m); r["office"] = "Ballot Measure"
        src = live.get(m.get("state"), {}).get(m.get("id"))
        yes = no = None; reporting = called = None
        if src:
            yes = int(src.get("yes", 0)); no = int(src.get("no", 0))
            reporting = src.get("reporting"); called = src.get("called")
        tot = (yes or 0) + (no or 0)
        r["candidates"] = ([{"name": "Yes", "party": "Y", "votes": yes}, {"name": "No", "party": "N", "votes": no}] if src else [])
        r["reporting_pct"] = reporting
        r["called"] = called
        r["yes_pct"] = (round(100.0 * yes / tot, 1) if tot else None)
        if called == "Pass" or (tot and yes > no):
            passed += 1
        elif called == "Fail" or (tot and no > yes):
            failed += 1
        else:
            und += 1
        out.append(r)
    doc = {"office": "measures", "election": {"date": "2026-11-03", "name": "2026 General"},
           "generated_at": utc_now(), "updated": (utc_now() if any(x.get("candidates") for x in out) else ""),
           "note": "2026 statewide ballot measures; results wired near Election Day.",
           "summary": {"total": len(out), "passed": passed, "failed": failed, "undecided": und},
           "races": out}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "measures.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    print("measures   count=%d  passed=%d failed=%d undecided=%d" % (len(out), passed, failed, und))


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# --- wired per-state results sources (added near Election Day) -----------------
# SOURCES[usps] -> function() -> {race_id: {candidates, reporting_pct, called}}
SOURCES = {}


def utc_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def leader(cands):
    if not cands:
        return None
    s = sorted(cands, key=lambda c: c.get("votes", 0), reverse=True)
    tot = sum(c.get("votes", 0) for c in s)
    return (s[0].get("party", "") or "").upper()[:1] if tot else None


def main():
    slate = load(SLATE_PATH)
    # gather live data per state (once per state), tolerant of failures
    live = {}
    for usps, fn in SOURCES.items():
        try:
            live[usps] = fn() or {}
        except Exception as e:  # noqa: BLE001 - one bad state must not sink the rest
            print("  ! results source %s failed: %s" % (usps, str(e)[:60]), file=sys.stderr)
            live[usps] = {}

    try:
        chambers = load(CHAMBERS_PATH)
    except (OSError, ValueError):
        chambers = {}

    any_data = False
    for office, races in slate.items():
        out_races = []
        tally = {"dem_called": 0, "rep_called": 0, "other_called": 0,
                 "dem_lead": 0, "rep_lead": 0, "other_lead": 0, "undecided": 0}
        for race in races:
            rc = dict(race)
            src = live.get(rc["state"], {}).get(rc["id"])
            if src:
                rc["candidates"] = src.get("candidates", [])
                rc["reporting_pct"] = src.get("reporting_pct")
                rc["called"] = src.get("called")
                rc["leader_party"] = leader(rc["candidates"])
                any_data = True
            called = rc.get("called")
            lead = rc.get("leader_party") or leader(rc.get("candidates") or [])
            if called in ("D", "R"):
                tally[("dem" if called == "D" else "rep") + "_called"] += 1
            elif called:
                tally["other_called"] += 1
            elif lead == "D":
                tally["dem_lead"] += 1
            elif lead == "R":
                tally["rep_lead"] += 1
            elif lead:
                tally["other_lead"] += 1
            else:
                tally["undecided"] += 1
            out_races.append(rc)
        d = tally["dem_called"] + tally["dem_lead"]
        r = tally["rep_called"] + tally["rep_lead"]
        other = tally["other_called"] + tally["other_lead"]
        out = {
            "office": office, "election": {"date": "2026-11-03", "name": "2026 General"},
            "generated_at": utc_now(),
            "updated": (utc_now() if any(rr.get("candidates") or rr.get("called") for rr in out_races) else ""),
            "note": "Live results wired from official state sources near Election Day; empty until then.",
            "summary": {"total": len(out_races), "called_dem": tally["dem_called"], "called_rep": tally["rep_called"],
                        "called_other": tally["other_called"], "uncalled": tally["undecided"] + tally["dem_lead"] + tally["rep_lead"] + tally["other_lead"]},
            "races": out_races,
        }
        if office in chambers:
            ch = chambers[office]; nu = ch.get("not_up", {"D": 0, "R": 0, "other": 0})
            out["balance"] = {
                "total": ch.get("total"), "control": ch.get("control"), "not_up": nu,
                "dem_called": nu.get("D", 0) + tally["dem_called"], "rep_called": nu.get("R", 0) + tally["rep_called"],
                "other_called": nu.get("other", 0) + tally["other_called"],
                "dem_lead": tally["dem_lead"], "rep_lead": tally["rep_lead"], "other_lead": tally["other_lead"],
                "dem_total": nu.get("D", 0) + tally["dem_called"] + tally["dem_lead"],
                "rep_total": nu.get("R", 0) + tally["rep_called"] + tally["rep_lead"],
                "other_total": nu.get("other", 0) + tally["other_called"] + tally["other_lead"],
                "undecided": tally["undecided"],
            }
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, office + ".json"), "w", encoding="utf-8") as f:
            json.dump(out, f, separators=(",", ":"))
        print("%-9s races=%d  D=%d R=%d other=%d undecided=%d" % (office, len(out_races), d, r, other, tally["undecided"]))
    if not any_data:
        print("no wired results sources yet (slate refreshed; empty results — expected off Election Day).")
    write_measures()
    return 0


if __name__ == "__main__":
    sys.exit(main())
