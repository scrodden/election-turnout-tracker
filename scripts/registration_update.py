#!/usr/bin/env python3
"""Build data/registration.json — statewide voter REGISTRATION by party for each
party-registration state, the denominator for the Registration-vs-Turnout page.

Registration is published year-round, so each state's numbers are wired from its
official registration-statistics source (config/registration_sources.json). A
per-state parser goes in SOURCES[code] and returns {rep,dem,npa,oth,as_of}; states
without a wired parser are omitted (no fabrication). Idempotent; run each cycle.

To wire a state: implement parse_<code>() -> {"rep":int,"dem":int,"npa":int,
"oth":int,"as_of":"YYYY-MM-DD"} using its source, register in SOURCES.

Run:  python scripts/registration_update.py
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATES_PATH = os.path.join(ROOT, "assets", "states.json")
SRC_PATH = os.path.join(ROOT, "config", "registration_sources.json")
OUT_PATH = os.path.join(ROOT, "data", "registration.json")


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_days(iso):
    from datetime import datetime, timezone
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return 1e9


# SOURCES[code] -> function() -> {"rep","dem","npa","oth","as_of"} (wired per state)
SOURCES = {}


def main():
    force = "--force" in sys.argv
    # Registration changes ~monthly, so refresh at most weekly even though the
    # workflow calls this every cycle (keeps heavy sources, e.g. NC's voter file,
    # from being fetched constantly). --force overrides.
    existing = load(OUT_PATH)
    if existing and existing.get("states") and not force and age_days(existing.get("generated_at", "")) < 6.5:
        print("registration.json is fresh (<6.5 days) — skipping (refreshes ~weekly).")
        return 0
    reg = load(STATES_PATH, {}) or {}
    srcmap = (load(SRC_PATH, {}) or {}).get("sources", {})
    out = {}
    for s in reg.get("states", []):
        code = s.get("code")
        if s.get("partisan", True) is False:
            continue                      # turnout-only states have no party registration
        fn = SOURCES.get(code)
        if not fn:
            continue
        try:
            r = fn() or {}
        except Exception as e:  # noqa: BLE001
            print("  ! %s registration failed: %s" % (code, str(e)[:60]), file=sys.stderr)
            continue
        rep = int(r.get("rep", 0)); dem = int(r.get("dem", 0)); npa = int(r.get("npa", 0)); oth = int(r.get("oth", 0))
        tot = rep + dem + npa + oth
        if tot <= 0:
            continue
        out[code] = {"rep": rep, "dem": dem, "npa": npa, "oth": oth, "total": tot,
                     "as_of": r.get("as_of", ""), "source": srcmap.get(code, "")}
    doc = {"generated_at": now(), "note": "Statewide voter registration by party; the denominator for the Registration-vs-Turnout comparison. Wired per state from official registration-statistics sources.", "states": out}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    print("registration.json: %d states with party-registration data%s" %
          (len(out), (" (" + ", ".join(sorted(out)) + ")") if out else " — none wired yet"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
