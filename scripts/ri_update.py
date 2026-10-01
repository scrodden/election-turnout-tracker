#!/usr/bin/env python3
"""Rhode Island mail ballots by city/town and registered party.

Source: the UF Election Lab's Rhode Island file (RI_county.csv, which is by the
39 cities and towns that run RI elections; the Lab's source is the RI
Secretary of State): mail ballots requested and returned by party (DEM / REP /
unaffiliated). The map is by city/town (assets/ri-towns.geojson, Census county
subdivisions); the Lab's RI_cd.csv feeds the Districts map. Used unaltered,
attributed, under CC BY-NC-ND 4.0.

RI's own outlets checked 2026-10-01: the Department of State data hub has
registration only; its ArcGIS "RI Voter Turnout Tracker" hasn't been set up for
the general. If an official feed appears, parse it here first and keep the Lab
as the fallback (see ky_update.py / il_update.py for the pattern).

Run:  python scripts/ri_update.py [--force]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import lab_standin as LAB  # noqa: E402

STATE = "ri"
CONFIG_PATH = os.path.join(ROOT, "config", STATE + ".json")
GEO_PATH = os.path.join(ROOT, "assets", STATE + "-towns.geojson")
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
                   role="stand-in until an official Rhode Island feed is wired"):
        print("ne: no Election Lab figures for Rhode Island yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
