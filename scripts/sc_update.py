#!/usr/bin/env python3
"""South Carolina in-person early-voting turnout by county (turnout-only; SC
publishes no party breakdown for this report).

Source: the SC Election Commission's "Early Voting Report" CSV. scvotes.gov
posts one of these for every statewide election during its early-voting
period, but there is no fixed filename or folder from one election to the
next -- past names include:
  2026/06/Early-Voting-Report-June-2026-Primary-6.csv
  2026/08/Early-Voting-Totals-2026-08-24-07-52-25-1-1-1.csv
  2026/08/Early-Voting-Complete.csv
So each run re-reads the public "Voter Registration and Participation
Statistics" page (which the SC Election Commission's own early-voting page
points to for this report) and picks the newest linked CSV under
wp-content/uploads/2026/<month>/ whose name doesn't say Primary/Runoff/
Special/Senate (the already-finished elections this year).

The CSV itself has no stable column layout either: one format lists a
'Jurisdiction' header with a per-early-voting-center breakdown under each
county and a 'TOTALS' column at the end; another lists only one row per
county (an 'Election Name' column) ending in a 'Total' column. Both put the
county's (or 'South Carolina' statewide) running total in the FIRST column of
its row, with every other column either a per-day count or blank, and the
LAST non-blank cell in that row is always the cumulative total -- so a row is
read by matching column 0 against this state's county names (ignoring rows
whose first cell is blank, which are the per-center sub-rows) and taking its
last non-empty cell, regardless of how many day-columns the report currently
has.

2026 general: early voting runs Monday Oct 19 - Saturday Oct 31 (closed Sun
Oct 25); until SC posts the report (expected once voting opens), this reads 0.

Run:  python scripts/sc_update.py [--force]
"""
import csv
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "sc"
CONFIG_PATH = os.path.join(ROOT, "config", "sc.json")
GEO_PATH = os.path.join(ROOT, "assets", "sc-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
STATS_PAGE = "https://scvotes.gov/elections-statistics/voter-participation/"
EARLY_VOTING_PAGE = "https://scvotes.gov/voters/early-voting/"
# finished 2026 elections whose CSVs are already linked on the stats page --
# never mistake one of these for the still-to-come general's report.
DONE_ELECTIONS = re.compile(r"primary|runoff|special|senate", re.I)
CSV_LINK = re.compile(
    r'<a[^>]+href="(https://scvotes\.gov/wp-content/uploads/2026/(?:0[9]|1[0-2])/[^"]*?\.csv)"[^>]*>(.*?)</a>', re.I | re.S)
# the general's report is linked as "Early Voting Turnout Report" -> EV-Totals-10-19-26.csv (2026-10-20):
# the file name alone doesn't say "early voting", so the link text counts too
EV_WORDS = re.compile(r"early[\s_-]*voting|\bEV[\s_-]+(?:totals|turnout|report|stat)", re.I)


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _norm(s):
    s = str(s).strip().lower()
    s = re.sub(r"\s+county$", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def _block(total):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(total),
            "rep_pct": None, "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def discover_csv_url(html=None):
    """-> (csv_url, status) picking the newest-posted "Early Voting Report" CSV
    on the stats page that isn't one of this year's already-finished elections."""
    if html is None:
        try:
            html = C.http_get(STATS_PAGE, no_cache=True, timeout=60)
        except Exception as e:  # noqa: BLE001
            return None, "stats page unreachable: %s" % str(e)[:140]
    links = [(m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))) for m in CSV_LINK.finditer(html)]
    candidates = [u for u, txt in links if EV_WORDS.search(u.rsplit("/", 1)[-1]) or EV_WORDS.search(txt)]
    good = [u for u, txt in links if u in candidates and not DONE_ELECTIONS.search(u + " " + txt)]
    if good:
        # newest by the date in the file name (EV-Totals-10-19-26.csv), else the first listed
        def stamp(u):
            m = re.search(r"(\d{1,2})-(\d{1,2})-(\d{2,4})(?=\D*\.csv$)", u)
            return (int(m.group(3)) % 100, int(m.group(1)), int(m.group(2))) if m else (0, 0, 0)
        return max(good, key=lambda u: (stamp(u), -good.index(u))), "ok"
    if candidates:
        return None, "only finished-election reports linked so far (%s)" % candidates[-1].rsplit("/", 1)[-1]
    return None, "no Early Voting Report CSV linked yet (general EV runs Oct 19-31; posted once voting opens)"


REG_CACHE = os.path.join(DATA_DIR, "_registration.json")
VR_LINK = re.compile(r'href="(https://scvotes\.gov/wp-content/uploads/\d{4}/\d{2}/'
                     r'VR-Totals-Race-County-Gender-Age-(\d{4})-(\d{1,2})-(\d{1,2})\.(?:xlsx|csv))"', re.I)


def registration(gidx, stats_html):
    """Registered voters by county from the newest "VR Totals by County / Race / Gender / Age" report linked on
    the stats page (monthly; its "Race: All" sheet, county name then all-voters total) -> (counties, as_of, url).
    Kept in data/sc/_registration.json and re-read only when a newer report is linked."""
    cache = load(REG_CACHE, {}) or {}
    links = sorted({(int(y), int(m), int(d), u) for u, y, m, d in VR_LINK.findall(stats_html or "")})
    if not links:
        return cache.get("counties") or {}, cache.get("as_of", ""), cache.get("url", "")
    y, m, d, url = links[-1]
    if cache.get("url") == url and cache.get("counties"):
        return cache["counties"], cache["as_of"], url
    try:
        raw = C.http_get(url, binary=True, timeout=90)
        if url.lower().endswith(".csv"):
            sheets = {"csv": list(csv.reader(io.StringIO(raw.decode("utf-8-sig", "replace"))))}
        else:
            sheets = C.read_xlsx(raw)
        rows = next((r for r in sheets.values() if any(c and str(c).strip().lower() == "race: all" for row in r[:12] for c in row)),
                    next(iter(sheets.values())))
        out = {}
        for row in rows:
            cells = [str(c).strip() for c in row]
            if len(cells) > 3 and re.fullmatch(r"\d{2}", cells[1] or "") and cells[2]:
                g = gidx.get(_norm(cells[2]))
                n = C.parse_number(cells[3])
                if g and n:
                    out[g["name"]] = int(n)
        if len(out) != len(gidx):
            raise ValueError("%d of %d counties" % (len(out), len(gidx)))
    except Exception as e:  # noqa: BLE001
        print("sc: registration report %s not used (%s)" % (url.rsplit("/", 1)[-1], str(e)[:100]), file=sys.stderr)
        return cache.get("counties") or {}, cache.get("as_of", ""), cache.get("url", "")
    cache = {"url": url, "as_of": "%04d-%02d-%02d" % (y, m, d), "counties": out}
    with open(REG_CACHE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cache, f, separators=(",", ":"))
    return out, cache["as_of"], url


def parse_csv(text, gidx):
    """-> ({county_name: total}, state_check_total, as_of). A county's FIRST
    matching row is its main-contest total; a county can repeat later in the
    file for a concurrent down-ballot special election sharing the same early
    voting period (seen on a 2026 primary report), which is a different vote
    count entirely and must not overwrite or add onto the main one."""
    rows = list(csv.reader(io.StringIO(text)))
    counties, state_total = {}, None
    for row in rows:
        if not row or not (row[0] or "").strip():
            continue
        name = row[0].strip()
        key = _norm(name)
        last = next((c for c in reversed(row) if (c or "").strip()), "")
        total = C.parse_number(last)
        if key in ("southcarolina", "southcarolinatotal"):
            if state_total is None:
                state_total = total
            continue
        g = gidx.get(key)
        if g and g["name"] not in counties:
            counties[g["name"]] = {"fips": g["fips"], "total": total}
    # "Election Date,Report Date" then " 11/03/2026, 10/20/2026 07:51:24": the report date, not the election's
    as_of = ""
    if len(rows) > 1 and any("report date" in (c or "").lower() for c in rows[0]):
        i = next(i for i, c in enumerate(rows[0]) if "report date" in (c or "").lower())
        as_of = (rows[1][i] if i < len(rows[1]) else "").strip()
    if not as_of:
        m = re.search(r"\d{1,2}/\d{1,2}/\d{4}", text[:400])
        as_of = m.group(0).strip() if m else ""
    return counties, state_total, as_of


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}
    prev = load(LATEST_PATH, {}) or {}

    if not force and C.checked_recently(DATA_DIR, "source", minutes=50):
        print("sc: checked under an hour ago.")
        return 0

    try:
        stats_html = C.http_get(STATS_PAGE, no_cache=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        stats_html = None
        csv_url, status = None, "stats page unreachable: %s" % str(e)[:140]
    if stats_html is not None:
        csv_url, status = discover_csv_url(stats_html)
    reg, reg_as_of, reg_url = registration(gidx, stats_html)
    counties, state_check, as_of = {}, None, ""
    if csv_url:
        try:
            text = C.http_get(csv_url, no_cache=True, timeout=60)
            counties, state_check, as_of = parse_csv(text, gidx)
            if not counties:
                status = "CSV found (%s) but no county rows matched" % csv_url.rsplit("/", 1)[-1]
        except Exception as e:  # noqa: BLE001
            status = "CSV fetch/parse failed (%s): %s" % (csv_url, str(e)[:140])
            csv_url = None

    cast_total = sum(c["total"] for c in counties.values())
    # Allow a small gap between the county rows and the statewide row: a few
    # UOCAVA/military-overseas ballots, or a down-ballot special election's
    # row sharing a county's name (see parse_csv), aren't tied to one county.
    gap = None if state_check is None else state_check - cast_total
    reconciled = gap is None or (0 <= gap <= max(50, (state_check or 0) // 200))
    # the general's first report (10/20) has a statewide row 161 BELOW its counties, each of which matches
    # its own early-voting centers exactly: within 0.5%, the county figures are used and the row's figure noted
    over = gap is not None and gap < 0 and -gap <= max(50, (state_check or 0) // 200)

    counties_out = {}
    for name, c in counties.items():
        blk = _block(c["total"])
        r = reg.get(name, 0)
        counties_out[name] = {"fips": c["fips"], "cast": blk, "early_voted": dict(blk),
                              "registered": r, "turnout_pct": round(100.0 * c["total"] / r, 2) if r else None}

    use_counties = (reconciled or over) and bool(counties_out)
    statewide_total = state_check if (state_check is not None and reconciled) else cast_total
    reg_total = sum(reg.values()) if len(reg) == len(gidx) else 0
    statewide = {"cast": _block(statewide_total), "early_voted": _block(statewide_total),
                 "registered": reg_total, "turnout_pct": round(100.0 * statewide_total / reg_total, 2) if reg_total else None}
    snap = {
        "state": STATE, "state_name": cfg.get("state_name", "South Carolina"),
        "election": cfg.get("election", {}), "partisan": False,
        "source": {"primary": "SC Election Commission \"Early Voting Report\" (county CSV, scvotes.gov); "
                              "in-person early voting, turnout-only",
                   "stats_page": STATS_PAGE, "early_voting_page": EARLY_VOTING_PAGE,
                   "csv_url": csv_url, "status": status,
                   "reconciled_with_statewide_row": reconciled, "fetched_at": C.utc_now_iso(),
                   **({"registration": {"url": reg_url, "as_of": reg_as_of}} if reg_total else {})},
        "source_compiled": as_of, "source_compiled_iso": (C.utc_now_iso() if cast_total else ""),
        "methods_present": (["early_voted"] if statewide_total else []),
        "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": (counties_out if use_counties else {}),
    }
    if not use_counties and counties_out:
        snap["source"]["status"] = ("county rows (%s) don't sum to the statewide row (%s) -- "
                                    "statewide total only" % (cast_total, state_check))
    elif over:
        snap["map_note"] = ("The SC Election Commission's report gives a statewide total of %s, %s fewer than its %d counties "
                            "add up to (each county matches its own early-voting centers), so the statewide figure here is "
                            "the counties' sum, %s." % (format(state_check, ","), format(-gap, ","), len(counties_out),
                                                       format(cast_total, ",")))
    elif gap:
        snap["map_note"] = ("%s of the %s early votes in the SC Election Commission's report aren't tied to a "
                            "single county (military/overseas ballots or a concurrent down-ballot election) and "
                            "appear only in the statewide total." % (format(gap, ","), format(state_check, ",")))
    hashable = {k: v for k, v in snap.items() if k != "source_compiled_iso"}
    hashable["source"] = {k: v for k, v in snap["source"].items() if k != "fetched_at"}
    snap["data_hash"] = C.data_hash(hashable)
    snap["generated_at"] = C.utc_now_iso()

    changed = force or snap["data_hash"] != prev.get("data_hash")
    os.makedirs(DATA_DIR, exist_ok=True)
    if not changed:
        snap["generated_at"] = prev.get("generated_at", snap["generated_at"])
    with open(LATEST_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(snap, f, separators=(",", ":"))
    if changed and cast_total:
        with open(HISTORY_PATH, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"generated_at": snap["generated_at"], "compiled": as_of,
                                "cast": cast_total}, separators=(",", ":")) + "\n")
    print("%s  %s | counties=%d  cast=%d  (%s)"
          % ("CHANGED" if changed else "NOCHANGE", status, len(counties_out), cast_total,
             csv_url or "no CSV yet"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
