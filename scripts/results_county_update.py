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
import io
import os
import sys
import json
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SLATE_PATH = os.path.join(ROOT, "config", "results_races.json")
sys.path.insert(0, HERE)
import common as C  # noqa: E402
import results_feeds as F  # noqa: E402


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def parse_NC():
    """NC SBE statewide precinct-by-contest results file: one tab-separated row per
    county/precinct/contest/choice, all zeros until polls close. Sums "US SENATE"
    (NC's only 2026 Governor/Senate-slate race -- no NC governor contest this cycle)
    by county/choice, over every row: votes counted outside regular precincts
    (absentee, early voting; "Real Precinct" = N) belong to the county too. A county
    with no reported votes yet is left out."""
    url = "https://s3.amazonaws.com/dl.ncsbe.gov/ENRS/2026_11_03/results_pct_20261103.zip"
    z = zipfile.ZipFile(io.BytesIO(C.http_get(url, binary=True, timeout=60)))
    name = next(n for n in z.namelist() if n.endswith(".txt"))
    lines = z.read(name).decode("utf-8", "replace").splitlines()
    header = lines[0].split("\t")
    idx = {h: i for i, h in enumerate(header)}
    need = ["County", "Contest Name", "Choice Party", "Total Votes"]
    if any(h not in idx for h in need):
        raise RuntimeError("NC county results: expected column missing (%s)" % header)
    names = {c["code"]: c["name"] for c in load(os.path.join(ROOT, "config", "nc_counties.json"))["counties"]}

    counties = {}   # county name -> {rep, dem, other}
    for ln in lines[1:]:
        f = ln.split("\t")
        if len(f) <= idx["Total Votes"] or f[idx["Contest Name"]] != "US SENATE":
            continue
        name = names.get(f[idx["County"]])
        if not name:
            continue
        votes = int(f[idx["Total Votes"]] or 0)
        party = f[idx["Choice Party"]]
        key = "rep" if party == "REP" else "dem" if party == "DEM" else "other"
        c = counties.setdefault(name, {"rep": 0, "dem": 0, "other": 0})
        c[key] += votes

    return {name: {"sen": v} for name, v in counties.items() if (v["rep"] + v["dem"] + v["other"])}


# SOURCES[usps] -> function() -> {County: {race_type: {rep,dem,other,reporting}}}
SOURCES = dict(F.COUNTIES, NC=parse_NC)


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
        if fn and F.polls_closed(usps):   # feeds carry test data before polls close
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
