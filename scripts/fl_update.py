#!/usr/bin/env python3
"""Fetch Florida vote-by-mail / early-voting turnout, by county and party, and
write a snapshot + time-series history the static site reads.

Source: Florida Dept. of State, County Vote-by-Mail & Early Voting Reports.
  Stats page : https://countyfilesvbm-ev.floridados.gov/VoteByMailEarlyVotingReports/PublicStats
  Data files : https://electionfiles.floridados.gov/countyballotreportfiles/Stats_<election>_*.txt
Each file is tab-separated with columns:
  ElectionNumber, ElectionDate, ElectionName, CountyName, StatType,
  TotalRep, TotalDem, TotalOth, TotalNpa, GrandTotal, CompileDate

Outputs (relative to repo root):
  data/fl/latest.json      current snapshot with derived margins/shares
  data/fl/history.jsonl    one compact line per change (deduped by content hash)

Run:  python scripts/fl_update.py        (writes only when data changed)
      python scripts/fl_update.py --force (always rewrite latest.json)
Exit code 0 always on success; prints "CHANGED" or "NOCHANGE" for the workflow.
"""
import os
import re
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import common as C  # noqa: E402

STATE = "fl"
CONFIG_PATH = os.path.join(ROOT, "config", "fl.json")
GEO_PATH = os.path.join(ROOT, "assets", "fl-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")

VOTED_METHODS = ["mail_voted", "early_voted", "election_day"]
FILE_URL_RE = re.compile(
    r"https://electionfiles\.floridados\.gov/countyballotreportfiles/[^\"'\s<>]+\.txt",
    re.IGNORECASE,
)


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_fips():
    """name -> FIPS, from the bundled county GeoJSON."""
    with open(GEO_PATH, encoding="utf-8") as f:
        geo = json.load(f)
    return {ft["properties"]["name"]: ft["properties"]["fips"]
            for ft in geo["features"]}


def discover_file_urls(cfg):
    """Return the set of stats-file URLs. Primary: scrape the live stats page's
    'Download File' links (this auto-includes the early-voting file the day it
    appears). Fallback/union: the URLs named in config."""
    urls = set()
    try:
        html = C.http_get(cfg["sources"]["stats_page"], no_cache=True)
        for m in FILE_URL_RE.findall(html):
            if cfg["election"]["number"] in m:
                urls.add(m)
    except Exception as e:  # noqa: BLE001
        print("WARN: could not scrape stats page: %s" % e, file=sys.stderr)
    base = cfg["sources"]["file_base"]
    for fname in cfg["sources"]["files"].values():
        urls.add(base + fname)
    return sorted(urls)


def parse_stats_file(text, stat_type_map):
    """Parse one tab-separated stats file into rows keyed by method.

    Returns (counties, statewide) where:
      counties[name][method] = raw dict {rep,dem,oth,npa,compiled,compiled_iso}
      statewide[method]      = same, from the 'State Totals' row
    """
    counties, statewide = {}, {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 10 or not parts[0].strip().isdigit():
            continue  # header or malformed
        county = parts[3].strip()
        stat_type = parts[4].strip()
        method = stat_type_map.get(stat_type)
        if not method:
            continue  # unknown stat type -> ignore
        raw_c, iso_c = C.parse_compile_date(parts[10] if len(parts) > 10 else "")
        rec = {
            "rep": C.parse_number(parts[5]),
            "dem": C.parse_number(parts[6]),
            "oth": C.parse_number(parts[7]),
            "npa": C.parse_number(parts[8]),
            "compiled": raw_c, "compiled_iso": iso_c,
        }
        if county.lower() == "state totals":
            statewide[method] = rec
        else:
            counties.setdefault(county, {})[method] = rec
    return counties, statewide


def to_block(rec):
    if not rec:
        return None
    return C.party_block(rec["rep"], rec["dem"], rec["oth"], rec["npa"],
                         rec.get("compiled", ""), rec.get("compiled_iso", ""))


def build_entity(methods):
    """Given {method: raw_rec}, produce {method: block, ..., 'cast': block}."""
    out = {}
    for m, rec in methods.items():
        out[m] = to_block(rec)
    voted = [out[m] for m in VOTED_METHODS if out.get(m)]
    out["cast"] = C.add_blocks(*voted) if voted else C.party_block(0, 0, 0, 0)
    return out


def newest(*iso_values):
    vals = [v for v in iso_values if v]
    return max(vals) if vals else ""


def build_snapshot(cfg, fips_map, all_counties, all_statewide):
    counties_out = {}
    max_iso, max_raw = "", ""
    for name, fips in sorted(fips_map.items()):
        methods = all_counties.get(name, {})
        ent = build_entity(methods)
        ent["fips"] = fips
        counties_out[name] = ent
        for m, rec in methods.items():
            if rec.get("compiled_iso", "") > max_iso:
                max_iso, max_raw = rec["compiled_iso"], rec["compiled"]

    statewide_out = build_entity(all_statewide)
    for rec in all_statewide.values():
        if rec.get("compiled_iso", "") > max_iso:
            max_iso, max_raw = rec["compiled_iso"], rec["compiled"]

    methods_present = sorted(
        {m for c in all_counties.values() for m in c} |
        set(all_statewide.keys())
    )
    snap = {
        "state": STATE,
        "state_name": cfg["state_name"],
        "election": cfg["election"],
        "source_compiled": max_raw,
        "source_compiled_iso": max_iso,
        "methods_present": methods_present,
        "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide_out,
        "counties": counties_out,
    }
    return snap


def compact_entity(ent):
    """[rep,dem,oth,npa,total] arrays per method, for the small history file."""
    out = {}
    for m in ["mail_provided", "mail_voted", "early_voted", "election_day", "cast"]:
        b = ent.get(m)
        if b and b.get("total"):
            out[m] = [b["rep"], b["dem"], b["oth"], b["npa"], b["total"]]
    return out


def append_history(snap):
    rec = {
        "generated_at": snap["generated_at"],
        "compiled": snap["source_compiled"],
        "compiled_iso": snap["source_compiled_iso"],
        "data_hash": snap["data_hash"],
        "statewide": compact_entity(snap["statewide"]),
        "counties": {n: compact_entity(e) for n, e in snap["counties"].items()
                     if compact_entity(e)},
    }
    # Skip if the last history line already has this content hash.
    if os.path.exists(HISTORY_PATH):
        last = None
        with open(HISTORY_PATH, "rb") as f:
            try:
                f.seek(-4096, os.SEEK_END)
            except OSError:
                f.seek(0)
            tail = f.read().decode("utf-8", "replace").strip().splitlines()
            if tail:
                last = tail[-1]
        if last:
            try:
                if json.loads(last).get("data_hash") == rec["data_hash"]:
                    return False
            except ValueError:
                pass
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return True


def existing_hash():
    if not os.path.exists(LATEST_PATH):
        return None
    try:
        with open(LATEST_PATH, encoding="utf-8") as f:
            return json.load(f).get("data_hash")
    except (ValueError, OSError):
        return None


def main():
    force = "--force" in sys.argv
    cfg = load_config()
    fips_map = load_fips()

    urls = discover_file_urls(cfg)
    print("Fetching %d file(s):" % len(urls))
    all_counties, all_statewide = {}, {}
    got = 0
    for url in urls:
        try:
            text = C.http_get(url, no_cache=True)
        except Exception as e:  # noqa: BLE001 - EV file 404s until EV starts
            print("  - skip %s (%s)" % (url.rsplit("/", 1)[-1], e))
            continue
        counties, statewide = parse_stats_file(text, cfg["stat_type_map"])
        if not counties and not statewide:
            print("  - %s: no rows" % url.rsplit("/", 1)[-1])
            continue
        got += 1
        print("  - %s: %d counties, methods=%s"
              % (url.rsplit("/", 1)[-1], len(counties),
                 sorted({m for c in counties.values() for m in c})))
        for cn, md in counties.items():
            all_counties.setdefault(cn, {}).update(md)
        all_statewide.update(statewide)

    if got == 0:
        print("ERROR: no data files could be fetched.", file=sys.stderr)
        return 2

    snap = build_snapshot(cfg, fips_map, all_counties, all_statewide)
    # Hash the data only (exclude volatile generated_at / data_hash).
    snap["data_hash"] = C.data_hash(snap)
    snap["generated_at"] = C.utc_now_iso()

    prev = existing_hash()
    changed = force or (snap["data_hash"] != prev)

    os.makedirs(DATA_DIR, exist_ok=True)
    if changed:
        with open(LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        added = append_history(snap)
        sw = snap["statewide"]
        cast = sw.get("cast", {})
        print("CHANGED  compiled=%s  cast R/D=%s/%s margin=%s  history+=%s"
              % (snap["source_compiled"], cast.get("rep"), cast.get("dem"),
                 cast.get("margin"), added))
    else:
        print("NOCHANGE  compiled=%s (hash %s)"
              % (snap["source_compiled"], (prev or "")[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
