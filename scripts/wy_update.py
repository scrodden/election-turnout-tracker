#!/usr/bin/env python3
"""Wyoming absentee / early ballots by county and registered party.

Source: the UF Election Lab's Wyoming county file (WY_county.csv; the Lab's
source is the Wyoming Secretary of State): absentee ballots requested and
returned by party. Wyoming has one at-large U.S. House seat, so there is no
Districts map.
Used unaltered, attributed, under CC BY-NC-ND 4.0.

If an official county-level feed turns up, parse it here first and keep the
Lab as the fallback (see ky_update.py / il_update.py for the pattern).

Run:  python scripts/wy_update.py [--force]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import lab_standin as LAB  # noqa: E402

STATE = "wy"
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
    if not LAB.run(STATE, cfg, GEO_PATH, LATEST_PATH, HISTORY_PATH, partisan=True, force=force,
                   role="stand-in until an official Wyoming feed is wired"):
        print("wy: no Election Lab figures for Wyoming yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
