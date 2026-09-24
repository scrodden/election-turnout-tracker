#!/usr/bin/env python3
"""New Jersey mail (and, from Oct 24, in-person early) ballots by COUNTY and party.

New Jersey's Division of Elections does not publish pre-election mail-ballot
counts (its county 'periodic reports' start on Election Day). The UF Election
Lab (Dr. Michael McDonald) receives the state's figures directly and publishes
them daily, including a county file:
  /data-downloads/earlyvote/2026/NJ_county.csv   (21 counties)
  /data-downloads/earlyvote/2026/US.csv          (statewide row: last_update)
Fields per county: request_/accept_/voted_/inperson_ x dem/rep/none/all.
We use them unaltered, with attribution, under CC BY-NC-ND 4.0 (credit the
Lab, non-commercial, numbers unmodified); county rows must sum to the Lab's
statewide row.

Democratic->dem, Republican->rep, unaffiliated ('none')->npa; any remainder of
*_all over the three -> oth. mail_provided = requested - returned (outstanding)
-> ballot chase. Turnout % uses total registered voters per county from the
Division of Elections' monthly registration-by-county PDF (official).

Run:  python scripts/nj_update.py [--force]
"""
import csv
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "nj"
CONFIG_PATH = os.path.join(ROOT, "config", "nj.json")
GEO_PATH = os.path.join(ROOT, "assets", "nj-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
KINDS = ("request", "accept", "inperson")


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _int(s):
    s = str(s or "").strip().replace(",", "")
    try:
        return int(float(s)) if s and s.upper() != "NA" else 0
    except ValueError:
        return 0


def _parties(row, prefix):
    d, r, n, a = (_int(row.get("%s_%s" % (prefix, k))) for k in ("dem", "rep", "none", "all"))
    return {"dem": d, "rep": r, "npa": n, "oth": max(0, a - d - r - n)}


def _pb(p):
    return C.party_block(p["rep"], p["dem"], p["oth"], p["npa"])


def registered_by_county():
    """{norm(county): total registered} from the NJ DOS monthly registration PDF."""
    try:
        import registration_update as R
        raw, as_of, url = R._latest_pdf(
            "https://www.nj.gov/state/elections/assets/pdf/svrs-reports/2026/2026-%s-voter-registration-by-county.pdf",
            lambda m: "%02d" % m)
        out = {}
        for name, nums in re.findall(r"^([A-Z][a-z]+(?: [A-Z][a-z]+)?)\s+(\d+(?:\s+\d+){10})\s*$", R._pdf_text(raw), re.M):
            n = [int(x) for x in nums.split()]
            if sum(n[:10]) == n[10]:
                out[_norm(name)] = n[10]
        return out, as_of
    except Exception as e:  # noqa: BLE001
        print("NJ registration by county unavailable: %s" % str(e)[:100], file=sys.stderr)
        return {}, ""


def entity(req, ret, inp, registered):
    out = {k: max(0, req[k] - ret[k]) for k in req}
    cast = {k: ret[k] + inp[k] for k in ret}
    ent = {"mail_voted": _pb(ret), "mail_provided": _pb(out), "early_voted": _pb(inp), "cast": _pb(cast),
           "registered": registered,
           "turnout_pct": (round(100.0 * sum(cast.values()) / registered, 2) if registered else None)}
    m = C.compute_mail(ent)
    if m:
        ent["mail"] = m
    return ent


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg.get("source", {})
    import lab_standin as LAB
    prev0 = load(LATEST_PATH, {}) or {}
    # the Lab updates ~daily: skip once we hold today's figures, else check ~hourly
    if prev0.get("counties") and not LAB.due(dict(prev0, lab_standin=True), force):
        print("nj: holding the Election Lab's %s update — next check not due." % (prev0.get("source") or {}).get("as_of"))
        return 0
    try:
        state_row = next((r for r in csv.DictReader(io.StringIO(C.http_get(src["lab_csv"], no_cache=True)))
                          if (r.get("state_abbv") or "").upper() == "NJ"), None)
        rows = list(csv.DictReader(io.StringIO(C.http_get(src["lab_county_csv"], no_cache=True))))
    except Exception as e:  # noqa: BLE001
        print("NJ: Election Lab files unavailable: %s" % str(e)[:120], file=sys.stderr)
        return 0
    if not rows or not state_row:
        print("NJ: no New Jersey figures in the Election Lab files yet.")
        return 0
    # county rows must add up to the Lab's statewide row
    for k in ("request_all", "accept_all", "inperson_all"):
        if sum(_int(r.get(k)) for r in rows) != _int(state_row.get(k)):
            print("NJ: county %s doesn't sum to the statewide row — keeping previous snapshot." % k, file=sys.stderr)
            return 0

    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}
    reg, reg_asof = registered_by_county()
    counties, unmatched = {}, []
    sw = {k: {"rep": 0, "dem": 0, "oth": 0, "npa": 0} for k in KINDS}
    reg_total = 0
    for r in rows:
        g = gidx.get(_norm(r.get("county")))
        if not g:
            unmatched.append(r.get("county"))
            continue
        parts = {k: _parties(r, k) for k in KINDS}
        for k in KINDS:
            for p, v in parts[k].items():
                sw[k][p] += v
        registered = reg.get(_norm(g["name"]), 0)
        reg_total += registered
        counties[g["name"]] = dict(entity(parts["request"], parts["accept"], parts["inperson"], registered), fips=g["fips"])
    if unmatched:
        print("NJ: unmatched counties %s — keeping previous snapshot." % unmatched, file=sys.stderr)
        return 0
    statewide = entity(sw["request"], sw["accept"], sw["inperson"], reg_total if len(reg) >= 21 else 0)
    if len(reg) < 21:
        for c in counties.values():
            c["registered"], c["turnout_pct"] = 0, None

    methods_present = [k for k in ("mail_voted", "early_voted", "mail_provided") if statewide[k]["total"]]
    as_of = state_row.get("last_update", "")
    snap = {
        "state": STATE, "state_name": cfg.get("state_name", "New Jersey"), "election": cfg.get("election", {}),
        "partisan": True,
        "source": {"primary": "UF Election Lab early-vote tracker (M. McDonald), New Jersey Division of Elections data, by county; CC BY-NC-ND 4.0",
                   "url": src.get("lab_page"), "as_of": as_of,
                   "registration": ("NJ DOS registration by county, %s" % reg_asof) if reg_asof else ""},
        "source_compiled": as_of, "source_compiled_iso": C.utc_now_iso(),
        "methods_present": methods_present, "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties,
    }
    prev = load(LATEST_PATH, {}) or {}
    snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k != "source_compiled_iso"})
    snap["generated_at"] = C.utc_now_iso()
    if not force and snap["data_hash"] == prev.get("data_hash"):
        print("NOCHANGE  (returned=%d as of %s)" % (statewide["cast"]["total"], as_of))
        return 0
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    c = statewide["cast"]
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"generated_at": snap["generated_at"],
                            "statewide": {"cast": [c["rep"], c["dem"], c["oth"], c["npa"], c["total"]]}},
                           separators=(",", ":")) + "\n")
    print("CHANGED  as of %s: %d counties | returned=%d of %d requested (R%d D%d NPA%d) margin=%s | turnout=%s%%"
          % (as_of, len(counties), c["total"], (statewide.get("mail") or {}).get("requested", 0),
             c["rep"], c["dem"], c["npa"], c["margin"], statewide["turnout_pct"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
