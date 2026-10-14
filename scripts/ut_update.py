#!/usr/bin/env python3
"""Utah turnout by county (no party breakdown; turnout-only).

Source: the Lt. Governor's Office "Master Aggregated Numbers" workbook
(vote.utah.gov/wp-content/uploads/.../Master-Aggregated-Numbers-2023-2026.xlsx),
linked from https://vote.utah.gov/election-results-data-historical-information .
It is a single, persistent XLSX the office keeps appending to after each
election's canvass is certified: one sheet per election, named
<G|P|PP><2-digit year> (e.g. P26 = 2026 primary, G25 = 2025 general). Each
sheet has one row per county with active voters, ballots counted by method
(mail/drop box, in-person early voting, in-person, UOCAVA/disability
electronic, provisional, emergency) and a total.

This is Utah's only statewide, machine-readable, per-county turnout source we
could find (see config/ut.json "_comment" for what was ruled out: there is no
live daily county feed -- Utah Code 20A-3a-405 instead makes each of the 29
counties post its OWN "estimated ballots received" on its OWN website on a
Mon/Wed/Fri cadence, with no state rollup and no party split; the Lt.
Governor's "Interactive Data Hub" Tableau dashboard is voter *registration*
demographics, not ballots returned).

Consequence: this writes ZERO for the 2026 general (sheet "G26") until Utah's
statewide canvass is certified, which happens WEEKS after election day (not
during the Oct 15-Nov 3 early-voting window). That matches the accepted
"source is real but not live yet" pattern used by LA/MD for their statewide
PDFs: the parser is real and proven (see --validate below); it lights up
automatically once the state appends the G26 sheet.

We re-discover the current xlsx URL from the historical-information page each
run (its upload path changes when the office replaces the file), falling back
to the last-known URL if the page can't be scraped.

Run:  python scripts/ut_update.py [--force]
      python scripts/ut_update.py --validate   (parse the live P26 sheet to prove the parser)
"""
import os
import re
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "ut"
CONFIG_PATH = os.path.join(ROOT, "config", STATE + ".json")
GEO_PATH = os.path.join(ROOT, "assets", "ut-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")

PAGE_URL = "https://vote.utah.gov/election-results-data-historical-information"
FALLBACK_XLSX = "https://vote.utah.gov/wp-content/uploads/2025/12/Master-Aggregated-Numbers-2023-2026.xlsx"
XLSX_LINK_RE = re.compile(r"https://vote\.utah\.gov/wp-content/uploads/[^\"'<>\s]*Master-Aggregated-Numbers[^\"'<>\s]*\.xlsx", re.I)

# election-year -> sheet code the 2026 general lives under once certified
SHEET = "G26"

# normalized source-column-name -> bucket it feeds. "mail_voted" picks up the
# two small electronic-return columns (disability / UOCAVA) along with
# mail/drop box; "election_day" picks up the small "emergency ballots"
# column along with in-person. Doing it this way keeps `cast` close to the
# workbook's own "Total ballots counted" (used directly, not re-summed) while
# still giving the front-end a sane method breakdown.
COL_MAP = {
    "county": "county",
    "activevoters": "registered",
    "countedmaildropbox": "mail_voted",
    "countedinpersonearlyvoting": "early_voted",
    "countedinperson": "election_day",
    "disabilityballotsreturnedelectronically": "mail_voted",
    "returnedelectronicallyuocava": "mail_voted",
    "countedprovisional": "provisional",
    "emergencyballotscounted": "election_day",
    "totalballotscounted": "cast_total",
}
METHODS = ["mail_voted", "early_voted", "election_day", "provisional"]


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def norm_header(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _block(total):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(total),
            "rep_pct": None, "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def discover_xlsx_url():
    try:
        html = C.http_get(PAGE_URL, no_cache=True, retries=2)
        m = XLSX_LINK_RE.search(html)
        if m:
            return m.group(0)
    except Exception as e:  # noqa: BLE001
        print("UT: couldn't scrape the data page for the current xlsx link: %s" % str(e)[:140], file=sys.stderr)
    return FALLBACK_XLSX


def parse_sheet(rows, county_names):
    """rows: positional read_xlsx rows for one sheet. Returns
    {county_name: {bucket: value}} using COL_MAP, or {} if the header row
    doesn't look like what we expect (so a format change fails loud, not
    silently-wrong)."""
    if not rows:
        return {}
    header = [norm_header(c) for c in rows[0]]
    idx = {}
    for i, h in enumerate(header):
        if h in COL_MAP and COL_MAP[h] not in idx:
            idx[COL_MAP[h]] = i
        elif h in COL_MAP:
            # a bucket fed by >1 column (mail_voted, election_day): remember all indices
            idx.setdefault(COL_MAP[h] + "_extra", []).append(i)
    if "county" not in idx or "cast_total" not in idx:
        print("UT: sheet header doesn't match the expected 'Master Aggregated Numbers' layout "
              "(missing County/Total columns) -- not parsing it.", file=sys.stderr)
        return {}

    def col(row, key):
        total = 0
        i = idx.get(key)
        if i is not None and i < len(row):
            total += C.parse_number(row[i])
        for i in idx.get(key + "_extra", []):
            if i < len(row):
                total += C.parse_number(row[i])
        return total

    out = {}
    for row in rows[1:]:
        if not row or not str(row[idx["county"]] if idx["county"] < len(row) else "").strip():
            continue
        name = str(row[idx["county"]]).strip()
        if name not in county_names:
            continue  # skips the trailing "Statewide" row and any blank/footnote rows
        out[name] = {k: col(row, k) for k in METHODS}
        out[name]["cast_total"] = col(row, "cast_total")
        out[name]["registered"] = col(row, "registered")
    return out


def build_snapshot(cfg, parsed, gidx, sheet_label):
    counties_out = {}
    for name, vals in parsed.items():
        g = gidx.get(name)
        if not g:
            continue
        ent = {"fips": g["fips"], "registered": vals["registered"]}
        for m in METHODS:
            if vals.get(m):
                ent[m] = _block(vals[m])
        ent["cast"] = _block(vals["cast_total"])
        ent["turnout_pct"] = C.pct(vals["cast_total"], vals["registered"])
        counties_out[name] = ent

    statewide = {"registered": sum(c["registered"] for c in counties_out.values())}
    for m in METHODS:
        tot = sum((c.get(m) or {}).get("total", 0) for c in counties_out.values())
        if tot:
            statewide[m] = _block(tot)
    statewide["cast"] = _block(sum(c["cast"]["total"] for c in counties_out.values()))
    statewide["turnout_pct"] = C.pct(statewide["cast"]["total"], statewide["registered"])
    methods_present = sorted({m for c in counties_out.values() for m in METHODS if c.get(m)})

    snap = {
        "state": STATE, "state_name": cfg.get("state_name", "Utah"), "election": cfg.get("election", {}),
        "partisan": False,
        "source": {"primary": "Utah Lt. Governor's Office, Master Aggregated Numbers workbook (statewide canvass "
                               "statistics by county); turnout-only, posted once the %s canvass is certified"
                               % sheet_label,
                    "url": PAGE_URL},
        "source_compiled": sheet_label, "source_compiled_iso": (C.utc_now_iso() if counties_out else ""),
        "methods_present": methods_present, "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties_out,
    }
    snap["data_hash"] = C.data_hash(snap)
    snap["generated_at"] = C.utc_now_iso()
    return snap


def main():
    force = "--force" in sys.argv
    validate = "--validate" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    geo = load(GEO_PATH, {"features": []})
    gidx = {f["properties"]["name"]: f["properties"] for f in geo["features"]}
    county_names = set(gidx)

    url = discover_xlsx_url()
    try:
        raw = C.http_get(url, binary=True, no_cache=True, retries=2)
    except Exception as e:  # noqa: BLE001
        print("UT: workbook unavailable (%s): %s" % (url, str(e)[:140]), file=sys.stderr)
        return 0 if not validate else 2

    try:
        sheets = C.read_xlsx(raw, positional=True)
    except Exception as e:  # noqa: BLE001
        print("UT: couldn't parse the xlsx: %s" % str(e)[:140], file=sys.stderr)
        return 2

    if validate:
        # Prove the parser against the live Primary 2026 sheet (real, already-
        # certified data in the same workbook) rather than the not-yet-existing
        # general sheet.
        parsed = parse_sheet(sheets.get("P26", []), county_names)
        snap = build_snapshot(cfg, parsed, gidx, "P26 (validation)")
        cast = snap["statewide"].get("cast", {})
        print("VALIDATE  sheet=P26  counties=%d  cast=%s  registered=%s  turnout=%s%%"
              % (len(snap["counties"]), cast.get("total"), snap["statewide"].get("registered"),
                 snap["statewide"].get("turnout_pct")))
        for name in sorted(snap["counties"])[:5]:
            c = snap["counties"][name]
            print("  %-12s cast=%-7s registered=%-8s turnout=%s%%"
                  % (name, c["cast"]["total"], c["registered"], c["turnout_pct"]))
        return 0

    parsed = parse_sheet(sheets.get(SHEET, []), county_names)
    snap = build_snapshot(cfg, parsed, gidx, SHEET)

    prev = load(LATEST_PATH, {}) or {}
    changed = force or snap["data_hash"] != prev.get("data_hash")
    os.makedirs(DATA_DIR, exist_ok=True)
    if changed:
        with open(LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        cast = snap["statewide"].get("cast", {"total": 0})
        if cast.get("total"):
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"generated_at": snap["generated_at"], "cast": cast["total"]},
                                    separators=(",", ":")) + "\n")
        print("CHANGED  sheet=%s  counties=%d  cast=%s" % (SHEET, len(snap["counties"]), cast.get("total")))
    else:
        print("NOCHANGE  sheet=%s not yet in the workbook (%d counties)" % (SHEET, len(snap["counties"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
