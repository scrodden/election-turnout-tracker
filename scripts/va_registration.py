#!/usr/bin/env python3
"""Virginia registered (active) voters by U.S. House district and by locality,
from VA ELECT's monthly registration-statistics CSVs:

  Daily_Registrant_Count_By_Congressional_<YYYY_MM_DD_hhmmss>.csv
      one row per precinct; District 'CON 01'..'CON 11' and a per-district
      TotalActiveVotersDistrict (repeated on every row of the district)
  Daily_Registrant_Count_By_Locality_<...>.csv
      one row per precinct; 'Locality: 001 ACCOMACK COUNTY' (3-digit county
      FIPS) and a per-locality TotalActiveVotersLocality, plus StateTotalLocality

Newest files are discovered from the year's registration-statistics page.
Cached in data/va/registered.json and refreshed at most every REFRESH_DAYS
(the files are monthly). Used as the turnout denominator by va_update.py
(districts) and va_locality_update.py (localities).

Run:  python scripts/va_registration.py [--force]
"""
import csv
import io
import json
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

BASE = "https://www.elections.virginia.gov"
PAGE = BASE + "/resultsreports/registration-statistics/%d-registration-statistics/"
CACHE_PATH = os.path.join(ROOT, "data", "va", "registered.json")
REFRESH_DAYS = 3


def _int(s):
    s = str(s or "").replace(",", "").strip()
    return int(s) if s.isdigit() else 0


def _newest(links, kind):
    """Pick the newest '<kind>_YYYY_MM_DD_*.csv' link by its date stamp."""
    best = None
    for href in links:
        m = re.search(r"%s_(\d{4})_(\d{2})_(\d{2})_\d+\.csv$" % kind, href)
        if m:
            key = m.groups()
            if best is None or key > best[0]:
                best = (key, href)
    return (BASE + best[1] if best[1].startswith("/") else best[1], "%s-%s-%s" % best[0]) if best else (None, None)


def discover():
    links = []
    year = datetime.now(timezone.utc).year
    for y in (year, year - 1):
        try:
            links += re.findall(r'href="([^"]+\.csv)"', C.http_get(PAGE % y, retries=2))
        except Exception:  # noqa: BLE001
            continue
        if links:
            break
    return (_newest(links, "Daily_Registrant_Count_By_Congressional"),
            _newest(links, "Daily_Registrant_Count_By_Locality"))


def _rows(url):
    text = C.http_get(url, retries=2).lstrip("﻿")
    return list(csv.DictReader(io.StringIO(text)))


def fetch():
    (cd_url, cd_asof), (loc_url, loc_asof) = discover()
    if not cd_url or not loc_url:
        raise RuntimeError("VA registration CSVs not found")
    # Sum the per-precinct active counts ourselves: the files' "Total..." column
    # headers are shifted relative to the data (e.g. 'StateTotalLocality' holds
    # the locality count), so they're only used as a cross-check.
    cds, cd_reported = {}, {}
    for r in _rows(cd_url):
        m = re.match(r"CON\s*(\d+)", (r.get("District") or "").strip())
        if m:
            k = "CD%d" % int(m.group(1))
            cds[k] = cds.get(k, 0) + _int(r.get("ACTIVE_VOTERS_1"))
            cd_reported[k] = _int(r.get("TotalActiveVotersDistrict"))
    locs = {}
    for r in _rows(loc_url):
        m = re.match(r"Locality:\s*(\d{3})\s+(.+)", (r.get("Locality") or "").strip())
        if m:
            e = locs.setdefault("51" + m.group(1), {"name": m.group(2).strip().title(), "active": 0})
            e["active"] += _int(r.get("ActiveVoters"))
    if len(cds) != 11 or len(locs) < 130:
        raise RuntimeError("VA registration parse looks wrong: %d CDs, %d localities" % (len(cds), len(locs)))
    bad = [k for k in cds if cd_reported.get(k) and cd_reported[k] != cds[k]]
    if bad:
        raise RuntimeError("VA registration: precinct sums disagree with district totals for %s" % bad)
    loc_sum = sum(v["active"] for v in locs.values())
    return {"as_of": loc_asof, "cd_as_of": cd_asof, "measure": "active registered voters",
            "source": {"congressional": cd_url, "locality": loc_url},
            "statewide_active": loc_sum, "cd": cds, "locality": locs,
            "fetched_at": C.utc_now_iso()}


def get(force=False):
    """Cached registration, refreshed when older than REFRESH_DAYS. Returns the
    cached copy (possibly stale) if a refresh fails, or None if there is none."""
    cur = None
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            cur = json.load(f)
    except (OSError, ValueError):
        pass
    fresh = False
    if cur and not force:
        try:
            t = datetime.strptime(cur["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            fresh = (datetime.now(timezone.utc) - t).days < REFRESH_DAYS
        except (KeyError, ValueError):
            pass
    if fresh:
        return cur
    try:
        new = fetch()
    except Exception as e:  # noqa: BLE001
        print("VA registration refresh failed: %s" % str(e)[:140], file=sys.stderr)
        return cur
    if cur and {k: v for k, v in cur.items() if k != "fetched_at"} == {k: v for k, v in new.items() if k != "fetched_at"}:
        new["fetched_at"] = cur["fetched_at"] if not force else new["fetched_at"]
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(new, f, separators=(",", ":"))
    return new


if __name__ == "__main__":
    r = get(force="--force" in sys.argv)
    if r:
        print("VA registration as of %s: %s active; CDs %s; %d localities"
              % (r["as_of"], "{:,}".format(r["statewide_active"]), sum(r["cd"].values()), len(r["locality"])))
