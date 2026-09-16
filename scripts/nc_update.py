#!/usr/bin/env python3
"""Fetch North Carolina turnout by county and party from the NCSBE voter-level
absentee file (mail + one-stop early voting). Writes data/nc/latest.json in the
same schema as Florida so the front end reuses everything.

Source: https://s3.amazonaws.com/dl.ncsbe.gov/ENRS/2026_11_03/absentee_20261103.zip
Each row = one RETURNED absentee/early ballot (NC law: request info isn't public
until returned), with county_desc, voter_party_code, ballot_req_type
(MAIL / EARLY VOTING), ballot_rtn_status, precinct_desc, etc. We aggregate to
county x party x method. There is no "mail outstanding" metric for NC.

Run:  python scripts/nc_update.py [--force]
"""
import os
import re
import sys
import io
import csv
import json
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "nc"
CONFIG_PATH = os.path.join(ROOT, "config", "nc.json")
COUNTIES_PATH = os.path.join(ROOT, "config", "nc_counties.json")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
VOTED_METHODS = ["mail_voted", "early_voted"]
_TODAY = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_date(s):
    s = (s or "").strip()
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s)
    return "%s-%s-%s" % (m.group(3), m.group(1), m.group(2)) if m else ""


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH)
    counties = load(COUNTIES_PATH)["counties"]
    by_code = {c["code"]: c for c in counties}   # code = UPPERCASE county name
    party_map = cfg["party_map"]
    method_map = cfg["method_map"]

    print("Fetching NCSBE absentee file...")
    raw = C.http_get(cfg["source"]["absentee_zip"], binary=True, no_cache=True, timeout=120)
    z = zipfile.ZipFile(io.BytesIO(raw))
    text = z.read(z.namelist()[0]).decode("latin-1")
    rows = list(csv.DictReader(io.StringIO(text)))
    print("  %d ballot records" % len(rows))

    # aggregate county -> method -> party counts
    agg = {}
    max_rtn = ""
    for r in rows:
        code = (r.get("county_desc") or "").strip().upper()
        mkey = method_map.get((r.get("ballot_req_type") or "").strip().upper())
        if not code or not mkey or code not in by_code:
            continue
        tgt = party_map.get((r.get("voter_party_code") or "").strip().upper(), "oth")
        slot = agg.setdefault(code, {}).setdefault(mkey, {"rep": 0, "dem": 0, "oth": 0, "npa": 0})
        slot[tgt] += 1
        d = parse_date(r.get("ballot_rtn_dt"))
        if d and d <= _TODAY and d > max_rtn:  # ignore stray future-dated records
            max_rtn = d

    # build snapshot
    counties_out = {}
    for c in counties:
        methods = agg.get(c["code"], {})
        ent = {"fips": c["fips"]}
        for mkey, s in methods.items():
            ent[mkey] = C.party_block(s["rep"], s["dem"], s["oth"], s["npa"])
        voted = [ent[m] for m in VOTED_METHODS if ent.get(m)]
        ent["cast"] = C.add_blocks(*voted) if voted else C.party_block(0, 0, 0, 0)
        counties_out[c["name"]] = ent

    statewide = {}
    for mkey in VOTED_METHODS:
        blocks = [counties_out[n][mkey] for n in counties_out if counties_out[n].get(mkey)]
        if blocks:
            statewide[mkey] = C.add_blocks(*blocks)
    voted = [statewide[m] for m in VOTED_METHODS if statewide.get(m)]
    statewide["cast"] = C.add_blocks(*voted) if voted else C.party_block(0, 0, 0, 0)

    methods_present = sorted({m for c in counties_out.values() for m in VOTED_METHODS if c.get(m)})
    snap = {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"],
        "source": {"primary": "NCSBE voter-level absentee file (returned ballots; mail + early)"},
        "source_compiled": max_rtn, "source_compiled_iso": (max_rtn + "T00:00:00") if max_rtn else "",
        "methods_present": methods_present, "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties_out,
    }
    snap["data_hash"] = C.data_hash(snap)
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
        rec = {"generated_at": snap["generated_at"], "compiled": max_rtn, "data_hash": snap["data_hash"],
               "statewide": {m: [b["rep"], b["dem"], b["oth"], b["npa"], b["total"]]
                             for m, b in statewide.items() if b.get("total")}}
        append = True
        if os.path.exists(HISTORY_PATH):
            try:
                with open(HISTORY_PATH, "rb") as f:
                    try:
                        f.seek(-2048, os.SEEK_END)
                    except OSError:
                        f.seek(0)
                    tail = f.read().decode("utf-8", "replace").strip().splitlines()
                if tail and json.loads(tail[-1]).get("data_hash") == rec["data_hash"]:
                    append = False
            except (ValueError, OSError):
                pass
        if append:
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        cast = statewide["cast"]
        print("CHANGED  through=%s  cast=%s (R%s D%s NPA%s) margin=%s"
              % (max_rtn, cast["total"], cast["rep"], cast["dem"], cast["npa"], cast["margin"]))
    else:
        print("NOCHANGE  through=%s (hash %s)" % (max_rtn, (prev or "")[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
