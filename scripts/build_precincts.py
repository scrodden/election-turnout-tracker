#!/usr/bin/env python3
"""Build a statewide Florida precinct-boundary GeoJSON from per-county GIS
sources, normalizing precinct ids so they join to the TQV turnout feed.

This is run occasionally (precinct boundaries change rarely) -- NOT in the
10-minute updater. Coverage grows county-by-county as sources are added to
config/fl_precinct_sources.json.

Each source entry:
{
  "code": "BAY",                 # TQV/county code (matches config/fl_counties.json)
  "county": "Bay",
  "url": "https://.../FeatureServer/0",   # ArcGIS layer (query endpoint)
  "precinct_field": "PRECINCT",  # attribute holding the precinct id
  "where": "1=1",                # optional filter
  "strip_leading_zeros": true,   # optional; "014" -> "14" to match TQV
  "id_prefix_strip": null         # optional regex removed from the id
}

Output: assets/fl-precincts.geojson  (features: {code, county, precinct})
Verify: python scripts/build_precincts.py --verify   (compares ids to TQV)

Usage:
  python scripts/build_precincts.py                 # build all configured counties
  python scripts/build_precincts.py BAY OKA         # build only these
  python scripts/build_precincts.py --verify        # build + report id-join match
"""
import os
import re
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

SOURCES_PATH = os.path.join(ROOT, "config", "fl_precinct_sources.json")
OUT_PATH = os.path.join(ROOT, "assets", "fl-precincts.geojson")
PRECINCTS_ALL_PATH = os.path.join(ROOT, "data", "fl", "precincts_all.json")
MAX_OFFSET = 0.0003  # ~33m server-side simplification (degrees)
PAGE = 1000


def norm_id(raw, src):
    """Normalize a GIS precinct id to match the TQV turnout keys: strip an
    optional prefix, aggregate a split id ('14.42') to precinct level ('14')
    unless keep_split is set, then drop leading zeros on pure-numeric ids."""
    s = str(raw).strip()
    if src.get("id_prefix_strip"):
        s = re.sub(src["id_prefix_strip"], "", s).strip()
    if "." in s and not src.get("keep_split"):
        s = s.split(".")[0]
    if src.get("strip_leading_zeros", True) and re.match(r"^\d+$", s):
        s = str(int(s))
    return s


def round_coords(o, nd=5):
    if isinstance(o, list):
        return [round_coords(x, nd) for x in o]
    if isinstance(o, float):
        return round(o, nd)
    return o


def fetch_layer(src):
    """Fetch all features from an ArcGIS layer as GeoJSON (paged, simplified)."""
    base = src["url"].rstrip("/")
    field = src["precinct_field"]
    where = src.get("where", "1=1")
    feats, offset = [], 0
    while True:
        q = ("%s/query?where=%s&outFields=%s&returnGeometry=true&outSR=4326"
             "&f=geojson&maxAllowableOffset=%s&resultOffset=%d&resultRecordCount=%d"
             % (base, C_urlq(where), C_urlq(field), MAX_OFFSET, offset, PAGE))
        data = json.loads(C.http_get(q, no_cache=True, retries=3))
        batch = data.get("features", [])
        feats.extend(batch)
        if len(batch) < PAGE or data.get("properties", {}).get("exceededTransferLimit") is False:
            if len(batch) < PAGE:
                break
        if not batch:
            break
        offset += PAGE
        if offset > 20000:
            break
    return feats


def C_urlq(s):
    import urllib.parse
    return urllib.parse.quote(str(s), safe="")


def build(codes=None):
    with open(SOURCES_PATH, encoding="utf-8") as f:
        sources = json.load(f)["counties"]
    if codes:
        sources = [s for s in sources if s["code"] in codes]
    out_features = []
    report = []
    for src in sources:
        try:
            raw_feats = fetch_layer(src)
        except Exception as e:  # noqa: BLE001
            report.append((src["code"], "FETCH-FAIL", str(e)[:60]))
            continue
        ids = set()
        for ft in raw_feats:
            pid = norm_id(ft.get("properties", {}).get(src["precinct_field"], ""), src)
            if not pid:
                continue
            geom = ft.get("geometry")
            if not geom:
                continue
            ids.add(pid)
            out_features.append({
                "type": "Feature",
                "properties": {"code": src["code"], "county": src["county"], "precinct": pid},
                "geometry": {"type": geom["type"], "coordinates": round_coords(geom["coordinates"])},
            })
        report.append((src["code"], "OK", "%d polygons, %d precincts" % (len(raw_feats), len(ids))))

    fc = {"type": "FeatureCollection", "features": out_features}
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(fc, f, separators=(",", ":"))
    print("Wrote %s (%d features, %d bytes)" % (OUT_PATH, len(out_features), os.path.getsize(OUT_PATH)))
    for code, status, note in report:
        print("  %-4s %-11s %s" % (code, status, note))
    return fc


def verify(fc):
    """Compare geometry precinct ids to TQV turnout precinct ids (where we have
    turnout data) to catch id-format mismatches."""
    if not os.path.exists(PRECINCTS_ALL_PATH):
        print("no precincts_all.json to verify against"); return
    turnout = json.load(open(PRECINCTS_ALL_PATH, encoding="utf-8"))["counties"]
    geo_ids = {}
    for ft in fc["features"]:
        geo_ids.setdefault(ft["properties"]["code"], set()).add(ft["properties"]["precinct"])
    print("\nJoin check (counties with turnout data):")
    for code, precs in turnout.items():
        gids = geo_ids.get(code)
        if not gids:
            print("  %-4s no geometry yet" % code); continue
        tids = set(precs.keys())
        matched = tids & gids
        print("  %-4s %d/%d turnout precincts matched to geometry %s"
              % (code, len(matched), len(tids), "" if matched == tids else "MISSING:" + ",".join(sorted(tids - gids))))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    fc = build(args or None)
    if "--verify" in sys.argv:
        verify(fc)
