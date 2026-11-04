#!/usr/bin/env python3
"""Florida 2026 Senate / Governor results by vote method (mail, early, Election Day), county by county.

The state's results files (Florida Election Watch) carry county totals only. Most counties run VR Systems' election
night reporting site, enr.electionsfl.org/<CODE>/<id>/, and its Reports page links a "Candidate Summary Results by
Party (CSV)" export. Where the county turned on results by vote type, that CSV has Mail / Early / Election Day (and
sometimes Provisional) columns; where it didn't, only totals, and the county is listed under `missing`.

Florida law (s. 98.0981) has counties replace any vote-type group of 1-29 votes with a dash; those cells are kept as
null, and a county whose Republican or Democratic row has a dash in a method can't be used for that method.

Writes data/fl/results_by_method.json:
  {updated, races:{sen:{label}, gov:{label}}, methods:[mail, early, eday, prov],
   counties:{County:{source, exported, sen:{rep:{mail,early,eday,prov,total}, dem:{...}, other:{...}}, gov:{...}}},
   missing:{County: why}}
Self-throttled to about every 30 minutes (data/fl/_checked.json "results_by_method").

Run:  python scripts/fl_results_by_method.py [--force]
"""
import csv
import html
import io
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

DATA_DIR = os.path.join(ROOT, "data", "fl")
OUT_PATH = os.path.join(DATA_DIR, "results_by_method.json")
IDS_PATH = os.path.join(ROOT, "config", "fl_enr_ids.json")
ENR = "https://enr.electionsfl.org/"
RACES = (("sen", re.compile(r"^(United States Senator|U\.?\s?S\.? Senat)", re.I)),   # Union: "US Senate"
         ("gov", re.compile(r"^Gov(ernor)?\b", re.I)))                               # Union: "Gov & Lt. Gov"
COLS = (("mail", re.compile(r"mail", re.I)), ("early", re.compile(r"early", re.I)),
        ("eday", re.compile(r"election\s*day", re.I)), ("prov", re.compile(r"provisional", re.I)))


def load(p, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def cell(v):
    """'67,412' -> 67412; '-' (a suppressed 1-29 group) or blank -> None."""
    v = (v or "").strip().replace(",", "")
    return int(float(v)) if re.match(r"^\d+(\.\d+)?$", v) else None


def county_csv(code, eid):
    """-> (csv url, export timestamp) for a county's Candidate Summary Results by Party export, or (None, why)."""
    try:
        page = C.http_get("%s%s/%d/Reports/" % (ENR, code, eid), no_cache=True, timeout=45)
    except Exception as e:  # noqa: BLE001
        return None, "no Reports page (%s)" % str(e)[:40]
    m = re.search(r'href="(https://[^"]+/CandidateSummaryResultsbyParty_([^_"]+)_[^"]+\.csv)"', page)
    return (m.group(1), m.group(2)) if m else (None, "no summary CSV on its Reports page")


def parse(text):
    """-> {race: {rep|dem|other: {mail, early, eday, prov, total}}} or None when the CSV has no vote-type columns."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return None
    head = rows[0]
    idx = {k: next((i for i, h in enumerate(head) if rx.search(h)), None) for k, rx in COLS}
    tot = next((i for i, h in enumerate(head) if re.match(r"(?i)total votes", h.strip())), None)
    ip = next((i for i, h in enumerate(head) if h.strip().lower() == "party"), None)
    if idx["mail"] is None or idx["early"] is None or idx["eday"] is None or tot is None or ip is None:
        return None
    out, seen = {}, set()
    for r in rows[1:]:
        if len(r) <= max(i for i in list(idx.values()) + [tot] if i is not None):
            continue
        race = next((k for k, rx in RACES if rx.search(r[0].strip())), None)
        if not race or (r[0], r[1]) in seen:   # some exports repeat a contest's first rows
            continue
        seen.add((r[0], r[1]))
        p = r[ip].strip().upper()
        key = "rep" if p == "REP" else "dem" if p == "DEM" else "other"
        e = out.setdefault(race, {}).setdefault(key, {"mail": 0, "early": 0, "eday": 0, "prov": 0, "total": 0})
        for k, i in idx.items():
            v = cell(r[i]) if i is not None else 0
            if v is None:   # suppressed: unknown for this method (a major party's row then can't be used)
                e[k] = None
            elif e[k] is not None:
                e[k] += v
        e["total"] += cell(r[tot]) or 0
    return out or None


def parse_summary(page):
    """The Summary page's per-race "DetailResults" tables (a county with no Reports page can still show them): the
    same rows as the CSV, rebuilt as CSV text for parse()."""
    lines = []
    for title, table in re.findall(r'<div class="row RaceName">.*?<label>([^<]+)</label>.*?<table class="DetailResults"[^>]*>(.*?)</table>',
                                   page, re.S):
        heads = [re.sub(r"\s+", " ", h).strip() for h in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)]
        if not lines:
            lines.append(["Contest", "Candidate Issue", "Party"] + heads[1:])
        elif heads[1:] != lines[0][3:]:
            continue   # a race laid out differently (e.g. a yes/no question); not one we read
        for tr in re.findall(r"<tr>(.*?)</tr>", table, re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if len(tds) != len(heads):
                continue
            who = re.sub(r"<div.*", "", tds[0], flags=re.S)
            who = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", who)).strip()
            p = (re.search(r"\((\w+)\)\s*$", who) or [None, ""])[1]
            lines.append([html.unescape(title).strip(), who, p] + [re.sub(r"<[^>]+>", "", x).strip() for x in tds[1:]])
    if not lines:
        return None
    buf = io.StringIO()
    csv.writer(buf).writerows(lines)
    return parse(buf.getvalue())


def matches(got, want):
    """Do the Republican and Democratic totals agree with the state's county results (Florida Election Watch)? A
    county's CSV export can be hours stale (Nassau's still read all zeros the morning after)."""
    if not want:
        return True
    return all(got.get(rk, {}).get(p, {}).get("total") == (want.get(rk) or {}).get(p)
               for rk in ("sen", "gov") if want.get(rk) for p in ("rep", "dem"))


def one(name, code, eid, want):
    if not eid:
        return name, None, "no VR Systems results site found for 2026"
    url, stamp = county_csv(code, eid)
    try:
        got = parse(C.http_get(url, timeout=45)) if url else None
        if not got or not matches(got, want):   # no CSV, totals only, or stale: the live Summary page may still have them
            live = parse_summary(C.http_get("%s%s/%d/Summary/" % (ENR, code, eid), no_cache=True, timeout=60))
            if live and (matches(live, want) or not got):
                got, stamp = live, None
    except Exception as e:  # noqa: BLE001
        return name, None, "results unreadable (%s)" % str(e)[:40]
    if not got:
        return name, None, "publishes totals only, not by vote method"
    if not matches(got, want):
        return name, None, "its results by vote method don't match its current totals yet"
    got["source"] = "%s%s/%d/Summary/" % (ENR, code, eid)
    got["exported"] = stamp
    return name, got, None


def main(argv):
    force = "--force" in argv
    if not force and C.checked_recently(DATA_DIR, "results_by_method", 30):
        print("fl results by method: checked under 30 min ago; skipping")
        return 0
    turnout = load(os.path.join(DATA_DIR, "latest.json"), {})
    ids = (load(IDS_PATH, {}) or {}).get("ids", {})
    totals = (load(os.path.join(DATA_DIR, "results_county.json"), {}) or {}).get("counties", {})
    jobs = [(n, c["code"], ids.get(c["code"]), totals.get(n)) for n, c in sorted(turnout.get("counties", {}).items())]
    counties, missing = {}, {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for name, got, why in pool.map(lambda j: one(*j), jobs):
            if got:
                counties[name] = got
            else:
                missing[name] = why
    old = load(OUT_PATH, {}) or {}
    for name, why in list(missing.items()):   # a county that fails a fetch keeps its last good copy
        if name in (old.get("counties") or {}) and why.startswith("results unreadable"):
            counties[name] = old["counties"][name]
            del missing[name]
    out = {"state": "fl", "updated": C.utc_now_iso(),
           "races": {"sen": {"label": "U.S. Senate (special)"}, "gov": {"label": "Governor"}},
           "methods": ["mail", "early", "eday", "prov"],
           "note": "County results by vote method from each county's VR Systems election night reporting CSV "
                   "(enr.electionsfl.org). Unofficial until certified. null = a group of 1-29 votes the county "
                   "suppressed under s. 98.0981, F.S.",
           "counties": counties, "missing": missing}
    if {k: v for k, v in out.items() if k != "updated"} != {k: v for k, v in old.items() if k != "updated"}:
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, separators=(",", ":"))
    print("fl results by method: %d counties by method, %d without (%s)" % (
        len(counties), len(missing), ", ".join("%s: %s" % kv for kv in sorted(missing.items())[:6])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
