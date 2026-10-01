#!/usr/bin/env python3
"""Build assets/<st>-towns.geojson (cities and towns = Census county
subdivisions) from the Census Bureau's TIGERweb service, layer "County
Subdivisions" (current vintage), simplified for the web map. For New England
states that run elections by city/town (e.g. Rhode Island).

Feature properties: name (e.g. "Barrington", "New Shoreham"), fips (10-digit
county subdivision GEOID).

Run once per state:  python scripts/build_town_geo.py ri 44
"""
import json
import os
import re
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

LAYER = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Places_CouSub_ConCity_SubMCD/MapServer/1/query"


def main():
    st, fips = sys.argv[1].lower(), sys.argv[2]
    q = urllib.parse.urlencode({
        "where": "STATE='%s'" % fips, "outFields": "NAME,BASENAME,GEOID,FUNCSTAT", "returnGeometry": "true",
        "outSR": "4326", "f": "geojson", "maxAllowableOffset": "0.0015", "geometryPrecision": "4"})
    g = json.loads(C.http_get(LAYER + "?" + q, retries=2))
    feats = []
    for f in sorted(g["features"], key=lambda f: f["properties"]["BASENAME"]):
        p = f["properties"]
        if re.search(r"not defined", p.get("NAME", ""), re.I) or not f.get("geometry"):
            continue
        feats.append({"type": "Feature", "geometry": f["geometry"],
                      "properties": {"name": p["BASENAME"], "fips": p["GEOID"]}})
    if not feats:
        raise SystemExit("no county subdivisions returned for state %s" % fips)
    out = os.path.join(ROOT, "assets", "%s-towns.geojson" % st)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": feats,
                   "source": "U.S. Census Bureau TIGERweb, County Subdivisions"}, fh, separators=(",", ":"))
    print("wrote %s: %d cities/towns, %d KB" % (out, len(feats), os.path.getsize(out) // 1024))


if __name__ == "__main__":
    main()
