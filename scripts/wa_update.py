#!/usr/bin/env python3
"""Washington ballots returned by county (turnout only; Washington is all-mail
and has no party registration).

Source: the Secretary of State's "Ballot Return Statistics" page
(sos.wa.gov/elections/data-research/election-data-and-maps/ballot-return-statistics),
a Power BI publish-to-web report (US Government cloud). Its public query API
(the one the embedded report itself calls; client in oh_update.PowerBI) gives:
  Ballot Status Files   one row per returned ballot: County, Ballot Status
                        (Accepted / Received / Rejected), Received Date Only
  Voters                County, VotersInElection (ballots mailed: every voter)
  AgeRange              County, AgeRange, Received (returned), Total (voters)
  Former Election Data  the 2022 general's ballots with their received dates
  Date Refresh          "Date/Time" of the last refresh (Pacific time)
  Measures Table        "# TotalReturned" etc. (the dashboard's headline)
Ballots returned = every status (the dashboard's "# TotalReturned"); checked
against that measure. Registered = voters in the election, so outstanding =
voters - returned. The 2022 comparison counts that election's ballots received
through the same number of days before its Election Day.

Run:  python scripts/wa_update.py [--force]
"""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "wa"
CONFIG_PATH = os.path.join(ROOT, "config", "wa.json")
GEO_PATH = os.path.join(ROOT, "assets", "wa-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
FORMER_PATH = os.path.join(DATA_DIR, "_former_election.json")


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def blk(t):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(t), "rep_pct": None,
            "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def _day(v):
    """A Power BI date value (epoch ms) -> date, or None."""
    return datetime.fromtimestamp(v / 1000, timezone.utc).date() if isinstance(v, (int, float)) else None


def measures(pbi, names):
    from oh_update import _decode_dsr
    sel = [{"Measure": {"Expression": {"SourceRef": {"Source": "m"}}, "Property": n}, "Name": "m%d" % i} for i, n in enumerate(names)]
    q = {"Version": 2, "From": [{"Name": "m", "Entity": "Measures Table", "Type": 0}], "Select": sel}
    body = {"version": "1.0.0", "cancelQueries": [], "modelId": pbi.model, "queries": [{
        "Query": {"Commands": [{"SemanticQueryDataShapeCommand": {
            "Query": q, "Binding": {"Primary": {"Groupings": [{"Projections": list(range(len(sel)))}]},
                                    "DataReduction": {"DataVolume": 4, "Primary": {"Window": {"Count": 100}}}, "Version": 1}}}]},
        "QueryId": "", "ApplicationContext": {"DatasetId": pbi.dataset, "Sources": [{"ReportId": pbi.report}]}}]}
    rows = _decode_dsr(pbi._call("querydata?synchronous=true", body))
    return dict(zip(names, rows[0])) if rows else {}


def former_election(pbi, src, gidx):
    """The comparison election's ballots by county and received date, read once (it doesn't change) into
    data/wa/_former_election.json: {"name", "election_day", "counties": {county: {iso date: n}}}."""
    cache = load(FORMER_PATH, {}) or {}
    if cache.get("name") == src["source_name"] and cache.get("counties"):
        return cache
    names = [r[0] for r in pbi.query("Former Election Data", ["Source.Name"], [], {}, aggs=[("Ballot ID", 5)])]
    if names != [src["source_name"]]:
        raise ValueError("former election is %s, not %s" % (names, src["source_name"]))
    out = {}
    for county, when, status, n in pbi.query("Former Election Data", ["County", "Received Date", "Ballot Status"], [], {},
                                             aggs=[("Ballot ID", 5)]):
        g, d = gidx.get(_norm(county)), _day(when)
        if not g or not d or status == "Ballot Status":   # 3 header rows read in as ballots
            continue
        dd = out.setdefault(g["name"], {})
        dd[d.isoformat()] = dd.get(d.isoformat(), 0) + int(n or 0)
    if len(out) != len(gidx):
        raise ValueError("former election: %d of %d counties" % (len(out), len(gidx)))
    cache = {"name": src["source_name"], "election_day": src["election_day"], "year": src["year"], "counties": out}
    with open(FORMER_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cache, f, separators=(",", ":"))
    return cache


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg["source"]
    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}
    prev = load(LATEST_PATH, {}) or {}
    if not force and prev.get("statewide", {}).get("cast", {}).get("total") and C.checked_recently(DATA_DIR, "source", minutes=30):
        print("wa: checked under 30 minutes ago.")
        return 0
    from oh_update import PowerBI
    pbi = PowerBI(src["pbi_api"], src["pbi_resource_key"])

    title = " ".join(str(r[0]) for r in pbi.query("Last Updated", ["Election Title"], [], {}))
    if src.get("election_match", "2026 November General") not in title:
        print("wa: the dashboard shows '%s', not the 2026 general -- not used" % title)
        return 0
    refresh = (pbi.query("Date Refresh", ["Date/Time"], [], {}) or [[""]])[0][0]   # "10/20/2026 5:25:57 PM", Pacific

    returned, accepted = {}, {}
    for county, status, n in pbi.query("Ballot Status Files", ["County", "Ballot Status"], [], {}, aggs=[("Ballot ID", 5)]):
        g = gidx.get(_norm(county))
        if not g:
            raise ValueError("county %r not on the map" % county)
        returned[g["name"]] = returned.get(g["name"], 0) + int(n or 0)
        if status == "Accepted":
            accepted[g["name"]] = accepted.get(g["name"], 0) + int(n or 0)
    voters = {}
    for county, n in pbi.query("Voters", ["County"], ["VotersInElection"], {}):
        g = gidx.get(_norm(county))
        if g:
            voters[g["name"]] = int(n or 0)
    head = measures(pbi, ["# TotalReturned", "# Accepted", "# Received", "# Challenged", "# Voters"])
    total, n_voters = sum(returned.values()), sum(voters.values())
    if total != head.get("# TotalReturned") or n_voters != head.get("# Voters") or len(voters) != len(gidx):
        raise ValueError("counties (%d returned, %d voters in %d counties) don't match the dashboard (%s, %s)"
                         % (total, n_voters, len(voters), head.get("# TotalReturned"), head.get("# Voters")))
    ages = {}
    for county, band, rec, tot in pbi.query("AgeRange", ["County", "AgeRange"], ["Received", "Total"], {}):
        g = gidx.get(_norm(county))
        if g and band:
            ages.setdefault(g["name"], {})[band] = int(rec or 0)
    through = max(filter(None, (_day(r[0]) for r in pbi.query("Ballot Status Files", ["Received Date Only"], [], {},
                                                             aggs=[("Ballot ID", 5)]))), default=None)

    counties = {}
    for name, g in ((p["name"], p) for p in gidx.values()):
        n, v = returned.get(name, 0), voters.get(name, 0)
        ent = {"fips": g["fips"], "cast": blk(n), "mail_voted": blk(n), "registered": v,
               "turnout_pct": round(100.0 * n / v, 2) if v else None}
        if v > n:
            ent["mail_provided"] = blk(v - n)
        if ages.get(name):
            ent["demographics"] = {"age": [{"label": k, "count": c} for k, c in sorted(ages[name].items())]}
        counties[name] = ent
    sw = {"cast": blk(total), "mail_voted": blk(total), "mail_provided": blk(max(0, n_voters - total)),
          "registered": n_voters, "turnout_pct": round(100.0 * total / n_voters, 2) if n_voters else None}
    sw["mail"] = C.compute_mail(sw)
    age_tot = {}
    for a in ages.values():
        for k, c in a.items():
            age_tot[k] = age_tot.get(k, 0) + c
    body = {
        "state": STATE, "state_name": cfg.get("state_name", "Washington"), "election": cfg.get("election", {}),
        "partisan": False, "methods_present": ["mail_voted", "mail_provided"],
        "method_labels": cfg.get("method_labels", {}), "mail_base_label": "Ballots sent",
        "statewide": sw, "counties": counties,
        "map_note": ("Ballots returned so far: %s accepted, %s received and still being processed, %s challenged "
                     "(signature or other issues voters can still fix), as the Secretary of State counts them."
                     % (format(head.get("# Accepted") or 0, ","), format(head.get("# Received") or 0, ","),
                        format(head.get("# Challenged") or 0, ","))),
    }
    if age_tot:
        body["demographics"] = {"age": [{"label": k, "count": c} for k, c in sorted(age_tot.items())],
                                "note": "Age of voters whose ballots have been returned, from the Secretary of State's dashboard."}
    try:   # the same number of days before Election Day in the comparison election
        fe = former_election(pbi, src["former_election"], gidx)
        if through:
            days_out = (date.fromisoformat(cfg["election"]["date"]) - through).days
            cutoff = (date.fromisoformat(fe["election_day"]) - timedelta(days=days_out)).isoformat()
            cty = {c: sum(n for d, n in dd.items() if d <= cutoff) for c, dd in fe["counties"].items()}
            body["same_point"] = {"label": "through %d days before Election Day" % days_out, "day": days_out,
                                  "what": "ballots returned",
                                  "source": "Washington Secretary of State ballot return statistics (%s general, by received date)" % fe["year"],
                                  "url": src["page"], "years": {fe["year"]: {"statewide": sum(cty.values()), "counties": cty}}}
    except Exception as e:  # noqa: BLE001 - the comparison is extra
        print("wa: %s comparison not used (%s)" % (src["former_election"].get("year"), str(e)[:140]), file=sys.stderr)

    import lab_standin as LAB
    bad = LAB.suspicious_drop(prev, total)
    if bad:
        print("wa: %s" % bad, file=sys.stderr)
        return 0
    h = C.data_hash(body)
    changed = force or h != prev.get("data_hash")
    now = C.utc_now_iso()
    iso = ""
    try:   # Pacific daylight time until Nov 1
        iso = (datetime.strptime(refresh, "%m/%d/%Y %I:%M:%S %p") + timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass
    snap = dict(body, source={"primary": "Washington Secretary of State, Ballot Return Statistics (Power BI dashboard)",
                              "url": src["page"], "as_of": refresh, "through": through.isoformat() if through else "",
                              "election": title},
                source_compiled=refresh, source_compiled_iso=iso or now,
                data_hash=h, generated_at=now if changed else prev.get("generated_at", now))
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LATEST_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(snap, f, separators=(",", ":"))
    if changed:
        with open(HISTORY_PATH, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"generated_at": now, "compiled": refresh, "cast": total, "registered": n_voters},
                               separators=(",", ":")) + "\n")
    print("%s  WA as of %s (received through %s) | returned %d of %d (%s%%) in %d counties%s"
          % ("CHANGED" if changed else "NOCHANGE", refresh, through, total, n_voters, sw["turnout_pct"], len(counties),
             (" | %s same point %d" % (list(snap["same_point"]["years"])[0], list(snap["same_point"]["years"].values())[0]["statewide"]))
             if snap.get("same_point") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
