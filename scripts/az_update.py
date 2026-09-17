#!/usr/bin/env python3
"""Arizona turnout by county and registered party — MULTI-COUNTY AGGREGATOR.

Arizona has party registration but no single statewide by-party early-vote feed:
each of the 15 county recorders publishes separately (Maricopa's daily
ballot-return stats + others), in different formats. This connector iterates the
counties in config/az.json and runs a registered per-county parser from PARSERS
(keyed by county FIPS) where one exists, merging the results into the statewide
by-party schema. Counties without a wired parser read 0.

To wire a county: write parse_<county>(county_cfg) -> {method: {rep,dem,oth,npa}}
using method keys mail_voted / early_voted (/ election_day), register it in
PARSERS, and set "wired": true in config/az.json. Start with the biggest:
Maricopa (04013, ~60% of AZ voters), then Pima (04019), Pinal (04021).

Run:  python scripts/az_update.py [--force]
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "az"
CONFIG_PATH = os.path.join(ROOT, "config", "az.json")
GEO_PATH = os.path.join(ROOT, "assets", "az-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
METHODS = ["mail_voted", "early_voted", "election_day"]
VOTED_METHODS = ["mail_voted", "early_voted", "election_day"]


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# --- per-county parsers -------------------------------------------------------
# Each takes the county's config entry and returns {method_key: {rep,dem,oth,npa}}
# or None if unavailable. Wire these as each county's live feed is confirmed.
# (None registered yet -> every county reads 0, valid empty snapshot.)

def _stub(_county):
    return None


PARSERS = {
    # "04013": parse_maricopa,   # ~60% of AZ voters — wire first
    # "04019": parse_pima,
    # "04021": parse_pinal,
}


def county_entity(fips, methods):
    ent = {"fips": fips}
    for mkey, s in methods.items():
        if s:
            ent[mkey] = C.party_block(s.get("rep", 0), s.get("dem", 0), s.get("oth", 0), s.get("npa", 0))
    voted = [ent[m] for m in VOTED_METHODS if ent.get(m)]
    ent["cast"] = C.add_blocks(*voted) if voted else C.party_block(0, 0, 0, 0)
    return ent


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH)

    counties_out = {}
    wired = 0
    for county in cfg["source"]["counties"]:
        parser = PARSERS.get(county["fips"], _stub)
        try:
            methods = parser(county)
        except Exception as e:  # noqa: BLE001 - a bad county parser must not sink the rest
            print("  ! %s parser failed: %s" % (county["name"], str(e)[:60]), file=sys.stderr)
            methods = None
        if methods:
            counties_out[county["name"]] = county_entity(county["fips"], methods)
            wired += 1

    statewide = {}
    for mkey in METHODS:
        blocks = [counties_out[n][mkey] for n in counties_out if counties_out[n].get(mkey)]
        if blocks:
            statewide[mkey] = C.add_blocks(*blocks)
    voted = [statewide[m] for m in VOTED_METHODS if statewide.get(m)]
    statewide["cast"] = C.add_blocks(*voted) if voted else C.party_block(0, 0, 0, 0)
    methods_present = sorted({m for c in counties_out.values() for m in METHODS if c.get(m)})

    snap = {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"],
        "source": {"primary": "Arizona county recorders (aggregated), by registered party"},
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
        if cast["total"]:
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"generated_at": snap["generated_at"], "data_hash": snap["data_hash"],
                                    "cast": cast["total"], "margin": cast["margin"]}, separators=(",", ":")) + "\n")
        print("CHANGED  counties wired=%d/%d  cast=%s margin=%s" % (wired, len(cfg["source"]["counties"]), cast["total"], cast["margin"]))
    else:
        print("NOCHANGE  (hash %s)  counties wired=%d" % ((prev or "")[:12], wired))
    return 0


if __name__ == "__main__":
    sys.exit(main())
