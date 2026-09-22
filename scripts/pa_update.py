#!/usr/bin/env python3
"""Fetch Pennsylvania mail-ballot turnout by county and party from the PA
Department of State open-data (Socrata) dataset, aggregated server-side.

Dataset: 2026 General Election Mail Ballot Requests (voter-level).
We request county x party x ballot_status counts via Socrata $group, so one
small request covers the whole state. Maps ballot_status:
  Pending       -> mail_provided (approved application, not yet returned)
  Vote Recorded -> mail_voted (returned & counted)
PA has no in-person early voting; cast = mail_voted.

Run:  python scripts/pa_update.py [--force]
"""
import os
import sys
import json
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "pa"
CONFIG_PATH = os.path.join(ROOT, "config", "pa.json")
COUNTIES_PATH = os.path.join(ROOT, "config", "pa_counties.json")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH)
    counties = {c["name"].upper(): c for c in load(COUNTIES_PATH)["counties"]}
    party_map = cfg["party_map"]
    status_map = cfg["status_map"]

    q = cfg["source"]["socrata"] + "?" + urllib.parse.urlencode({
        "$select": "countyname,party,ballot_status,count(*)",
        "$group": "countyname,party,ballot_status",
        "$limit": "50000",
    })
    print("Fetching PA Socrata aggregation...")
    rows = json.loads(C.http_get(q, no_cache=True, timeout=90))
    print("  %d grouped rows" % len(rows))

    agg = {}
    for r in rows:
        cu = (r.get("countyname") or "").strip().upper()
        c = counties.get(cu)
        mkey = status_map.get((r.get("ballot_status") or "").strip())
        if not c or not mkey:
            continue
        tgt = party_map.get((r.get("party") or "").strip().upper(), "oth")
        slot = agg.setdefault(c["name"], {}).setdefault(mkey, {"rep": 0, "dem": 0, "oth": 0, "npa": 0})
        slot[tgt] += int(r.get("count") or r.get("count_1") or 0)

    counties_out = {}
    for cu, c in counties.items():
        methods = agg.get(c["name"], {})
        ent = {"fips": c["fips"]}
        for mkey, s in methods.items():
            ent[mkey] = C.party_block(s["rep"], s["dem"], s["oth"], s["npa"])
        ent["cast"] = ent.get("mail_voted") or C.party_block(0, 0, 0, 0)
        m = C.compute_mail(ent)          # mail-ballot 'chase' metrics (sent/returned by party)
        if m:
            ent["mail"] = m
        counties_out[c["name"]] = ent

    statewide = {}
    for mkey in ["mail_voted", "mail_provided"]:
        blocks = [counties_out[n][mkey] for n in counties_out if counties_out[n].get(mkey)]
        if blocks:
            statewide[mkey] = C.add_blocks(*blocks)
    statewide["cast"] = statewide.get("mail_voted") or C.party_block(0, 0, 0, 0)
    sw_mail = C.compute_mail(statewide)
    if sw_mail:
        statewide["mail"] = sw_mail

    methods_present = sorted({m for c in counties_out.values() for m in ["mail_voted", "mail_provided"] if c.get(m)})
    snap = {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"],
        "source": {"primary": "PA Dept. of State open data — mail-ballot applications by county & party"},
        "source_compiled": C.utc_now_iso()[:10], "source_compiled_iso": C.utc_now_iso(),
        "methods_present": methods_present, "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties_out,
    }
    snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k not in ("source_compiled", "source_compiled_iso")})
    snap["generated_at"] = C.utc_now_iso()

    prev = None
    if os.path.exists(LATEST_PATH):
        try:
            prev = load(LATEST_PATH).get("data_hash")
        except (ValueError, OSError):
            pass
    changed = force or (snap["data_hash"] != prev)
    os.makedirs(DATA_DIR, exist_ok=True)
    if changed:
        with open(LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        rec = {"generated_at": snap["generated_at"], "data_hash": snap["data_hash"],
               "statewide": {m: [x["rep"], x["dem"], x["oth"], x["npa"], x["total"]]
                             for m, x in statewide.items() if x.get("total")}}
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        mp = statewide.get("mail_provided") or {}
        mv = statewide.get("mail_voted") or {}
        print("CHANGED  mail_outstanding=%s (R%s D%s margin=%s)  mail_voted=%s"
              % (mp.get("total"), mp.get("rep"), mp.get("dem"), mp.get("margin"), mv.get("total")))
    else:
        print("NOCHANGE  (hash %s)" % (prev or "")[:12])
    return 0


if __name__ == "__main__":
    sys.exit(main())
