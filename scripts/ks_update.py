#!/usr/bin/env python3
"""Kansas advance ballots by county and registered party.

Source: the UF Election Lab's Kansas files (its source is the Kansas Secretary
of State): mail ballots requested and returned and in-person advance ballots,
by registered party (Democratic / Republican / unaffiliated; other parties =
the rest), statewide (US.csv), by county (KS_county.csv) and by U.S. House
district (KS_cd.csv, for the Districts map). Used unaltered, attributed,
under CC BY-NC-ND 4.0.

The Secretary of State's own page (sos.ks.gov/elections/advance-voting-data.html)
embeds a Power BI report with only a statewide daily series (sent / returned /
in person) -- no county and no party -- so it isn't used.

Run:  python scripts/ks_update.py [--force]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import lab_standin as LAB  # noqa: E402

STATE = "ks"
CONFIG_PATH = os.path.join(ROOT, "config", STATE + ".json")
GEO_PATH = os.path.join(ROOT, "assets", STATE + "-counties.geojson")
CD_GEO_PATH = os.path.join(ROOT, "assets", STATE + "-cd.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
DISTRICTS_PATH = os.path.join(DATA_DIR, "districts.json")


def main():
    force = "--force" in sys.argv
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    os.makedirs(DATA_DIR, exist_ok=True)
    LAB.districts(STATE, cfg, CD_GEO_PATH, DISTRICTS_PATH, partisan=True, force=force)   # ~hourly
    if not LAB.run(STATE, cfg, GEO_PATH, LATEST_PATH, HISTORY_PATH, partisan=True, force=force,
                   role="stand-in until the Secretary of State publishes county-by-party figures"):
        print("ks: no Election Lab figures for Kansas yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
