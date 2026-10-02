#!/usr/bin/env python3
"""Connecticut absentee / early ballots, statewide by registered party.

Source: the UF Election Lab's Connecticut row (its source is the Connecticut
Secretary of the State): ballots requested and returned by party (DEM / REP /
unaffiliated). Used unaltered, attributed, under CC BY-NC-ND 4.0.

The Lab's CT_county.csv is keyed by voters' mailing city (232 names: towns
plus villages and post offices such as Mystic, Cos Cob, Sandy Hook, some of
which span two towns), so it doesn't line up with Connecticut's 169 towns and
isn't mapped; lab_standin shows statewide figures and switches to the town map
(assets/ct-towns.geojson) automatically if every Lab row matches a town.
data.ct.gov has no 2026 absentee dataset (checked 2026-10-02).

Run:  python scripts/ct_update.py [--force]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import lab_standin as LAB  # noqa: E402

STATE = "ct"
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
    if not LAB.run(STATE, cfg, GEO_PATH, LATEST_PATH, HISTORY_PATH, partisan=True, force=force,
                   role="stand-in until an official Connecticut feed is wired"):
        print("wy: no Election Lab figures for Connecticut yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
