#!/usr/bin/env python3
"""Fetch Nevada turnout by county and party from the NV SoS VIVID portal.

Two feeds (static JSON, behind Incapsula -> needs a Referer header):
  turnout-seed.json : in-person early voting, per day, per county {dem,rep,other}
  ballot-seed.json  : mail ballot stages per county, rawByPartyCode
                      {sent,received,accepted,...:{DEM,REP,...}}

NV is all-mail; we report mail_voted (received), mail_provided (sent-received =
outstanding), early_voted, and cast = mail_voted + early_voted, by party.

The feeds currently hold the 2026 PRIMARY; NV general early voting/mail begin in
October. We gate on `general_start` so the tracker reads 0 until real general
data appears, then lights up automatically. Use --all to ignore the gate (to
test parsing against whatever the feed currently holds).

Run:  python scripts/nv_update.py [--force] [--all]
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "nv"
CONFIG_PATH = os.path.join(ROOT, "config", "nv.json")
COUNTIES_PATH = os.path.join(ROOT, "config", "nv_counties.json")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
VOTED_METHODS = ["mail_voted", "early_voted"]


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def party_of(code):
    c = str(code).strip().upper()
    return "dem" if c == "DEM" else "rep" if c == "REP" else "oth"


def main():
    force = "--force" in sys.argv
    ignore_gate = "--all" in sys.argv
    cfg = load(CONFIG_PATH)
    counties = {c["name"]: c for c in load(COUNTIES_PATH)["counties"]}
    ref = cfg["source"]["referer"]
    gstart = cfg["general_start"]

    # accumulator: county -> method -> {rep,dem,oth,npa}
    agg = {}
    def add(county, method, party, n):
        if county not in counties or not n:
            return
        slot = agg.setdefault(county, {}).setdefault(method, {"rep": 0, "dem": 0, "oth": 0, "npa": 0})
        slot[party] += int(n)

    src_date = ""

    # 1) in-person early voting (sum days within the general window)
    try:
        t = json.loads(C.http_get(cfg["source"]["turnout_seed"], referer=ref, no_cache=True))
        for day in t.get("days", []):
            d = day.get("date", "")
            if not ignore_gate and d < gstart:
                continue
            for r in day.get("rows", []):
                add(r.get("county"), "early_voted", "dem", r.get("dem"))
                add(r.get("county"), "early_voted", "rep", r.get("rep"))
                add(r.get("county"), "early_voted", "oth", r.get("other"))
                if d > src_date:
                    src_date = d
    except Exception as e:  # noqa: BLE001
        print("WARN early-voting feed: %s" % str(e)[:60], file=sys.stderr)

    # 2) mail ballots (whole snapshot; gate on generatedAt)
    try:
        b = json.loads(C.http_get(cfg["source"]["ballot_seed"], referer=ref, no_cache=True))
        gen = (b.get("generatedAt") or "")[:10]
        if ignore_gate or gen >= gstart:
            for row in b.get("rows", []):
                county = row.get("county")
                stages = row.get("rawByPartyCode", {})
                received = stages.get("received", {}) or {}
                sent = stages.get("sent", {}) or {}
                for code, nrec in received.items():
                    add(county, "mail_voted", party_of(code), nrec)
                for code, nsent in sent.items():
                    out = int(nsent) - int(received.get(code, 0))
                    if out > 0:
                        add(county, "mail_provided", party_of(code), out)
            if gen > src_date:
                src_date = gen
    except Exception as e:  # noqa: BLE001
        print("WARN mail feed: %s" % str(e)[:60], file=sys.stderr)

    # build snapshot
    counties_out = {}
    for name, c in counties.items():
        methods = agg.get(name, {})
        ent = {"fips": c["fips"]}
        for mkey, s in methods.items():
            ent[mkey] = C.party_block(s["rep"], s["dem"], s["oth"], s["npa"])
        voted = [ent[m] for m in VOTED_METHODS if ent.get(m)]
        ent["cast"] = C.add_blocks(*voted) if voted else C.party_block(0, 0, 0, 0)
        counties_out[name] = ent

    statewide = {}
    for mkey in ["mail_voted", "early_voted", "mail_provided"]:
        blocks = [counties_out[n][mkey] for n in counties_out if counties_out[n].get(mkey)]
        if blocks:
            statewide[mkey] = C.add_blocks(*blocks)
    voted = [statewide[m] for m in VOTED_METHODS if statewide.get(m)]
    statewide["cast"] = C.add_blocks(*voted) if voted else C.party_block(0, 0, 0, 0)

    methods_present = sorted({m for c in counties_out.values() for m in ["mail_voted", "early_voted", "mail_provided"] if c.get(m)})
    snap = {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"],
        "source": {"primary": "NV SoS VIVID (in-person early voting + mail ballot tracker), by registered party"},
        "source_compiled": src_date, "source_compiled_iso": (src_date + "T00:00:00") if src_date else "",
        "methods_present": methods_present, "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties_out,
    }
    snap["data_hash"] = C.data_hash(snap)
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
        rec = {"generated_at": snap["generated_at"], "compiled": src_date, "data_hash": snap["data_hash"],
               "statewide": {m: [x["rep"], x["dem"], x["oth"], x["npa"], x["total"]]
                             for m, x in statewide.items() if x.get("total")}}
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        cast = statewide["cast"]
        print("CHANGED  through=%s  cast=%s (R%s D%s Oth%s) margin=%s  methods=%s"
              % (src_date or "(pre-general)", cast["total"], cast["rep"], cast["dem"], cast["oth"],
                 cast["margin"], methods_present))
    else:
        print("NOCHANGE  through=%s (hash %s)" % (src_date or "(pre-general)", (prev or "")[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
