#!/usr/bin/env python3
"""Fetch Virginia early-vote turnout by locality (turnout-only; VA has no party
registration). Source: VPAP's per-election early-vote-by-locality TopoJSON
(properties: locality, early_votes, perc_early_voters). We only read the
properties (own geometry is used for the map).

The 2026 general file publishes when VA early voting opens (~Sept 18); until then
this reads empty. Locality matching handles VA's county / independent-city name
collisions using FIPS-derived type (independent cities are FIPS >= 51510).

Run:  python scripts/va_update.py [--force]
      python scripts/va_update.py --validate   (prove the matcher on the sample file)
"""
import os
import re
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "va"
CONFIG_PATH = os.path.join(ROOT, "config", "va.json")
GEO_PATH = os.path.join(ROOT, "assets", "va-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def base_key(name):
    n = name.lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]", "", n)


def geo_index():
    """(base, type) -> {name, fips} from VA geometry; type by FIPS (city>=51510)."""
    geo = load(GEO_PATH)
    idx = {}
    for ft in geo["features"]:
        name = ft["properties"]["name"]; fips = ft["properties"]["fips"]
        typ = "city" if int(fips) >= 51510 else "county"
        b = name.lower()
        if typ == "city":
            b = re.sub(r"\s+city$", "", b)      # Census names cities "X city"
        idx[(base_key(b), typ)] = {"name": name, "fips": fips}
    return idx


def match_locality(vpap_name, idx):
    n = vpap_name.strip().lower()
    if n.endswith(" county"):
        return idx.get((base_key(n[:-7]), "county"))
    if n.endswith(" city"):
        return idx.get((base_key(n[:-5]), "city"))
    b = base_key(n)   # bare -> prefer county, else city
    return idx.get((b, "county")) or idx.get((b, "city"))


def fetch_dataset(cfg, validate):
    base = cfg["source"]["base"]; ref = cfg["source"]["referer"]
    names = [cfg["source"]["validation_file"]] if validate else cfg["source"]["general_candidates"]
    for fn in names:
        try:
            d = json.loads(C.http_get(base + fn, referer=ref, no_cache=True, retries=2))
            return fn, d
        except Exception:  # noqa: BLE001
            continue
    return None, None


def main():
    force = "--force" in sys.argv
    validate = "--validate" in sys.argv
    cfg = load(CONFIG_PATH)
    idx = geo_index()

    fn, d = fetch_dataset(cfg, validate)
    rows = (d.get("objects", {}).get("fipses", {}).get("geometries", []) if d else [])
    counties_out = {}
    reg_total = cast_total = 0
    unmatched = []
    for g in rows:
        p = g.get("properties", {})
        loc = p.get("locality")
        m = match_locality(loc, idx) if loc else None
        if not m:
            if loc:
                unmatched.append(loc)
            continue
        ev = int(p.get("early_votes") or 0)
        perc = p.get("perc_early_voters")
        reg = int(round(ev / perc)) if perc else 0
        blk = C.party_block(0, 0, 0, ev)   # turnout-only: total only (stored in npa slot? no)
        blk = {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": ev,
               "rep_pct": None, "dem_pct": None, "npa_pct": None, "oth_pct": None,
               "margin": None, "compiled": "", "compiled_iso": ""}
        counties_out[m["name"]] = {"fips": m["fips"], "cast": blk, "early_voted": dict(blk),
                                   "registered": reg, "turnout_pct": (round(perc * 100, 2) if perc else None)}
        reg_total += reg; cast_total += ev

    if validate:
        print("Matcher validation on %s: matched %d/%d, unmatched=%s"
              % (fn, len(counties_out), len(rows), unmatched[:20]))
        return 0

    def sw_block(t):
        return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": t, "rep_pct": None,
                "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}
    statewide = {"cast": sw_block(cast_total), "early_voted": sw_block(cast_total),
                 "registered": reg_total,
                 "turnout_pct": (round(100.0 * cast_total / reg_total, 2) if reg_total else None)}
    snap = {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"],
        "partisan": False,
        "source": {"primary": "VPAP early-vote-by-locality (turnout-only; VA has no party registration)"},
        "source_compiled": (d.get("updated") if d else "") or "",
        "source_compiled_iso": C.utc_now_iso() if d else "",
        "methods_present": (["early_voted"] if cast_total else []),
        "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties_out,
    }
    snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k != "source_compiled_iso"})
    snap["generated_at"] = C.utc_now_iso()

    prev = None
    if os.path.exists(LATEST_PATH):
        try:
            prev = load(LATEST_PATH).get("data_hash")
        except (ValueError, OSError):
            pass
    changed = force or (snap["data_hash"] != prev)
    os.makedirs(DATA_DIR, exist_ok=True)
    if changed:
        with open(LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        if cast_total:
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"generated_at": snap["generated_at"], "compiled": snap["source_compiled"],
                                    "data_hash": snap["data_hash"], "cast": cast_total, "registered": reg_total},
                                   separators=(",", ":")) + "\n")
        print("CHANGED  file=%s  localities=%d  early_cast=%s  turnout=%s%%"
              % (fn or "(none yet)", len(counties_out), cast_total, statewide["turnout_pct"]))
    else:
        print("NOCHANGE  (hash %s)" % (prev or "")[:12])
    if unmatched:
        print("  unmatched localities:", unmatched[:10])
    return 0


if __name__ == "__main__":
    sys.exit(main())
