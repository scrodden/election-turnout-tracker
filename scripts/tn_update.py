#!/usr/bin/env python3
"""Tennessee early & absentee ballots by county (turnout-only; Tennessee has no
party registration).

Source: the Tennessee Secretary of State's daily "Early and Absentee Voters"
report, linked from https://sos.tn.gov/govotetn ("Early Voting Turnout: View
totals by county: PDF | XLSX") as
  sos-prod.tnsosgovfiles.com/s3fs-public/document/<YYYYMMDD>Day<N>Turnout.xlsx
Sheet "EarlyAbsentee": County, Date (Excel serial), Day Total, with a
"Statewide" row. Per the report's footnote, the totals "include in-person early
voters, absentee by-mail ballots submitted by voters, and voters at licensed
facilities" -- every ballot cast so far, not split by method.

Each run reads the newest DayN file listed on the page. If it holds only its
own day (rather than every day so far), the earlier DayN files are read too and
the days are summed; every day's county rows must add up to that day's
Statewide row. If the state's report can't be read or doesn't check out, the
UF Election Lab's Tennessee figures (built from the same report) are used.

Early voting: October 14 - 29, 2026.

Run:  python scripts/tn_update.py [--force]
"""
import json
import os
import re
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "tn"
CONFIG_PATH = os.path.join(ROOT, "config", "tn.json")
GEO_PATH = os.path.join(ROOT, "assets", "tn-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
PAGE = "https://sos.tn.gov/govotetn"
FILE_RE = r"(https://sos-prod\.tnsosgovfiles\.com/s3fs-public/document/%s)Day(\d+)Turnout\.xlsx"


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower().replace(" county", ""))


def _int(v):
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _day(v):
    """Excel serial (46309) or ISO text -> date."""
    n = _int(v)
    if n and n > 30000:
        return date(1899, 12, 30) + timedelta(days=n)
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def parse_day_file(raw):
    """-> {date: {"Statewide": n, county: n, ...}} from one DayN workbook."""
    sheets = C.read_xlsx(raw, positional=True)
    rows = sheets.get("EarlyAbsentee") or next(iter(sheets.values()), [])
    if not rows or [str(x).strip().lower() for x in rows[0][:3]] != ["county", "date", "day total"]:
        raise ValueError("unexpected layout: %s" % (rows[0][:4] if rows else "empty"))
    out = {}
    for r in rows[1:]:
        if len(r) < 3 or not str(r[0]).strip():
            continue
        d, n = _day(r[1]), _int(r[2])
        if d is None or n is None:
            continue
        out.setdefault(d, {})[str(r[0]).strip()] = n
    return out


def fetch_official(cfg):
    """-> (as_of_text, {county: cumulative}, statewide, days) or None."""
    tag = cfg["election"]["date"].replace("-", "")
    page = C.http_get(PAGE, no_cache=True, retries=2)
    found = {int(n): base for base, n in re.findall(FILE_RE % tag, page)}
    if not found:
        print("tn: no %s DayN turnout file linked on %s yet" % (tag, PAGE))
        return None
    last = max(found)
    base = found[last]
    days = parse_day_file(C.http_get("%sDay%dTurnout.xlsx" % (base, last), binary=True, no_cache=True, retries=2))
    if len(days) < last:   # the newest file holds only some days: read the earlier ones too
        for n in range(1, last):
            for d, vals in parse_day_file(C.http_get("%sDay%dTurnout.xlsx" % (base, n), binary=True,
                                                     no_cache=True, retries=2)).items():
                days.setdefault(d, vals)   # a newer file's figures for a day win
    if len(days) < last:
        raise ValueError("only %d dated days for Day%d" % (len(days), last))
    totals, statewide = {}, 0
    for d, vals in sorted(days.items()):
        sw = vals.get("Statewide")
        counties = {k: v for k, v in vals.items() if k != "Statewide"}
        if sw is None or sum(counties.values()) != sw:
            raise ValueError("%s: counties sum to %d, Statewide row %s" % (d, sum(counties.values()), sw))
        statewide += sw
        for k, v in counties.items():
            totals[k] = totals.get(k, 0) + v
    end = max(days)
    return "Day %d (through %s %d)" % (last, end.strftime("%b"), end.day), totals, statewide, sorted(days)


def blk(t):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(t), "rep_pct": None,
            "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {})
    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}
    try:
        res = fetch_official(cfg)
    except Exception as e:  # noqa: BLE001
        print("tn: state report unusable (%s) -- trying the Election Lab" % str(e)[:160], file=sys.stderr)
        res = None
    if res:
        as_of, totals, sw_total, days = res
        unmatched = [k for k in totals if _norm(k) not in gidx]
        if unmatched or len(totals) != len(gidx):
            print("tn: county names don't match the map (%s) -- trying the Election Lab" % unmatched[:5], file=sys.stderr)
            res = None
    if not res:
        import lab_standin as LAB
        os.makedirs(DATA_DIR, exist_ok=True)
        if not LAB.run(STATE, cfg, GEO_PATH, LATEST_PATH, HISTORY_PATH, partisan=False, force=force,
                       role="stand-in while the Secretary of State's own report can't be read"):
            print("tn: no figures yet.")
        return 0

    counties = {}
    for k, v in totals.items():
        g = gidx[_norm(k)]
        counties[g["name"]] = {"fips": g["fips"], "cast": blk(v), "early_voted": blk(v), "registered": 0, "turnout_pct": None}
    body = {
        "state": STATE, "state_name": cfg.get("state_name", "Tennessee"), "election": cfg.get("election", {}),
        "partisan": False, "methods_present": ["early_voted"], "method_labels": cfg.get("method_labels", {}),
        "statewide": {"cast": blk(sw_total), "early_voted": blk(sw_total), "registered": 0, "turnout_pct": None},
        "counties": counties,
    }
    prev = load(LATEST_PATH, {}) or {}
    import lab_standin as LAB
    bad = LAB.suspicious_drop(prev, sw_total)
    if bad:
        print("tn: %s" % bad, file=sys.stderr)
        return 0
    h = C.data_hash(body)
    changed = force or h != prev.get("data_hash")
    now = C.utc_now_iso()
    snap = dict(body, source={"primary": "Tennessee Secretary of State, daily 'Early and Absentee Voters' report (by county)",
                              "url": PAGE, "as_of": as_of, "days": [d.isoformat() for d in days]},
                source_compiled=as_of, source_compiled_iso=now if changed else prev.get("source_compiled_iso", now),
                data_hash=h, generated_at=now if changed else prev.get("generated_at", now))
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    if changed:
        with open(HISTORY_PATH, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"generated_at": now, "cast": sw_total, "registered": 0, "data_hash": h},
                               separators=(",", ":")) + "\n")
    print("%s  %s | counties=%d cast=%d" % ("CHANGED" if changed else "NOCHANGE", as_of, len(counties), sw_total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
