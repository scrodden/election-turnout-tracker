#!/usr/bin/env python3
"""Idaho absentee-ballot turnout by county AND party. Source: the VoteIdaho
absentee dashboard's Datawrapper charts, which expose clean CSV datasets:
  5f5QM  county x party of returned ballots (ResCountyDesc, Republican,
         Democratic, Unaffiliated)
  3EDnv  county totals (ResCountyDesc, Returned, total_issued, early_voting,
         total_voted)
  Jgr2z  statewide x party (PartyDesc, Issued, Returned) -> partisan mail chase

Idaho registers by party, so this is a partisan feed: cast = returned ballots
(Republican->rep, Democratic->dem, Unaffiliated->npa, minor->oth). Datawrapper
bumps a version integer on each publish, so we resolve the current version from
https://datawrapper.dwcdn.net/<id>/ then fetch /<id>/<ver>/dataset.csv.

Run:  python scripts/id_update.py [--force]
"""
import os
import re
import sys
import csv as csvmod
import io
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "id"
CONFIG_PATH = os.path.join(ROOT, "config", "id.json")
GEO_PATH = os.path.join(ROOT, "assets", "id-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")

PARTY = {"republican": "rep", "democratic": "dem", "democrat": "dem", "unaffiliated": "npa"}


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
        return int(float(s)) if s else 0
    except ValueError:
        return 0


def dw_csv(base, cid):
    """Resolve the Datawrapper chart's current version, then fetch its CSV rows."""
    embed = C.http_get(base + cid + "/", no_cache=True)
    m = re.search(r"/%s/(\d+)/" % re.escape(cid), embed)
    if not m:
        raise RuntimeError("ID: no version for chart %s" % cid)
    text = C.http_get("%s%s/%s/dataset.csv" % (base, cid, m.group(1)), no_cache=True)
    return list(csvmod.DictReader(io.StringIO(text)))


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg.get("source", {})
    base = src.get("dw_base", "https://datawrapper.dwcdn.net/")
    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}

    counties = {}
    unmatched = []
    sw = {"rep": 0, "dem": 0, "oth": 0, "npa": 0}
    ok = False
    try:
        cparty = {r["ResCountyDesc"]: r for r in dw_csv(base, src["chart_county_party"])}
        ctot = {r["ResCountyDesc"]: r for r in dw_csv(base, src["chart_county_totals"])}
        ok = True
        allc = set(cparty) | set(ctot)
        for name in allc:
            g = gidx.get(_norm(name))
            if not g:
                unmatched.append(name)
                continue
            p = cparty.get(name, {})
            rep = _int(p.get("Republican")); dem = _int(p.get("Democratic")); npa = _int(p.get("Unaffiliated"))
            voted = _int(ctot.get(name, {}).get("total_voted"))
            oth = max(0, voted - (rep + dem + npa))   # minor parties not in the county×party chart
            blk = C.party_block(rep, dem, oth, npa)
            issued = _int(ctot.get(name, {}).get("total_issued"))
            counties[g["name"]] = {"fips": g["fips"], "cast": blk, "mail_voted": blk,
                                   "issued": issued, "turnout_pct": None, "registered": 0}
            for k in ("rep", "dem", "oth", "npa"):
                sw[k] += blk[k]
    except Exception as e:  # noqa: BLE001
        print("ID county fetch failed: %s" % str(e)[:120], file=sys.stderr)

    statewide = {"cast": C.party_block(sw["rep"], sw["dem"], sw["oth"], sw["npa"])}
    statewide["mail_voted"] = statewide["cast"]
    statewide["registered"] = 0
    statewide["turnout_pct"] = None
    # statewide partisan mail chase from the party issued/returned chart
    try:
        prov = {"rep": 0, "dem": 0, "oth": 0, "npa": 0}
        voted = {"rep": 0, "dem": 0, "oth": 0, "npa": 0}
        for r in dw_csv(base, src["chart_statewide_party"]):
            tgt = PARTY.get((r.get("PartyDesc") or "").strip().lower(), "oth")
            iss = _int(r.get("Issued")); ret = _int(r.get("Returned"))
            voted[tgt] += ret
            prov[tgt] += max(0, iss - ret)
        statewide["mail_provided"] = C.party_block(prov["rep"], prov["dem"], prov["oth"], prov["npa"])
        statewide["mail_voted"] = C.party_block(voted["rep"], voted["dem"], voted["oth"], voted["npa"])
        m = C.compute_mail(statewide)
        if m:
            statewide["mail"] = m
    except Exception as e:  # noqa: BLE001
        print("ID statewide-party fetch failed: %s" % str(e)[:100], file=sys.stderr)

    methods_present = ["mail_voted"] if sw["rep"] + sw["dem"] + sw["npa"] + sw["oth"] else []
    snap = {
        "state": STATE, "state_name": cfg.get("state_name", "Idaho"),
        "election": cfg.get("election", {}), "partisan": True,
        "source": {"primary": "Idaho SoS VoteIdaho absentee dashboard (Datawrapper CSV); returned ballots by county & party"},
        "source_compiled": C.utc_now_iso()[:10], "source_compiled_iso": C.utc_now_iso(),
        "methods_present": methods_present, "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties,
    }
    snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k not in ("source_compiled", "source_compiled_iso")})
    snap["generated_at"] = C.utc_now_iso()

    prev = load(LATEST_PATH, {}) or {}
    changed = force or snap["data_hash"] != prev.get("data_hash")
    os.makedirs(DATA_DIR, exist_ok=True)
    if changed and (ok or not prev.get("counties")):
        with open(LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        cast = statewide["cast"]
        if cast.get("total"):
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"generated_at": snap["generated_at"],
                                    "statewide": {"cast": [cast["rep"], cast["dem"], cast["oth"], cast["npa"], cast["total"]]}},
                                   separators=(",", ":")) + "\n")
        print("CHANGED  counties=%d  cast=%d  (R%s D%s NPA%s margin=%s)"
              % (len(counties), cast["total"], cast["rep"], cast["dem"], cast["npa"], cast["margin"]))
    else:
        print("NOCHANGE  (cast=%d, %d counties)" % (statewide["cast"]["total"], len(counties)))
    if unmatched:
        print("  unmatched:", unmatched[:10])
    return 0


if __name__ == "__main__":
    sys.exit(main())
