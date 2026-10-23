#!/usr/bin/env python3
"""West Virginia turnout by county and registered party (early voting +
absentee; WV registers by party).

No public SoS feed: sos.wv.gov's election-data pages are historical only, and
during early voting it has posted only press releases (statewide totals in
prose; checked 2026-10-14 and again 2026-10-22, when nothing for the general
was up yet). The SoS shares daily county counts with the UF Election Lab,
whose 2026 files cite "West Virginia Secretary of State" (first posted
2026-10-22: 16,922 in-person early voters, Oct 21-22, in all 55 counties, by
party -- Democrat / Republican / none, the last being no party and the minor
parties). So this writes the Election Lab stand-in (scripts/lab_standin.py: US.csv
+ WV_county.csv, unaltered, attributed). The Lab notes WV publishes no mail-ballot
figure and its "requested" columns just repeat the early votes, so they aren't
shown (config lab_no_requests). An official feed wired in fetch_counties()
replaces the stand-in automatically.

Run:  python scripts/wv_update.py [--force]
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "wv"
CONFIG_PATH = os.path.join(ROOT, "config", STATE + ".json")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
SOURCE = "West Virginia Secretary of State (by county & registered party)"
VOTED_METHODS = ["early_voted", "mail_voted"]


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def fetch_counties(cfg):
    """Return {county: {fips, methods:{mkey:{rep,dem,oth,npa}}}} from the live feed.

    None yet (see the module docstring): {} -> the Election Lab stand-in is written instead.
    """
    return {}


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH)

    rows = fetch_counties(cfg)
    if not rows:   # stand-in until the SoS posts a county feed of its own
        import lab_standin as LAB
        if LAB.run(STATE, cfg, os.path.join(ROOT, "assets", "wv-counties.geojson"), LATEST_PATH, HISTORY_PATH,
                   partisan=True, force=force):
            return 0
    counties_out = {}
    for name, r in rows.items():
        ent = {"fips": r["fips"]}
        for mkey, s in r.get("methods", {}).items():
            ent[mkey] = C.party_block(s.get("rep", 0), s.get("dem", 0), s.get("oth", 0), s.get("npa", 0))
        voted = [ent[m] for m in VOTED_METHODS if ent.get(m)]
        ent["cast"] = C.add_blocks(*voted) if voted else C.party_block(0, 0, 0, 0)
        counties_out[name] = ent

    statewide = {}
    for mkey in VOTED_METHODS:
        blocks = [counties_out[n][mkey] for n in counties_out if counties_out[n].get(mkey)]
        if blocks:
            statewide[mkey] = C.add_blocks(*blocks)
    voted = [statewide[m] for m in VOTED_METHODS if statewide.get(m)]
    statewide["cast"] = C.add_blocks(*voted) if voted else C.party_block(0, 0, 0, 0)
    methods_present = sorted({m for c in counties_out.values() for m in VOTED_METHODS if c.get(m)})

    snap = {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"],
        "source": {"primary": SOURCE},
        "source_compiled": "", "source_compiled_iso": (C.utc_now_iso() if counties_out else ""),
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
        cast = statewide["cast"]
        print("CHANGED  counties=%d  cast=%s margin=%s" % (len(counties_out), cast["total"], cast["margin"]))
    else:
        print("NOCHANGE  (hash %s)" % (prev or "")[:12])
    return 0


if __name__ == "__main__":
    sys.exit(main())
