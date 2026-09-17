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
OUT_DIR = os.path.join(ROOT, "data", "results")


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

    any_data = False
    for office, races in slate.items():
        out_races = []
        d = r = other = unc = 0
        for race in races:
            rc = dict(race)
            src = live.get(rc["state"], {}).get(rc["id"])
            if src:
                rc["candidates"] = src.get("candidates", [])
                rc["reporting_pct"] = src.get("reporting_pct")
                rc["called"] = src.get("called")
                rc["leader_party"] = leader(rc["candidates"])
                any_data = True
            won = rc.get("called") or rc.get("leader_party")
            if won == "D":
                d += 1
            elif won == "R":
                r += 1
            elif won:
                other += 1
            else:
                unc += 1
            out_races.append(rc)
        out = {
            "office": office, "election": {"date": "2026-11-03", "name": "2026 General"},
            "generated_at": utc_now(),
            "updated": (utc_now() if any(rr.get("candidates") or rr.get("called") for rr in out_races) else ""),
            "note": "Live results wired from official state sources near Election Day; empty until then.",
            "summary": {"total": len(out_races), "called_dem": d, "called_rep": r, "called_other": other, "uncalled": unc},
            "races": out_races,
        }
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, office + ".json"), "w", encoding="utf-8") as f:
            json.dump(out, f, separators=(",", ":"))
        print("%-9s races=%d  D=%d R=%d other=%d uncalled=%d" % (office, len(out_races), d, r, other, unc))
    if not any_data:
        print("no wired results sources yet (slate refreshed; empty results — expected off Election Day).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
