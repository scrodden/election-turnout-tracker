#!/usr/bin/env python3
"""Build a district map (assets/<out>.geojson) from an official redistricting
plan published as an ArcGIS feature service with a DISTRICT number field,
simplified for the web map. Used for Florida, whose 2026 congressional plan
(EOGPCRP2026, enacted May 2026) postdates the Census Bureau's January 1, 2026
district file; the Florida Senate's redistricting office (ArcGIS org FLSCOR)
hosts the enacted plans.

Feature properties: name "<prefix><n>", district_number, fips "<state fips>-<prefix><nnn>".

Run once per plan:
  python scripts/build_plan_geo.py fl-cd CD 12 https://services6.arcgis.com/WNLWyVVqYEXv5C4T/arcgis/rest/services/EOGPCRP2026_WFL1/FeatureServer/0
"""
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402


def main():
    out_name, prefix, fips, layer = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4].rstrip("/")
    q = urllib.parse.urlencode({"where": "1=1", "outFields": "DISTRICT", "returnGeometry": "true", "outSR": "4326",
                                "f": "geojson", "maxAllowableOffset": "0.002", "geometryPrecision": "4"})
    g = json.loads(C.http_get(layer + "/query?" + q, retries=2))
    feats = []
    for f in sorted(g["features"], key=lambda f: int(f["properties"]["DISTRICT"])):
        n = int(f["properties"]["DISTRICT"])
        feats.append({"type": "Feature", "geometry": f["geometry"],
                      "properties": {"name": "%s%d" % (prefix, n), "district_number": n, "fips": "%s-%s%03d" % (fips, prefix, n)}})
    if not feats:
        raise SystemExit("no districts returned from %s" % layer)
    path = os.path.join(ROOT, "assets", out_name + ".geojson")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": feats, "source": layer}, fh, separators=(",", ":"))
    print("wrote %s: %d districts, %d KB" % (path, len(feats), os.path.getsize(path) // 1024))


if __name__ == "__main__":
    main()
