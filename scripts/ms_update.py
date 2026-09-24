#!/usr/bin/env python3
"""Fetch Washington ballots-returned turnout by county (turnout-only; WA has no
party registration).

Source: WA Secretary of State "Ballot Return Statistics" (ballots returned & %
by county). WA is all-mail. No persistent machine-readable county feed was found
off-season -- the live numbers are published on a seasonal dashboard/report.

STATUS: staged skeleton. This writes an empty (0) turnout-only snapshot until a
verified live 2026 feed is wired; the Friday rollout routine finalizes the
parser against the live report when WA's general ballots start returning
(~mid-Oct 2026). Kept in the same schema as Virginia's turnout-only connector so
the front end treats it identically (green turnout-intensity map, no party UI).

Run:  python scripts/wa_update.py [--force]
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "ms"
CONFIG_PATH = os.path.join(ROOT, "config", "ms.json")
GEO_PATH = os.path.join(ROOT, "assets", "ms-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def fetch_counties(cfg):
    """Return {county_name: {fips, cast_total, registered}} from the live WA feed.

    PENDING: no verified machine-readable 2026 feed off-season. Returns {} for
    now (empty snapshot). The rollout routine wires the live parser in Oct.
    """
    return {}


def sw_block(t):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": t, "rep_pct": None,
            "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH)

    rows = fetch_counties(cfg)
    counties_out = {}
    reg_total = cast_total = 0
    for name, r in rows.items():
        ev = int(r.get("cast_total") or 0)
        reg = int(r.get("registered") or 0)
        blk = sw_block(ev)
        counties_out[name] = {"fips": r["fips"], "cast": blk, "early_voted": dict(blk),
                              "registered": reg,
                              "turnout_pct": (round(100.0 * ev / reg, 2) if reg else None)}
        reg_total += reg
        cast_total += ev

    statewide = {"cast": sw_block(cast_total), "early_voted": sw_block(cast_total),
                 "registered": reg_total,
                 "turnout_pct": (round(100.0 * cast_total / reg_total, 2) if reg_total else None)}
    snap = {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"],
        "partisan": False,
        "source": {"primary": "Mississippi ballot/turnout data (turnout-only)"},
        "source_compiled": "", "source_compiled_iso": (C.utc_now_iso() if cast_total else ""),
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
                f.write(json.dumps({"generated_at": snap["generated_at"], "cast": cast_total,
                                    "registered": reg_total, "data_hash": snap["data_hash"]},
                                   separators=(",", ":")) + "\n")
        print("CHANGED  counties=%d  cast=%s  turnout=%s%%"
              % (len(counties_out), cast_total, statewide["turnout_pct"]))
    else:
        print("NOCHANGE  (hash %s)" % (prev or "")[:12])
    return 0


if __name__ == "__main__":
    sys.exit(main())
