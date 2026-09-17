#!/usr/bin/env python3
"""Election-Day county turnout scraper (framework).

Runs on Election Day. For each target in config/eday_targets.json that has a
wired parser in PARSERS, it fetches election-day ballots-cast for that
jurisdiction and writes, per state, data/<state>/eday.json = {County: {fips,
total}}. The state connector merges that file as an `election_day` turnout total
(common.merge_eday), so the 10-minute workflow shows Election-Day turnout live.

These county/hub feeds are live ONLY on Nov 3 (URLs/formats unknown until then)
and are turnout totals, NOT by party. Parsers are wired near the election. With
no parser wired, a target yields nothing and no eday.json is written (safe no-op
off Election Day).

To wire a target: implement parse_<key>(target) -> int (election-day ballots
cast) or {County: total} for a state hub, and register it in PARSERS.

Run:  python scripts/eday_scrape.py [--force]
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

CONFIG_PATH = os.path.join(ROOT, "config", "eday_targets.json")


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# --- wired parsers (fill near the election; live-only feeds) -------------------
# County parser: parse_<key>(target) -> int (election-day ballots cast).
# State-hub parser: returns {County Name: int}. Return None if unavailable.

def _county_stub(_target):
    return None


def _hub_stub(_target):
    return None


PARSERS = {
    # "tx_harris":  parse_tx_harris,    # harrisvotes.com election-day turnout
    # "tx_generic": parse_tx_generic,   # county clerk election-day turnout
    # "ga_hub":     parse_ga_hub,       # GA SoS data hub election-day, per county
}


def main():
    force = "--force" in sys.argv  # noqa: F841 - reserved; scraper is idempotent
    cfg = load(CONFIG_PATH)
    per_state = {}   # state -> {County: {fips, total}}
    wired = 0
    for t in cfg["targets"]:
        parser = PARSERS.get(t.get("parser"))
        if not parser:
            continue
        try:
            result = parser(t)
        except Exception as e:  # noqa: BLE001 - one bad target must not sink the rest
            print("  ! %s/%s failed: %s" % (t["state"], t["name"], str(e)[:60]), file=sys.stderr)
            continue
        if result is None:
            continue
        wired += 1
        st = t["state"]
        bucket = per_state.setdefault(st, {})
        if t["kind"] == "state_hub" and isinstance(result, dict):
            for county, total in result.items():
                bucket[county] = {"fips": "", "total": int(total or 0)}
        else:
            bucket[t["name"]] = {"fips": t.get("fips", ""), "total": int(result or 0)}

    for st, counties in per_state.items():
        outdir = os.path.join(ROOT, "data", st)
        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, "eday.json"), "w", encoding="utf-8") as f:
            json.dump({"generated_at": C.utc_now_iso(), "counties": counties}, f, separators=(",", ":"))
        print("wrote data/%s/eday.json  counties=%d" % (st, len(counties)))

    if not per_state:
        print("no wired election-day targets produced data (expected off Election Day). targets=%d wired=%d"
              % (len(cfg["targets"]), wired))
    return 0


if __name__ == "__main__":
    sys.exit(main())
