#!/usr/bin/env python3
"""Delaware mail ballots by county, state house / senate district and party.

Source: the UF Election Lab's Delaware files (DE_county.csv, DE_sdl.csv,
DE_sdu.csv; the Lab's source is the Delaware Department of Elections): mail
ballots received and counted, by party (DEM / REP / other). Delaware doesn't
publish mail-ballot requests (the Lab's "requested" just repeats ballots
received), so there's no outstanding count or ballot chase (config
lab_no_requests). Delaware has one at-large U.S. House seat, so the district
maps are the state House (41) and Senate (21). Used unaltered, attributed,
under CC BY-NC-ND 4.0. elections.delaware.gov publishes registration totals
only (checked 2026-10-03).

Run:  python scripts/de_update.py [--force]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import lab_standin as LAB  # noqa: E402

STATE = "de"
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
    for kind, suffix in (("sdl", "sh"), ("sdu", "ss")):   # state house / senate maps, ~hourly
        LAB.districts(STATE, cfg, os.path.join(ROOT, "assets", "de-%s.geojson" % suffix),
                      os.path.join(DATA_DIR, "districts_%s.json" % suffix), partisan=True, force=force, kind=kind)
    if not LAB.run(STATE, cfg, GEO_PATH, LATEST_PATH, HISTORY_PATH, partisan=True, force=force,
                   role="stand-in until an official Delaware feed is wired"):
        print("ne: no Election Lab figures for Delaware yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
