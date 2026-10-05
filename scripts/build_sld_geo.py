#!/usr/bin/env python3
"""Build assets/<st>-ss.geojson (state senate) or assets/<st>-sh.geojson (state
house) from the Census Bureau's TIGERweb service, layers "2026 State
Legislative Districts - Upper / Lower", simplified for the web map. For states
whose legislative maps didn't change after the Census vintage (check first;
Florida's plans come from scripts/build_plan_geo.py instead).

Feature properties: name "SD<n>" / "HD<n>", district_number, fips (GEOID).

Run once per chamber:  python scripts/build_sld_geo.py de 10 upper
             (custom name prefix / file suffix: ... az 04 upper LD ld)
"""
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

BASE = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Legislative/MapServer/%d/query"
CHAMBERS = {"upper": (1, "SD", "ss"), "lower": (2, "HD", "sh")}


def main():
    st, fips, chamber = sys.argv[1].lower(), sys.argv[2], sys.argv[3].lower()
    layer, prefix, suffix = CHAMBERS[chamber]
    if len(sys.argv) > 5:   # e.g. Arizona's shared legislative districts: ... upper LD ld
        prefix, suffix = sys.argv[4], sys.argv[5]
    q = urllib.parse.urlencode({"where": "STATE='%s'" % fips, "outFields": "BASENAME,GEOID,NAME", "returnGeometry": "true",
                                "outSR": "4326", "f": "geojson", "maxAllowableOffset": "0.002", "geometryPrecision": "4"})
    g = json.loads(C.http_get(BASE % layer + "?" + q, retries=2))
    feats = []
    for f in g["features"]:
        b = str(f["properties"].get("BASENAME") or "").strip()
        if not b.isdigit():   # e.g. "ZZZ" water-only placeholders
            continue
        n = int(b)
        feats.append({"type": "Feature", "geometry": f["geometry"],
                      "properties": {"name": "%s%d" % (prefix, n), "district_number": n, "fips": f["properties"]["GEOID"]}})
    feats.sort(key=lambda f: f["properties"]["district_number"])
    if not feats:
        raise SystemExit("no %s districts returned for state %s" % (chamber, fips))
    out = os.path.join(ROOT, "assets", "%s-%s.geojson" % (st, suffix))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": feats,
                   "source": "U.S. Census Bureau TIGERweb, 2026 State Legislative Districts (%s)" % chamber}, fh,
                  separators=(",", ":"))
    print("wrote %s: %d districts, %d KB" % (out, len(feats), os.path.getsize(out) // 1024))


if __name__ == "__main__":
    main()
