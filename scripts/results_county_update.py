#!/usr/bin/env python3
"""County-level 2026 result margins, for the turnout-vs-result comparison.

Writes data/<st>/results_county.json for every state with a 2026 Governor or
U.S. Senate race (from config/results_races.json). Each file:
  {state, updated, race_types:[...], counties:{County:{gov:{rep,dem,other,margin,reporting},
   sen:{...}}}}
where margin = R% - D% of the actual votes (positive = Republican). The turnout
app overlays this against each county's partisan TURNOUT margin to show which
counties over/under-performed their registration (the original 2028-reference goal).

County results are wired per state from official SoS county-results feeds near
Election Day (SOURCES registry, stubs now — there is no free national API). Until
then this writes empty files (no fabrication).

A source: parse_<usps>() -> {County Name: {"gov": {rep,dem,other,reporting}, "sen": {...}}}
(votes, not margins; margin is computed here). Register in SOURCES[usps].

Run:  python scripts/results_county_update.py [--force]
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SLATE_PATH = os.path.join(ROOT, "config", "results_races.json")


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# SOURCES[usps] -> function() -> {County: {race_type: {rep,dem,other,reporting}}}
SOURCES = {}


def margin(rep, dem, other=0):
    tot = (rep or 0) + (dem or 0) + (other or 0)
    return round(100.0 * (rep - dem) / tot, 2) if tot else None


def main():
    slate = load(SLATE_PATH)
    # which race types each state has
    types = {}
    for office, key in (("senate", "sen"), ("governor", "gov")):
        for r in slate.get(office, []):
            types.setdefault(r["state"], set()).add(key)

    any_data = False
    wrote = 0
    for usps, race_types in types.items():
        live = {}
        fn = SOURCES.get(usps)
        if fn:
            try:
                live = fn() or {}
            except Exception as e:  # noqa: BLE001
                print("  ! %s county results failed: %s" % (usps, str(e)[:60]), file=sys.stderr)
                live = {}
        counties = {}
        for county, races in live.items():
            entry = {}
            for rt, v in races.items():
                entry[rt] = {"rep": v.get("rep", 0), "dem": v.get("dem", 0), "other": v.get("other", 0),
                             "margin": margin(v.get("rep", 0), v.get("dem", 0), v.get("other", 0)),
                             "reporting": v.get("reporting")}
            if entry:
                counties[county] = entry
        if counties:
            any_data = True
        st = usps.lower()
        outdir = os.path.join(ROOT, "data", st)
        if not os.path.isdir(outdir):
            continue  # we don't track county turnout for this state
        out = {"state": st, "updated": (_now() if counties else ""),
               "race_types": sorted(race_types), "counties": counties,
               "note": "County result margins wired near Election Day; empty until then."}
        with open(os.path.join(outdir, "results_county.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, separators=(",", ":"))
        wrote += 1
    print("results_county: wrote %d state files; %s" % (wrote, "has data" if any_data else "all empty (expected off Election Day)"))
    return 0


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    sys.exit(main())
