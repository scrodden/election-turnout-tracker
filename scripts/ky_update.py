#!/usr/bin/env python3
"""Kentucky absentee ballots by county and party.

Source: the KY State Board of Elections' "2026 General Election Current Voter
Turnout" workbook linked from elect.ky.gov (Documents/Absentee_Public_MMDDYY.xlsx,
date in the name; sheet DATA). Per county:
  All/DEM/REP Mail-in Applications, All/DEM/REP Ballots SENT,
  All/DEM/REP Ballots RETURNED, All/DEM/REP Excused In-person,
  All/DEM/REP No Excuse In-person, FPCA Applications, FPCA RETURNED,
  UNOFFICIAL Total Absentee; plus a TOTALS row (used as a check).
Only DEM and REP are split out, so everyone else (independents, minor parties,
and military/overseas FPCA ballots, which carry no party) -> oth.
requested = mail-in + FPCA applications; mail_voted = mail + FPCA returned;
early_voted = excused + no-excuse in-person; mail_provided = requested -
returned (outstanding) -> ballot chase.

If the workbook can't be read, falls back to the UF Election Lab stand-in.

Run:  python scripts/ky_update.py [--force]
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "ky"
CONFIG_PATH = os.path.join(ROOT, "config", STATE + ".json")
GEO_PATH = os.path.join(ROOT, "assets", "ky-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
HOME = "https://elect.ky.gov/Pages/default.aspx"


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _num(v):
    v = str(v or "").replace(",", "").strip()
    try:
        return int(float(v)) if v else 0
    except ValueError:
        return 0


def newest_workbook():
    h = C.http_get(HOME, retries=2)
    links = re.findall(r'href="([^"]*Absentee_Public_(\d{2})(\d{2})(\d{2})\.xlsx)"', h)
    if not links:
        raise RuntimeError("KY: turnout workbook not linked from the home page")
    href, mm, dd, yy = max(links, key=lambda x: (x[3], x[1], x[2]))
    url = href if href.startswith("http") else "https://elect.ky.gov" + href
    return url, "20%s-%s-%s" % (yy, mm, dd)


def parse(raw):
    rows = next(iter(C.read_xlsx(raw, positional=True).values()), [])
    hi = next(i for i, r in enumerate(rows) if r and r[0].strip() == "County")
    head = [c.strip() for c in rows[hi]]

    def col(name):
        return head.index(name)
    cols = {k: col(k) for k in ("All Mail-in Applications", "DEM Mail-in Applications", "REP Mail-in Applications",
                                "All Ballots RETURNED", "DEM Ballots RETURNED", "REP Ballots RETURNED",
                                "Excused In-person", "DEM Excused In-person", "REP Excused In-person",
                                "No Excuse In-person", "DEM No Excuse In-person", "REP No Excuse In-person",
                                "FPCA Applications", "FPCA RETURNED", "UNOFFICIAL Total Absentee")}
    out, totals = {}, None
    for r in rows[hi + 1:]:
        if not r or not r[0].strip():
            continue
        vals = {k: _num(r[i]) if i < len(r) else 0 for k, i in cols.items()}
        if r[0].strip().upper() == "TOTALS":
            totals = vals
        else:
            out[r[0].strip()] = vals
    if not totals:
        raise RuntimeError("KY: TOTALS row not found")
    for k in cols:
        if sum(v[k] for v in out.values()) != totals[k]:
            raise RuntimeError("KY: county %r doesn't add up to TOTALS" % k)
    return out


def split(allv, dem, rep):
    return {"dem": dem, "rep": rep, "npa": 0, "oth": max(0, allv - dem - rep)}


def entity(v):
    req = split(v["All Mail-in Applications"] + v["FPCA Applications"],
                v["DEM Mail-in Applications"], v["REP Mail-in Applications"])
    ret = split(v["All Ballots RETURNED"] + v["FPCA RETURNED"], v["DEM Ballots RETURNED"], v["REP Ballots RETURNED"])
    inp = split(v["Excused In-person"] + v["No Excuse In-person"],
                v["DEM Excused In-person"] + v["DEM No Excuse In-person"],
                v["REP Excused In-person"] + v["REP No Excuse In-person"])

    def pb(d):
        return C.party_block(d["rep"], d["dem"], d["oth"], d["npa"])
    e = {"mail_voted": pb(ret), "early_voted": pb(inp),
         "mail_provided": pb({k: max(0, req[k] - ret[k]) for k in req}),
         "cast": pb({k: ret[k] + inp[k] for k in ret}), "registered": 0, "turnout_pct": None}
    m = C.compute_mail(e)
    if m:
        e["mail"] = m
    return e


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    from datetime import datetime, timezone
    if not force and (load(LATEST_PATH, {}) or {}).get("counties") and datetime.now(timezone.utc).minute >= 12:
        print("ky: next check at the top of the hour.")   # the workbook updates about daily
        return 0
    try:
        url, as_of = newest_workbook()
        rows = parse(C.http_get(url, binary=True, retries=2))
    except Exception as e:  # noqa: BLE001
        print("KY SBE workbook unavailable: %s" % str(e)[:140], file=sys.stderr)
        rows = {}
    if not rows:
        import lab_standin as LAB   # official file unavailable -> UF Election Lab stand-in
        LAB.run(STATE, cfg, GEO_PATH, LATEST_PATH, HISTORY_PATH, partisan=True, force=force)
        return 0

    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}
    unmatched = [n for n in rows if _norm(n) not in gidx]
    if unmatched:
        print("KY: unmatched counties %s — keeping previous snapshot." % unmatched[:5], file=sys.stderr)
        return 0
    counties = {}
    tot = {k: 0 for k in next(iter(rows.values()))}
    for name, v in rows.items():
        g = gidx[_norm(name)]
        counties[g["name"]] = dict(entity(v), fips=g["fips"])
        for k in tot:
            tot[k] += v[k]
    statewide = entity(tot)
    snap = {
        "state": STATE, "state_name": cfg.get("state_name", "Kentucky"), "election": cfg.get("election", {}),
        "partisan": True,
        "source": {"primary": "KY State Board of Elections 'Current Voter Turnout' absentee workbook, as of %s" % as_of,
                   "url": url, "as_of": as_of},
        "source_compiled": as_of, "source_compiled_iso": C.utc_now_iso(),
        "methods_present": [k for k in ("mail_voted", "early_voted", "mail_provided") if statewide[k]["total"]],
        "method_labels": cfg.get("method_labels", {}), "statewide": statewide, "counties": counties,
    }
    prev = load(LATEST_PATH, {}) or {}
    snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k != "source_compiled_iso"})
    snap["generated_at"] = C.utc_now_iso()
    if not force and snap["data_hash"] == prev.get("data_hash"):
        print("NOCHANGE  (KY workbook as of %s)" % as_of)
        return 0
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    c = statewide["cast"]
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"generated_at": snap["generated_at"],
                            "statewide": {"cast": [c["rep"], c["dem"], c["oth"], c["npa"], c["total"]]}},
                           separators=(",", ":")) + "\n")
    m = statewide.get("mail") or {}
    print("CHANGED  KY SBE workbook as of %s: %d counties | returned=%d of %d requested (R%d D%d Oth%d) margin=%s"
          % (as_of, len(counties), c["total"], m.get("requested", 0), c["rep"], c["dem"], c["oth"], c["margin"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
