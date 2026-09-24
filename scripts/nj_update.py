#!/usr/bin/env python3
"""New Jersey mail (and, from Oct 24, in-person early) ballots by party --
STATEWIDE ONLY for now.

New Jersey's Division of Elections does not publish pre-election mail-ballot
counts; its county 'periodic reports' start on Election Day. The UF Election
Lab (Dr. Michael McDonald) receives the state's figures directly and publishes
them daily in its early-vote tracker CSV
(election.lab.ufl.edu/data-downloads/earlyvote/2026/US.csv). We use the New
Jersey row exactly as published, with attribution, under the Lab's terms
(CC BY-NC-ND 4.0: credit the Lab, no commercial use, numbers unaltered).

Row fields used: request_{dem,rep,none,all}, accept_{...} (mail ballots
returned & accepted), inperson_{...}, voted_{...}, last_update.
Democratic->dem, Republican->rep, unaffiliated ('none')->npa; any remainder of
*_all over the three -> oth. mail_provided = requested - returned (outstanding)
-> ballot chase.

If county sources are wired later (COUNTY_SOURCES), they can replace this.

Run:  python scripts/nj_update.py [--force]
"""
import csv
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "nj"
CONFIG_PATH = os.path.join(ROOT, "config", "nj.json")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _int(s):
    s = str(s or "").strip().replace(",", "")
    try:
        return int(float(s)) if s and s.upper() != "NA" else 0
    except ValueError:
        return 0


def _parties(row, prefix):
    d, r, n, a = (_int(row.get("%s_%s" % (prefix, k))) for k in ("dem", "rep", "none", "all"))
    return {"dem": d, "rep": r, "npa": n, "oth": max(0, a - d - r - n)}


def _pb(p):
    return C.party_block(p["rep"], p["dem"], p["oth"], p["npa"])


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg.get("source", {})
    try:
        text = C.http_get(src["lab_csv"], no_cache=True)
        row = next((r for r in csv.DictReader(io.StringIO(text)) if (r.get("state_abbv") or "").upper() == "NJ"), None)
    except Exception as e:  # noqa: BLE001
        print("NJ: Election Lab CSV unavailable: %s" % str(e)[:120], file=sys.stderr)
        return 0
    if not row or not _int(row.get("request_all") or row.get("voted_all")):
        print("NJ: no New Jersey figures in the Election Lab file yet.")
        return 0

    req, ret, inp = _parties(row, "request"), _parties(row, "accept"), _parties(row, "inperson")
    out = {k: max(0, req[k] - ret[k]) for k in req}
    statewide = {"mail_voted": _pb(ret), "mail_provided": _pb(out), "early_voted": _pb(inp),
                 "cast": _pb({k: ret[k] + inp[k] for k in ret}), "registered": 0, "turnout_pct": None}
    m = C.compute_mail(statewide)
    if m:
        statewide["mail"] = m
    methods_present = [k for k in ("mail_voted", "early_voted", "mail_provided") if statewide[k]["total"]]
    as_of = row.get("last_update", "")
    snap = {
        "state": STATE, "state_name": cfg.get("state_name", "New Jersey"), "election": cfg.get("election", {}),
        "partisan": True, "statewide_only": True,
        "source": {"primary": "UF Election Lab early-vote tracker (M. McDonald), New Jersey Division of Elections data; CC BY-NC-ND 4.0",
                   "url": src.get("lab_page"), "as_of": as_of},
        "source_compiled": as_of, "source_compiled_iso": C.utc_now_iso(),
        "methods_present": methods_present, "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": {},
    }
    prev = load(LATEST_PATH, {}) or {}
    snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k != "source_compiled_iso"})
    snap["generated_at"] = C.utc_now_iso()
    if not force and snap["data_hash"] == prev.get("data_hash"):
        print("NOCHANGE  (returned=%d as of %s)" % (statewide["cast"]["total"], as_of))
        return 0
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    c = statewide["cast"]
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"generated_at": snap["generated_at"],
                            "statewide": {"cast": [c["rep"], c["dem"], c["oth"], c["npa"], c["total"]]}},
                           separators=(",", ":")) + "\n")
    print("CHANGED  as of %s: returned=%d of %d requested (R%d D%d NPA%d) margin=%s"
          % (as_of, c["total"], (m or {}).get("requested", 0), c["rep"], c["dem"], c["npa"], c["margin"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
