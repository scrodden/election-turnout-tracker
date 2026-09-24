#!/usr/bin/env python3
"""Hawaii ballots by county (turnout-only; HI has no party registration).

Source: Hawaii Office of Elections "Voting Report" page, which posts a daily
statewide PDF, AbsenteeReconState-YYYYMMDD.pdf ("Absentee Reconciliation",
2026 General Election). One row per county plus a statewide total row, nine
numbers each:
  ELECT Sent (a), ELECT Voted (b), ELECT Invalid (c)   electronic (email/fax)
  EV Voted (d)                                         early in-person voting
  MAIL Sent (e), MAIL Voted (f), MAIL Invalid (g)      mail (Hawaii is all-mail)
  VOTED (b+d+f), TOTAL (a+d+e)
Kalawao is administered with Maui and has no row of its own.

mail_voted = b + f; early_voted = d; cast = VOTED; mail_provided = ballots
still outstanding = (a - b - c) + (e - f - g) -> ballot chase. Every registered
voter is mailed a ballot, so there is no separate turnout denominator here.

Run:  python scripts/hi_update.py [--force]
"""
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "hi"
CONFIG_PATH = os.path.join(ROOT, "config", "hi.json")
GEO_PATH = os.path.join(ROOT, "assets", "hi-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")

COUNTY_LABELS = ["Hawai'i", "Maui", "Kaua'i", "Honolulu"]


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _block(total):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(total),
            "rep_pct": None, "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def newest_report(src):
    """Newest statewide PDF linked from the report page -> (url, 'YYYY-MM-DD')."""
    html = C.http_get(src["report_page"], retries=2)
    best = None
    for u, d in re.findall(r'href="([^"]*AbsenteeReconState-(\d{8})\.pdf)"', html):
        if best is None or d > best[1]:
            best = (u, d)
    if not best:
        raise RuntimeError("HI: no AbsenteeReconState PDF linked")
    d = best[1]
    return best[0], "%s-%s-%s" % (d[:4], d[4:6], d[6:])


def parse(raw):
    """-> (election title, {county label: [9 ints]}, statewide [9 ints])"""
    from pypdf import PdfReader
    text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(raw)).pages)
    title = re.search(r"(\d{4} (?:General|Primary|Special)[^\n]*Election)", text)
    rows = {}
    for label in COUNTY_LABELS:
        m = re.search(r"^%s\s*\n((?:\d+\s*\n){8}\d+)" % re.escape(label), text, re.M)
        if not m:
            raise RuntimeError("HI: no row for %s" % label)
        rows[label] = [int(x) for x in m.group(1).split()]
    # statewide total row: the nine numbers right after the Honolulu row
    after = text[text.find("Honolulu"):]
    nums = [int(x) for x in re.findall(r"^(\d+)\s*$", after, re.M)]
    total = nums[9:18] if len(nums) >= 18 else None
    if not total or any(total[i] != sum(r[i] for r in rows.values()) for i in range(9)):
        raise RuntimeError("HI: county rows don't add up to the statewide row")
    return (title.group(1) if title else ""), rows, total


def entity(r):
    a, b, c, d, e, f, g, voted, _tot = r
    out = max(0, a - b - c) + max(0, e - f - g)
    ent = {"cast": _block(voted), "mail_voted": _block(b + f), "early_voted": _block(d),
           "mail_provided": _block(out), "registered": 0, "turnout_pct": None,
           "ballots_sent": a + e}
    m = C.compute_mail(ent)
    if m:
        ent["mail"] = m
    return ent


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg.get("source", {})
    prev = load(LATEST_PATH, {}) or {}
    try:
        url, as_of = newest_report(src)
        if url.startswith("/"):
            url = "https://elections.hawaii.gov" + url
        if not force and (prev.get("source") or {}).get("report_url") == url:
            print("hi: still the %s report — nothing new." % as_of)
            return 0
        title, rows, total = parse(C.http_get(url, binary=True, retries=2))
    except Exception as e:  # noqa: BLE001
        print("HI fetch/parse failed: %s" % str(e)[:160], file=sys.stderr)
        return 0
    if src.get("election_match") and src["election_match"].lower() not in title.lower():
        print("hi: report is for %r, not the %s — ignoring." % (title, src["election_match"]))
        return 0

    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}
    counties = {}
    for label, r in rows.items():
        g = gidx[_norm(label)]
        counties[g["name"]] = dict(entity(r), fips=g["fips"])
    statewide = entity(total)
    methods_present = [k for k in ("mail_voted", "early_voted", "mail_provided") if statewide[k]["total"]]
    snap = {
        "state": STATE, "state_name": cfg.get("state_name", "Hawaii"), "election": cfg.get("election", {}),
        "partisan": False,
        "source": {"primary": "Hawaii Office of Elections daily Absentee Reconciliation report (statewide PDF)",
                   "report_url": url, "report_date": as_of, "report_title": title},
        "source_compiled": as_of, "source_compiled_iso": C.utc_now_iso(),
        "methods_present": methods_present, "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties,
    }
    snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k not in ("source", "source_compiled_iso")})
    snap["generated_at"] = C.utc_now_iso()
    os.makedirs(DATA_DIR, exist_ok=True)
    changed = force or snap["data_hash"] != prev.get("data_hash")
    with open(LATEST_PATH, "w", encoding="utf-8") as f:   # also records the report URL we've seen
        json.dump(snap, f, separators=(",", ":"))
    if changed and statewide["cast"]["total"]:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"generated_at": snap["generated_at"], "cast": statewide["cast"]["total"],
                                "data_hash": snap["data_hash"]}, separators=(",", ":")) + "\n")
    print("%s  %s (%s) | voted=%d (mail %d, early %d) | sent=%d"
          % ("CHANGED" if changed else "NOCHANGE", title, as_of, statewide["cast"]["total"],
             statewide["mail_voted"]["total"], statewide["early_voted"]["total"], statewide["ballots_sent"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
