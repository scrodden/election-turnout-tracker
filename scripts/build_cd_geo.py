#!/usr/bin/env python3
"""Build assets/<st>-cd.geojson (U.S. House districts for the 2026 elections)
from the Census Bureau's TIGERweb service, layer "120th Congressional
Districts" (January 1, 2026 vintage), simplified for the web map.

Feature properties match assets/va-cd.geojson: name "CD<n>", district_number,
fips "<state fips>-<nn>".

Run once per state (the boundaries don't change during the cycle):
  python scripts/build_cd_geo.py ga 13
"""
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

LAYER = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Legislative/MapServer/0/query"


def main():
    st, fips = sys.argv[1].lower(), sys.argv[2]
    q = urllib.parse.urlencode({
        "where": "STATE='%s'" % fips, "outFields": "CD120,NAME", "returnGeometry": "true",
        "outSR": "4326", "f": "geojson", "maxAllowableOffset": "0.003", "geometryPrecision": "4"})
    g = json.loads(C.http_get(LAYER + "?" + q, retries=2))
    feats = []
    for f in sorted(g["features"], key=lambda f: int(f["properties"]["CD120"])):
        n = int(f["properties"]["CD120"])
        feats.append({"type": "Feature", "geometry": f["geometry"],
                      "properties": {"name": "CD%d" % n, "district_number": n, "fips": "%s-%02d" % (fips, n)}})
    if not feats:
        raise SystemExit("no districts returned for state %s" % fips)
    out = os.path.join(ROOT, "assets", "%s-cd.geojson" % st)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": feats,
                   "source": "U.S. Census Bureau TIGERweb, 120th Congressional Districts (January 1, 2026 vintage)"},
                  fh, separators=(",", ":"))
    print("wrote %s: %d districts, %d KB" % (out, len(feats), os.path.getsize(out) // 1024))


if __name__ == "__main__":
    main()
