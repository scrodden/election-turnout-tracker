#!/usr/bin/env python3
"""Refresh civicAPI's Nov 3 race and measure rows into $CIVIC_CACHE (results_feeds.civic_rows).

results_eday.yml runs this in the background, again each time it finishes: a pass pages through every year's races
on civicAPI (no date filter) and takes minutes, so the 2-minute results cycle reads the latest copy instead.

Run:  CIVIC_CACHE=/tmp/civic_rows.json python scripts/civic_refresh.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import results_feeds as F  # noqa: E402


def main():
    if not F.CIVIC_CACHE:
        print("civic refresh: CIVIC_CACHE isn't set; nothing to do")
        return 0
    t = time.time()
    try:
        rows, mrows = F.civic_rows(refresh=True)
    except Exception as e:  # noqa: BLE001 - the last copy stays
        print("civic refresh failed after %.0fs: %s" % (time.time() - t, str(e)[:160]))
        return 1
    print("civic refresh: %d races, %d measures in %.0fs" % (len(rows), len(mrows), time.time() - t))
    return 0


if __name__ == "__main__":
    sys.exit(main())
