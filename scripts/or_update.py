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
    if raw is None:
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
