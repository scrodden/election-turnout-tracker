#!/usr/bin/env python3
"""New Mexico absentee + early in-person ballots by county and registered party.

Source: the UF Election Lab's New Mexico files (US.csv statewide row and
NM_county.csv; the Lab's source is the New Mexico Secretary of State):
absentee ballots requested and accepted, and early in-person ballots, by
county and party (DEM / REP / other). Used unaltered, attributed, under
CC BY-NC-ND 4.0, as a stand-in for the SoS Voter Turnout portal
(electionresults.sos.state.nm.us / electionresults.sos.nm.gov, one server):
checked 2026-10-07, it resets every connection from scripts and from a
browser pane (TLS handshake closed by the remote host), so it can't be read
or verified from here. If that portal becomes reachable, wire it here and the
stand-in drops out.

Run:  python scripts/nm_update.py [--force]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import lab_standin as LAB  # noqa: E402

STATE = "nm"
CONFIG_PATH = os.path.join(ROOT, "config", STATE + ".json")
GEO_PATH = os.path.join(ROOT, "assets", STATE + "-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")


def main():
    force = "--force" in sys.argv
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    os.makedirs(DATA_DIR, exist_ok=True)
    if not LAB.run(STATE, cfg, GEO_PATH, LATEST_PATH, HISTORY_PATH, partisan=True, force=force,
                   role="stand-in while the SoS Voter Turnout portal is unreachable"):
        print("nm: no Election Lab figures for New Mexico yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
