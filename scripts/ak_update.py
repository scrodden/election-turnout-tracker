#!/usr/bin/env python3
"""Alaska turnout by county and registered party.

Source: Iowa SoS "Absentee Ballot Statistics" PDF, by county and party
(requested / issued / received). "Received" (returned) by party = ballots cast.
The 2024 file persists, so this is upgradeable to a real pypdf parser (like
LA/MD). STAGED SKELETON for now: writes an empty (0) partisan snapshot until the
parser is wired against the live 2026 file (the Friday rollout routine does this
in Oct). See config/ia.json for the exact URLs.

Run:  python scripts/ia_update.py [--force]
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "ak"
CONFIG_PATH = os.path.join(ROOT, "config", STATE + ".json")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
SOURCE = "Alaska Division of Elections early/absentee voting (by borough/house district & party affiliation)"
VOTED_METHODS = ["early_voted", "mail_voted"]


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def fetch_counties(cfg):
    """Return {county: {fips, methods:{mkey:{rep,dem,oth,npa}}}} from the live feed.

    PENDING: no wired parser yet. Returns {} (empty snapshot); the rollout
    routine wires the live parser in Oct.
    """
    return {}


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH)

    rows = fetch_counties(cfg)
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
