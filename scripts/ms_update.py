#!/usr/bin/env python3
"""Mississippi absentee ballots (turnout-only; MS has no party registration).

Mississippi has no no-excuse early voting: ballots cast before Election Day are
absentee, by mail or in person at the circuit clerk's office. The Secretary of
State (sos.ms.gov, which blocks scripted requests) publishes no absentee
statistics page, so this shows the UF Election Lab's statewide Mississippi
figures (the Lab gets them from the Secretary of State): absentee ballots
requested and returned. Used unaltered, attributed, CC BY-NC-ND 4.0. The Lab
has no Mississippi county file yet; lab_standin picks one up automatically if
it appears (and it reconciles with the statewide row).

If an official county-level feed turns up, parse it here first and keep the
Lab as the fallback (see ky_update.py / il_update.py for the pattern).

Run:  python scripts/ms_update.py [--force]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import lab_standin as LAB  # noqa: E402

STATE = "ms"
CONFIG_PATH = os.path.join(ROOT, "config", "ms.json")
GEO_PATH = os.path.join(ROOT, "assets", "ms-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")


def main():
    force = "--force" in sys.argv
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    os.makedirs(DATA_DIR, exist_ok=True)
    if not LAB.run(STATE, cfg, GEO_PATH, LATEST_PATH, HISTORY_PATH, partisan=False, force=force,
                   role="stand-in until an official Mississippi feed is wired"):
        print("ms: no Election Lab figures for Mississippi yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
