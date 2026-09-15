#!/usr/bin/env python3
"""Fill statewide precinct-map coverage from the VEST 2020 Florida precinct
shapefile (Harvard Dataverse, CC-licensed) for every county not already
covered by a current per-county ArcGIS source.

Why: ArcGIS Online search only finds counties that publish to the ArcGIS cloud;
many counties self-host or don't publish. VEST is one statewide file (all 67
counties, NAD83 degrees, COUNTY = the same 3-letter codes as TQV, PRECINCT =
precinct id). Precinct boundaries drift over time, so a county is only filled
from VEST when its VEST precinct ids still match the county's live TQV roster
above MATCH_MIN; counties that redistricted since 2020 keep their current
ArcGIS source (build_precincts.py) or are left for manual sourcing.

Pipeline (manual, occasional):
  python scripts/build_precincts.py            # current ArcGIS counties
  python scripts/build_precincts_vest.py       # add VEST for the rest
Merges into assets/fl-precincts.geojson (ArcGIS-sourced counties are kept).
"""
import os
import re
import sys
import json
import struct
import zipfile
import tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

OUT_PATH = os.path.join(ROOT, "assets", "fl-precincts.geojson")
COUNTIES_PATH = os.path.join(ROOT, "config", "fl_counties.json")
CONFIG_PATH = os.path.join(ROOT, "config", "fl.json")
IDCACHE_PATH = os.path.join(ROOT, "data", "fl", "_tqv_ids.json")
VEST_URL = "https://dataverse.harvard.edu/api/access/datafile/12070362"  # fl_2020.zip
CACHE_ZIP = os.path.join(tempfile.gettempdir(), "vest_fl_2020.zip")
MATCH_MIN = 0.70
SIMPLIFY_TOL = 0.0012   # degrees (~130 m)
COORD_NDIGITS = 5


# ---- id normalization (must match build_precincts / turnout aggregation) ----
def norm(s):
    s = str(s).strip()
    if "." in s:
        s = s.split(".")[0]
    return str(int(s)) if re.match(r"^\d+$", s) else s.upper()


# ---- DBF ------------------------------------------------------------------
def read_dbf(buf):
    numrec = struct.unpack("<I", buf[4:8])[0]
    hdrsize = struct.unpack("<H", buf[8:10])[0]
    recsize = struct.unpack("<H", buf[10:12])[0]
    fields, off = [], 32
    while buf[off] != 0x0D:
        name = buf[off:off + 11].split(b"\x00")[0].decode("latin-1")
        flen = buf[off + 16]
        fields.append((name, flen))
        off += 32
    rows = []
    for i in range(numrec):
        r = buf[hdrsize + i * recsize: hdrsize + (i + 1) * recsize]
        o, rec = 1, {}
        for name, flen in fields:
            rec[name] = r[o:o + flen].decode("latin-1").strip()
            o += flen
        rows.append(rec)
    return rows


# ---- SHP (polygons only) --------------------------------------------------
def read_shp_polygons(buf):
    """Yield list-of-rings per record (record order matches the DBF)."""
    n = len(buf)
    off = 100  # file header
    out = []
    while off < n:
        # record header: recNum (BE i32), contentLen (BE i32, 16-bit words)
        content_len = struct.unpack(">i", buf[off + 4:off + 8])[0]
        start = off + 8
        shape_type = struct.unpack("<i", buf[start:start + 4])[0]
        if shape_type in (5, 15, 25):  # Polygon / PolygonZ / PolygonM
            p = start + 4 + 32  # skip shape type + bbox(4 doubles)
            num_parts = struct.unpack("<i", buf[p:p + 4])[0]; p += 4
            num_points = struct.unpack("<i", buf[p:p + 4])[0]; p += 4
            parts = list(struct.unpack("<%di" % num_parts, buf[p:p + 4 * num_parts]))
            p += 4 * num_parts
            coords = struct.unpack("<%dd" % (2 * num_points), buf[p:p + 16 * num_points])
            rings = []
            bounds = parts + [num_points]
            for k in range(num_parts):
                ring = []
                for j in range(bounds[k], bounds[k + 1]):
                    ring.append((coords[2 * j], coords[2 * j + 1]))
                rings.append(ring)
            out.append(rings)
        else:
            out.append([])  # null / non-polygon
        off = start + content_len * 2
    return out


# ---- Douglas-Peucker simplification ---------------------------------------
def _dp(points, tol):
    if len(points) < 3:
        return points
    dmax, idx = 0.0, 0
    ax, ay = points[0]; bx, by = points[-1]
    dx, dy = bx - ax, by - ay
    seglen2 = dx * dx + dy * dy
    for i in range(1, len(points) - 1):
        px, py = points[i]
        if seglen2 == 0:
            d = ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
        else:
            t = ((px - ax) * dx + (py - ay) * dy) / seglen2
            t = max(0, min(1, t))
            cx, cy = ax + t * dx, ay + t * dy
            d = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
        if d > dmax:
            dmax, idx = d, i
    if dmax > tol:
        left = _dp(points[:idx + 1], tol)
        right = _dp(points[idx:], tol)
        return left[:-1] + right
    return [points[0], points[-1]]


def simplify_ring(ring, tol):
    if len(ring) < 4:
        r = ring
    else:
        r = _dp(ring, tol)
        if len(r) < 4:
            r = ring[:: max(1, len(ring) // 8)]
    return [[round(x, COORD_NDIGITS), round(y, COORD_NDIGITS)] for x, y in r]


# ---- TQV rosters ----------------------------------------------------------
def load_rosters(codes):
    cfg = json.load(open(CONFIG_PATH, encoding="utf-8"))
    ids = json.load(open(IDCACHE_PATH, encoding="utf-8"))
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
    # source shapefile (cache the download)
    if not os.path.exists(CACHE_ZIP):
        print("Downloading VEST FL 2020 shapefile...")
        data = C.http_get(VEST_URL, binary=True, timeout=180, retries=3)
        with open(CACHE_ZIP, "wb") as f:
            f.write(data)
    z = zipfile.ZipFile(CACHE_ZIP)
    name = [n for n in z.namelist() if n.endswith(".shp")][0][:-4]
    rows = read_dbf(z.read(name + ".dbf"))
    geoms = read_shp_polygons(z.read(name + ".shp"))
    print("VEST records: %d dbf / %d shp" % (len(rows), len(geoms)))

    # group VEST precincts by county
    by_county = {}
    for rec, rings in zip(rows, geoms):
        if not rings:
            continue
        code = rec.get("COUNTY", "").strip().upper()
        pid = norm(rec.get("PRECINCT", ""))
        if code and pid:
            by_county.setdefault(code, []).append((pid, rings))

    # existing coverage (from ArcGIS build) — keep those counties as-is
    existing = json.load(open(OUT_PATH, encoding="utf-8")) if os.path.exists(OUT_PATH) \
        else {"type": "FeatureCollection", "features": []}
    have = {ft["properties"]["code"] for ft in existing["features"]}
    counties = {c["code"]: c["name"] for c in json.load(open(COUNTIES_PATH, encoding="utf-8"))["counties"]}

    candidates = [code for code in by_county if code not in have]
    rosters = load_rosters(candidates)

    added, skipped = [], []
    new_features = list(existing["features"])
    for code in sorted(candidates):
        precs = by_county[code]
        vest_ids = {pid for pid, _ in precs}
        roster = rosters.get(code, set())
        matched = len(vest_ids & roster) if roster else 0
        rate = matched / len(roster) if roster else 0.0
        if roster and rate < MATCH_MIN:
            skipped.append((code, rate, len(vest_ids), len(roster)))
            continue
        for pid, rings in precs:
            simp = [simplify_ring(r, SIMPLIFY_TOL) for r in rings]
            simp = [r for r in simp if len(r) >= 4]
            if not simp:
                continue
            new_features.append({
                "type": "Feature",
                "properties": {"code": code, "county": counties.get(code, code),
                               "precinct": pid, "src": "vest2020"},
                "geometry": {"type": "MultiPolygon", "coordinates": [[r] for r in simp]},
            })
        added.append((code, rate, len(precs)))

    fc = {"type": "FeatureCollection", "features": new_features}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(fc, f, separators=(",", ":"))
    mb = os.path.getsize(OUT_PATH) / 1048576
    total_codes = {ft["properties"]["code"] for ft in new_features}
    print("\nAdded %d counties from VEST, skipped %d (low match)." % (len(added), len(skipped)))
    for code, rate, n in added:
        print("  + %-4s %-13s %3d precincts  (roster match %.0f%%)" % (code, counties.get(code, ""), n, rate * 100))
    print("Skipped (redistricted since 2020 -> need current GIS):")
    for code, rate, nv, nr in skipped:
        print("  - %-4s %-13s match %.0f%% (VEST %d / roster %d)" % (code, counties.get(code, ""), rate * 100, nv, nr))
    print("\nTotal coverage: %d/67 counties, %d features, %.2f MB"
          % (len(total_codes), len(new_features), mb))
    missing = sorted(set(counties) - total_codes)
    print("Still missing (%d): %s" % (len(missing), ", ".join(missing)))


if __name__ == "__main__":
    main()
