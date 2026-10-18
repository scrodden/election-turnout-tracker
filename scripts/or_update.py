#!/usr/bin/env python3
"""Oregon turnout by county (no party breakdown in the live feed; turnout-only).

Source: the Oregon SoS "Daily Ballot Returns" report (an OregonVotes.gov /
Power BI export, published as a PDF at sos.oregon.gov), which lists every
county's active/eligible voters, ballots returned, and percent returned
during the ~2-week count window before a statewide election. Requires pypdf
(the workflow installs it); see common.read_xlsx's sibling parsers (LA/MD) for
the same "real parser, fetch 404s until the file is live" pattern this
follows.

Party breakdown: Oregon DOES register voters by party, and the SoS's
POST-CERTIFICATION "Official Voter Registration & Turnout Statistics" PDF (not
published until weeks after election day, once results are certified) does
break ballots-returned out by county AND party. But the live, during-the-count
"Daily Ballot Returns" file -- the only thing published while ballots are
actually coming in -- carries no party split, confirmed from both the May 2026
primary's report and contemporaneous news coverage of the file's columns
(County / Eligible or Active Voters / Ballots Returned / Percent Returned).
So this connector is turnout-only (partisan:false in config), matching what's
actually live.

URL: the filename is keyed to the election date and has changed convention
almost every cycle (2024: static "G24-Daily-Ballot-Returns.pdf" under
/voting/Documents/; Nov 2025: "november-2025-Daily-Ballot-Returns.pdf" under
/elections/Documents/; May 2026 primary: "May-19-2026-Daily-Ballot-Returns.pdf"
under /elections/Documents/). The file is taken down once its cycle ends (all
of the above now 404), so there is no persisted copy to test against and no
single URL we can be sure of ahead of time. We try a short list of candidates
built from those conventions each run; whichever one first returns a real PDF
is used and remembered. None exist yet as of the 2026-10-14 rollout (daily
counts historically start posting ~2 weeks before election day, i.e. around
2026-10-20) -- see config/or.json "_comment".

Run:  python scripts/or_update.py [--force]
      python scripts/or_update.py --validate <path-to-a-sample-pdf>
          (parse a locally-saved sample of this report to sanity-check the
          regex if/when one becomes available)
"""
import os
import re
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "or"
CONFIG_PATH = os.path.join(ROOT, "config", STATE + ".json")
GEO_PATH = os.path.join(ROOT, "assets", "or-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")

# One line per county: COUNTY NAME (all-caps, may have an internal space, e.g.
# "HOOD RIVER") then two integers (commas allowed) and a percent. Matches the
# same report generator's "Total" table confirmed on the post-election stats
# PDF (county / active / ballots returned / percent) -- see config/or.json.
COUNTY_LINE_RE = re.compile(r"^([A-Z][A-Z ]*[A-Z])\s+([\d,]+)\s+([\d,]+)\s+([\d.]+)%")


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _block(total):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(total),
            "rep_pct": None, "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def candidate_urls(cfg):
    src = cfg.get("source", {})
    base = src.get("base", "https://sos.oregon.gov/elections/Documents/")
    alt_base = src.get("alt_base", "https://sos.oregon.gov/voting/Documents/")
    names = src.get("candidates") or []
    out = [base + n for n in names]
    out += [alt_base + n for n in names if "/" not in n]
    return out


def _page_matches(text, county_names):
    rows = {}
    for raw_line in text.split("\n"):
        m = COUNTY_LINE_RE.match(raw_line.strip())
        if not m:
            continue
        name = m.group(1).strip()
        if name not in county_names:
            continue
        rows[name] = (C.parse_number(m.group(2)), C.parse_number(m.group(3)))
    return rows


def parse_pdf(raw_bytes, county_names):
    """raw_bytes: a Daily-Ballot-Returns PDF. Returns {COUNTY_UPPER: (active, returned)}.

    Stops at the FIRST page whose county-line matches cover nearly all of
    `county_names`, rather than merging matches across every page. A
    minimal, single-purpose "Daily Ballot Returns" report should only ever
    have one such table; this guards against a differently-shaped document
    (e.g. a multi-section report with several per-county tables -- a
    per-party breakdown, a per-race vote count, etc., each of which can
    coincidentally match the same "COUNTY  number  number  pct%" shape) being
    silently mis-read as the turnout table. See or_update.py's --validate,
    which exercises this against the one page we can actually confirm the
    real report's column layout from."""
    import io
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
    threshold = max(1, int(0.9 * len(county_names)))
    best = {}
    for pg in reader.pages:
        rows = _page_matches(pg.extract_text() or "", county_names)
        if len(rows) >= threshold:
            return rows
        if len(rows) > len(best):
            best = rows
    return best


# ---- 2026 general: the "Ballot Return Dashboard" PDF (Power BI export) ----
# Page "Unofficial Ballots Returns by County" (active voters, ballots received)
# and page "... by County by Party" (one column per party; blank cells are
# dropped by plain text extraction, so numbers are placed by column position
# using pypdf's layout mode, then checked: every county's parties add up to
# its Total, every party column adds up to the Total row, and each county's
# Total equals its "Ballots Received" on the county page).
DASH_PARTIES = ["Democrat", "Independent", "Libertarian", "No Labels", "Nonaffiliated", "Other", "Pacific Green",
                "Progressive", "Republican", "Working Families", "Total"]
DASH_MAP = {"Democrat": "dem", "Republican": "rep", "Nonaffiliated": "npa"}   # the rest: minor parties -> oth


def _layout_rows(text, header_names):
    lines = text.splitlines()
    hi = next((i for i, l in enumerate(lines) if all(n in l for n in (header_names[0], header_names[-1]))), None)
    if hi is None:
        return None
    hdr, cols, pos = lines[hi], [], 0
    for n in header_names:
        j = hdr.find(n, pos)
        if j < 0:
            return None
        cols.append((n, j + len(n)))
        pos = j + len(n)
    rows = {}
    for l in lines[hi + 1:]:
        m = re.match(r"^\s*([A-Za-z][A-Za-z .]+?)(?:\s{2,}|$)", l)
        if not m:
            continue
        vals = {}
        for t in re.finditer(r"[\d,]+", l[m.end(1):]):
            end = m.end(1) + t.end()
            col = min(cols, key=lambda c: abs(c[1] - end))[0]
            vals[col] = vals.get(col, 0) + int(t.group().replace(",", ""))
        rows[m.group(1).strip().upper()] = vals
    return rows


def parse_dashboard(raw_bytes, county_names):
    """-> ({COUNTY: {"active", "received", "party": {rep,dem,npa,oth}}}, report_time_text) or raises ValueError."""
    import io
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
    texts = [pg.extract_text(extraction_mode="layout") or "" for pg in reader.pages]
    cty_page = next((t for t in texts if "Ballots Returns by County" in t and "Active Voters" in t and "by Party" not in t), None)
    party_page = next((t for t in texts if "by County by Party" in t), None)
    if not cty_page or not party_page:
        raise ValueError("dashboard pages not found")
    base = {}
    for l in cty_page.splitlines():
        m = re.match(r"^\s*([A-Z][A-Z .]+?)\s{2,}([\d,]+)(?:\s+([\d,]+))?", l)
        if m and m.group(1).strip() in county_names:
            base[m.group(1).strip()] = (int(m.group(2).replace(",", "")), int((m.group(3) or "0").replace(",", "")))
    rows = _layout_rows(party_page, DASH_PARTIES)
    if not rows or "TOTAL" not in rows:
        raise ValueError("party table not found")
    tot = rows.pop("TOTAL")
    rows = {k: v for k, v in rows.items() if k in county_names}
    if set(base) != county_names or set(rows) != county_names:
        raise ValueError("counties: %d on the county page, %d in the party table (of %d)" % (len(base), len(rows), len(county_names)))
    out = {}
    for c in county_names:
        v = rows[c]
        parties = sum(x for k, x in v.items() if k != "Total")
        if parties != v.get("Total", 0) or v.get("Total", 0) != base[c][1]:
            raise ValueError("%s: parties %d, Total %s, received %d" % (c, parties, v.get("Total"), base[c][1]))
        p = {"rep": 0, "dem": 0, "npa": 0, "oth": 0}
        for k, x in v.items():
            if k != "Total":
                p[DASH_MAP.get(k, "oth")] += x
        out[c] = {"active": base[c][0], "received": base[c][1], "party": p}
    if any(sum(v.get(n, 0) for v in rows.values()) != tot.get(n, 0) for n in DASH_PARTIES):
        raise ValueError("party columns don't add up to the Total row")
    m = re.search(r"Report Generated:\s*(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s*[AP]M)", "\n".join(texts))
    return out, (m.group(1) if m else "")


def report_time_iso(text):
    """'10/16/2026 7:00 AM' (Pacific) -> ISO UTC."""
    from datetime import datetime, timedelta
    try:
        t = datetime.strptime(re.sub(r"\s+", " ", text.strip()), "%m/%d/%Y %I:%M %p")
    except ValueError:
        return ""
    return (t + timedelta(hours=7 if 3 <= t.month <= 11 else 8)).strftime("%Y-%m-%dT%H:%M:%SZ")   # PDT / PST


def build_dashboard_snapshot(cfg, rows, gidx, url, report_time):
    counties_out = {}
    for name_upper, r in rows.items():
        g = gidx[name_upper]
        p = r["party"]
        blk = C.party_block(p["rep"], p["dem"], p["oth"], p["npa"])
        counties_out[g["name"]] = {"fips": g["fips"], "registered": r["active"], "mail_voted": blk, "cast": dict(blk),
                                   "turnout_pct": C.pct(r["received"], r["active"])}
    mv = C.add_blocks(*[c["mail_voted"] for c in counties_out.values()])
    reg = sum(c["registered"] for c in counties_out.values())
    statewide = {"mail_voted": mv, "cast": dict(mv), "registered": reg, "turnout_pct": C.pct(mv["total"], reg)}
    return {
        "state": STATE, "state_name": cfg.get("state_name", "Oregon"), "election": cfg.get("election", {}),
        "partisan": True,
        "source": {"primary": "Oregon Secretary of State, Ballot Return Dashboard (unofficial ballots received by county and "
                              "registered party; active voters)", "url": url, "as_of": report_time},
        "source_compiled": report_time, "source_compiled_iso": report_time_iso(report_time) or C.utc_now_iso(),
        "methods_present": ["mail_voted"] if mv["total"] else [], "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties_out,
    }


def fetch_live(cfg):
    """Try each candidate URL; return (raw_bytes, url) for the first that
    looks like a real PDF, or (None, None)."""
    for url in candidate_urls(cfg):
        try:
            raw = C.http_get(url, binary=True, no_cache=True, retries=2)
        except Exception:  # noqa: BLE001
            continue
        if raw[:4] == b"%PDF":
            return raw, url
    return None, None


def build_snapshot(cfg, rows, gidx, as_of_url):
    counties_out = {}
    for name_upper, (active, returned) in rows.items():
        g = gidx.get(name_upper)
        if not g:
            continue
        ent = {"fips": g["fips"], "registered": active, "mail_voted": _block(returned),
               "cast": _block(returned), "turnout_pct": C.pct(returned, active)}
        counties_out[g["name"]] = ent

    statewide = {"registered": sum(c["registered"] for c in counties_out.values())}
    cast_total = sum(c["cast"]["total"] for c in counties_out.values())
    if cast_total:
        statewide["mail_voted"] = _block(cast_total)
    statewide["cast"] = _block(cast_total)
    statewide["turnout_pct"] = C.pct(cast_total, statewide["registered"])

    snap = {
        "state": STATE, "state_name": cfg.get("state_name", "Oregon"), "election": cfg.get("election", {}),
        "partisan": False,
        "source": {"primary": "Oregon Secretary of State, Daily Ballot Returns report (county ballots returned); "
                               "turnout-only -- no party breakdown in the live feed",
                    "url": as_of_url or ""},
        "source_compiled": as_of_url or "", "source_compiled_iso": (C.utc_now_iso() if counties_out else ""),
        "methods_present": (["mail_voted"] if cast_total else []), "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties_out,
    }
    snap["data_hash"] = C.data_hash(snap)
    snap["generated_at"] = C.utc_now_iso()
    return snap


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    geo = load(GEO_PATH, {"features": []})
    gidx = {f["properties"]["name"].upper(): f["properties"] for f in geo["features"]}
    county_names = set(gidx)

    if "--validate" in sys.argv:
        i = sys.argv.index("--validate")
        sample = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
        if not sample or not os.path.exists(sample):
            print("usage: or_update.py --validate <path-to-sample-pdf>", file=sys.stderr)
            return 2
        with open(sample, "rb") as f:
            raw = f.read()
        rows = parse_pdf(raw, county_names)
        snap = build_snapshot(cfg, rows, gidx, sample)
        cast = snap["statewide"].get("cast", {})
        print("VALIDATE  counties=%d  cast=%s  registered=%s  turnout=%s%%"
              % (len(snap["counties"]), cast.get("total"), snap["statewide"].get("registered"),
                 snap["statewide"].get("turnout_pct")))
        return 0

    raw, url = fetch_live(cfg)
    snap = None
    if raw is not None and "Dashboard" in (url or ""):
        try:
            rows, report_time = parse_dashboard(raw, county_names)
            snap = build_dashboard_snapshot(cfg, rows, gidx, url, report_time)
            snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k != "source_compiled_iso"})
            snap["generated_at"] = C.utc_now_iso()
        except Exception as e:  # noqa: BLE001
            print("OR: dashboard not usable (%s) -- trying the Election Lab" % str(e)[:160], file=sys.stderr)
            import lab_standin as LAB
            os.makedirs(DATA_DIR, exist_ok=True)
            if not LAB.run(STATE, cfg, os.path.join(ROOT, "assets", "or-counties.geojson"), LATEST_PATH, HISTORY_PATH,
                           partisan=True, force=force, role="stand-in while the SoS dashboard can't be read"):
                print("or: no figures yet.")
            return 2
    if snap is not None:
        pass
    elif raw is None:
        tried = candidate_urls(cfg)
        print("OR: no live Daily Ballot Returns file yet (tried %d candidate URLs; the 2-week pre-election "
              "count window hasn't started). First tried: %s" % (len(tried), tried[0] if tried else "(none configured)"))
        snap = build_snapshot(cfg, {}, gidx, "")
    else:
        rows = parse_pdf(raw, county_names)
        if not rows:
            print("OR: fetched %s but found no recognizable county rows -- report layout may have changed; "
                  "not overwriting with an empty snapshot." % url, file=sys.stderr)
            return 2
        snap = build_snapshot(cfg, rows, gidx, url)

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
        print("CHANGED  url=%s  counties=%d  cast=%s" % (url or "(none)", len(snap["counties"]), cast.get("total")))
    else:
        print("NOCHANGE  (%d counties)" % len(snap["counties"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
