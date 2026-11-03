#!/usr/bin/env python3
"""Build assets/crosswalks/fl-precinct-cd.json: each Florida precinct's share in each 2026 congressional district.

A one-time build from the two boundary files the site already ships (assets/fl-precincts.geojson and
assets/fl-cd.geojson, plan EOGPCRP2026): a 12x12 grid of points over each precinct, each point inside the precinct
tested against the districts (and each county the same way, under "_county", for precincts the boundary file
lacks, e.g. Indian River or precincts added since). Shares under 5% are boundary noise and dropped. fl_cd_eday.py
uses it to put each county's Election Day check-ins (by party, per polling place, from TQV) into districts.

Run:  python scripts/build_fl_precinct_cd.py
"""
import json
import os
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "assets", "crosswalks", "fl-precinct-cd.json")
GRID = 12
MIN_SHARE = 0.05


def polygons(geom):
    if geom["type"] == "Polygon":
        return [geom["coordinates"]]
    return geom["coordinates"] if geom["type"] == "MultiPolygon" else []


def in_ring(x, y, ring):
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi:
            inside = not inside
        j = i
    return inside


def inside(x, y, polys):
    return any(in_ring(x, y, p[0]) and not any(in_ring(x, y, h) for h in p[1:]) for p in polys)


def bbox(polys):
    xs = [pt[0] for p in polys for pt in p[0]]
    ys = [pt[1] for p in polys for pt in p[0]]
    return min(xs), min(ys), max(xs), max(ys)


def main():
    with open(os.path.join(ROOT, "assets", "fl-cd.geojson"), encoding="utf-8") as f:
        cds = [(str(ft["properties"]["district_number"]), polygons(ft["geometry"])) for ft in json.load(f)["features"]]
    cds = [(n, p, bbox(p)) for n, p in cds]
    with open(os.path.join(ROOT, "assets", "fl-precincts.geojson"), encoding="utf-8") as f:
        precincts = json.load(f)["features"]
    with open(os.path.join(ROOT, "config", "fl_counties.json"), encoding="utf-8") as f:
        code_of = {c["fips"]: c["code"] for c in json.load(f)["counties"]}
    with open(os.path.join(ROOT, "assets", "fl-counties.geojson"), encoding="utf-8") as f:
        for ft in json.load(f)["features"]:
            ft["properties"] = {"code": code_of[ft["properties"]["fips"]], "precinct": "_county"}
            precincts.append(ft)
    out = defaultdict(dict)
    for ft in precincts:
        polys = polygons(ft["geometry"])
        if not polys:
            continue
        x0, y0, x1, y1 = bbox(polys)
        near = [c for c in cds if not (c[2][2] < x0 or c[2][0] > x1 or c[2][3] < y0 or c[2][1] > y1)]
        hits = defaultdict(int)
        pts = [(x0 + (i + 0.5) * (x1 - x0) / GRID, y0 + (j + 0.5) * (y1 - y0) / GRID) for i in range(GRID) for j in range(GRID)]
        pts = [p for p in pts if inside(p[0], p[1], polys)] or [((x0 + x1) / 2, (y0 + y1) / 2)]   # slivers: the middle
        for x, y in pts:
            for n, cpolys, _b in near:
                if inside(x, y, cpolys):
                    hits[n] += 1
                    break
        tot = sum(hits.values())
        keep = {n: h / tot for n, h in hits.items() if tot and h / tot >= MIN_SHARE}
        s = sum(keep.values())
        if s:
            p = ft["properties"]
            out[p["code"]][str(p["precinct"]).strip()] = {n: round(v / s, 3) for n, v in sorted(keep.items(), key=lambda kv: int(kv[0]))}
    doc = {"_source": "Built by scripts/build_fl_precinct_cd.py from assets/fl-precincts.geojson and assets/fl-cd.geojson "
                      "(congressional plan EOGPCRP2026): each precinct's share of area-sampled points in each district.",
           "counties": {c: out[c] for c in sorted(out)}}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, separators=(",", ":"))
    n = sum(len(v) - 1 for v in out.values())
    multi = sum(1 for v in out.values() for s in v.values() if len(s) > 1)
    print("fl precinct->CD: %d precincts in %d counties (%d in more than one district)" % (n, len(out), multi))


if __name__ == "__main__":
    main()
