#!/usr/bin/env python3
"""Fill any remaining precinct-map counties from FreshTake.Vote's current (2026)
per-county precinct paths, for counties not covered by a current ArcGIS source
or VEST.

FreshTake publishes, per county, `data/precincts/<CODE>.json` =
  {county, viewBox, precincts: {precinct_id: {name, d: "<SVG path>"}}}
The SVG "d" is drawn in a cos-corrected equirectangular projection fit to the
county's geographic bounding box (verified: viewBox aspect == (dlon*cos(midlat))
/dlat). So each SVG (x,y) inverts to lon/lat using the county's Census bbox:
  lon = minlon + x/W*(maxlon-minlon)
  lat = maxlat - y/H*(maxlat-minlat)
Precinct ids are the same ids TQV uses, so they join to the turnout data.

Run after build_precincts.py (ArcGIS) and build_precincts_vest.py (VEST):
  python scripts/build_precincts_freshtake.py            # all still-missing counties
  python scripts/build_precincts_freshtake.py LEE MRN    # specific codes
Merges into assets/fl-precincts.geojson (existing counties kept).
"""
import os
import re
import sys
import json
import math
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

OUT_PATH = os.path.join(ROOT, "assets", "fl-precincts.geojson")
GEO_PATH = os.path.join(ROOT, "assets", "fl-counties.geojson")
COUNTIES_PATH = os.path.join(ROOT, "config", "fl_counties.json")
CONFIG_PATH = os.path.join(ROOT, "config", "fl.json")
IDCACHE_PATH = os.path.join(ROOT, "data", "fl", "_tqv_ids.json")
FT_BASE = "https://www.freshtake.vote/data/precincts/"
MATCH_MIN = 0.60
COORD_NDIGITS = 5


def norm(s):
    s = str(s).strip()
    if "." in s:
        s = s.split(".")[0]
    return str(int(s)) if re.match(r"^\d+$", s) else s.upper()


def county_bboxes():
    geo = json.load(open(GEO_PATH, encoding="utf-8"))
    out = {}
    for ft in geo["features"]:
        mnx = mny = 1e9; mxx = mxy = -1e9

        def walk(o):
            nonlocal mnx, mny, mxx, mxy
            if isinstance(o, list) and o and isinstance(o[0], (int, float)):
                mnx = min(mnx, o[0]); mxx = max(mxx, o[0]); mny = min(mny, o[1]); mxy = max(mxy, o[1])
            elif isinstance(o, list):
                for x in o:
                    walk(x)
        walk(ft["geometry"]["coordinates"])
        out[ft["properties"]["fips"]] = (mnx, mny, mxx, mxy)
    return out


def parse_svg_rings(d):
    """Absolute M/L/Z path -> list of rings [[x,y],...]."""
    rings, cur = [], None
    for cmd, args in re.findall(r"([MLZmlz])([^MLZmlz]*)", d):
        nums = re.findall(r"-?\d*\.?\d+", args)
        pts = [(float(nums[i]), float(nums[i + 1])) for i in range(0, len(nums) - 1, 2)]
        u = cmd.upper()
        if u == "M":
            if cur:
                rings.append(cur)
            cur = list(pts)
        elif u == "L":
            if cur is None:
                cur = []
            cur.extend(pts)
        elif u == "Z":
            if cur:
                rings.append(cur); cur = None
    if cur:
        rings.append(cur)
    return rings


def load_rosters(codes, cfg, ids):
    base = cfg["tqv"]["base"]

    def roster(code):
        eid = ids.get(code)
        if not eid:
            return code, set()
        try:
            d = json.loads(C.http_get("%s%s/%s/data.json" % (base, code, eid), no_cache=True, retries=2))
        except Exception:  # noqa: BLE001
            return code, set()
        t = d.get("Turnout", {}) or {}
        s = set()
        for src in (d.get("PrecinctSplit") or {}), (t.get("PrecinctType") or {}):
            for k in src:
                s.add(norm(k))
        return code, {x for x in s if x}

    out = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for code, r in ex.map(roster, codes):
            out[code] = r
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    counties = {c["code"]: c for c in json.load(open(COUNTIES_PATH, encoding="utf-8"))["counties"]}
    cfg = json.load(open(CONFIG_PATH, encoding="utf-8"))
    ids = json.load(open(IDCACHE_PATH, encoding="utf-8"))
    bboxes = county_bboxes()

    existing = json.load(open(OUT_PATH, encoding="utf-8")) if os.path.exists(OUT_PATH) \
        else {"type": "FeatureCollection", "features": []}
    have = {ft["properties"]["code"] for ft in existing["features"]}
    todo = args or sorted(set(counties) - have)
    print("FreshTake fill for %d counties: %s" % (len(todo), ", ".join(todo)))

    rosters = load_rosters(todo, cfg, ids)
    features = list(existing["features"])
    added, skipped = [], []
    for code in todo:
        c = counties[code]
        bbox = bboxes.get(c["fips"])
        try:
            d = json.loads(C.http_get(FT_BASE + code + ".json", no_cache=True, retries=2))
        except Exception as e:  # noqa: BLE001
            skipped.append((code, "fetch-fail")); continue
        vb = d["viewBox"].split(); W = float(vb[2]); H = float(vb[3])
        minlon, minlat, maxlon, maxlat = bbox
        dlon, dlat = maxlon - minlon, maxlat - minlat
        precs = d.get("precincts", {})
        roster = rosters.get(code, set())
        pid_norm = {norm(pid) for pid in precs}
        # roster is split-level for some counties, so match can read low even when
        # freshtake's precinct-level geometry is correct; --force keeps it anyway.
        rate = (len(pid_norm & roster) / len(roster)) if roster else 1.0
        if roster and rate < MATCH_MIN and "--force" not in sys.argv:
            skipped.append((code, "match %.0f%%" % (rate * 100))); continue
        cnt = 0
        for pid, obj in precs.items():
            rings = parse_svg_rings(obj.get("d", ""))
            polys = []
            for ring in rings:
                if len(ring) < 3:
                    continue
                ll = [[round(minlon + x / W * dlon, COORD_NDIGITS),
                       round(maxlat - y / H * dlat, COORD_NDIGITS)] for x, y in ring]
                if ll[0] != ll[-1]:
                    ll.append(ll[0])
                if len(ll) >= 4:
                    polys.append([ll])
            if not polys:
                continue
            features.append({
                "type": "Feature",
                "properties": {"code": code, "county": c["name"],
                               "precinct": norm(pid), "src": "freshtake"},
                "geometry": {"type": "MultiPolygon", "coordinates": polys},
            })
            cnt += 1
        added.append((code, cnt, rate))

    fc = {"type": "FeatureCollection", "features": features}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(fc, f, separators=(",", ":"))
    mb = os.path.getsize(OUT_PATH) / 1048576
    total = {ft["properties"]["code"] for ft in features}
    print("\nAdded %d counties from FreshTake:" % len(added))
    for code, cnt, rate in added:
        print("  + %-4s %-13s %3d precincts (match %.0f%%)" % (code, counties[code]["name"], cnt, rate * 100))
    if skipped:
        print("Skipped:", ", ".join("%s(%s)" % (c, why) for c, why in skipped))
    print("\nTotal coverage: %d/67 counties, %d features, %.2f MB" % (len(total), len(features), mb))
    print("Still missing:", ", ".join(sorted(set(counties) - total)) or "none - all 67 covered")


if __name__ == "__main__":
    main()
