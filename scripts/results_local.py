#!/usr/bin/env python3
"""Read the results feeds that only answer a home connection (Clarity Elections: Iowa, Colorado) and write
data/results/local/<ST>.json for the workflow's results update to pick up (results_feeds._local). Waits for each state's
polls to close, like every other feed. Run on Sam's PC by local_ga_reader.py on election night; by hand it needs no
arguments and writes only those files.

Run:  python scripts/results_local.py
"""
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import results_feeds as F  # noqa: E402


def read_all():
    """-> list of files written."""
    wrote = []
    os.makedirs(F.LOCAL_DIR, exist_ok=True)
    for usps in F.CLARITY:
        if not F.polls_closed(usps):
            continue
        try:
            races, measures = F.clarity_parse(usps)
        except Exception as e:  # noqa: BLE001 - the workflow keeps the last figures
            print("%s: %s" % (usps, str(e)[:120]))
            continue
        path = os.path.join(F.LOCAL_DIR, usps + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"fetched": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "races": races, "measures": measures}, f, separators=(",", ":"))
        wrote.append(path)
        print("%s: %d races, %d measures" % (usps, len(races), len(measures)))
    return wrote


if __name__ == "__main__":
    read_all()
