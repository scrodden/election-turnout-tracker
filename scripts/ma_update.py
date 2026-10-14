#!/usr/bin/env python3
"""Massachusetts mail / early ballots by city and town.

Source: the UF Election Lab's Massachusetts files (its source is the Secretary
of the Commonwealth's early and mail voting statistics): mail ballots
requested and returned, and early in-person ballots once early voting opens
(October 17), statewide (US.csv) and for all 351 cities and towns
(MA_county.csv, keyed by municipality). Used unaltered, attributed, under
CC BY-NC-ND 4.0. Mapped on assets/ma-towns.geojson (python scripts/build_town_geo.py ma 25).

Turnout only for now: the Secretary also posts a by-party sheet, but its party
"Cast" figures don't reconcile with its own overall totals (the Lab has asked
the state about it, 2026-10-13), so the Lab — and this site — use only the
overall statistics. If the Lab adds party columns, lab_standin picks them up
automatically. The Secretary's own pages sit behind an hCaptcha challenge, so
they aren't fetched directly.

Run:  python scripts/ma_update.py [--force]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import lab_standin as LAB  # noqa: E402

STATE = "ma"
CONFIG_PATH = os.path.join(ROOT, "config", STATE + ".json")
GEO_PATH = os.path.join(ROOT, "assets", STATE + "-towns.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")


def main():
    force = "--force" in sys.argv
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    os.makedirs(DATA_DIR, exist_ok=True)
    if not LAB.run(STATE, cfg, GEO_PATH, LATEST_PATH, HISTORY_PATH, partisan=True, force=force,
                   role="stand-in until the Secretary of the Commonwealth's own feed can be read"):
        print("ma: no Election Lab figures for Massachusetts yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
