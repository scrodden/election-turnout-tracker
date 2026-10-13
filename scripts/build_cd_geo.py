#!/usr/bin/env python3
"""Build assets/<st>-cd.geojson (U.S. House districts for the 2026 elections)
from the Census Bureau's TIGERweb service, layer "120th Congressional
Districts" (January 1, 2026 vintage, which already has the 2025-26 redraws in
TX, CA, NC, OH, UT, FL, TN, LA and AL), simplified for the web map.

Exception: Missouri. Its 2025 map is in the 120th layer, but the U.S. Supreme
Court's September 25, 2026 stay keeps the 2022 map in use for the November
2026 general election (the 2025 map is subject to the Proposition A veto
referendum), so Missouri comes from the "119th Congressional Districts" layer.
(Virginia's April 2026 redraw was struck down in May; the 120th layer
correctly keeps its existing lines.)

Feature properties match assets/va-cd.geojson: name "CD<n>", district_number
(0 for an at-large seat), fips "<state fips>-<nn>". Census "ZZ" water-only
areas (Great Lakes / Long Island Sound) are left out.

  python scripts/build_cd_geo.py ga 13     one state (usps, fips)
  python scripts/build_cd_geo.py --all     every state without a file yet
  python scripts/build_cd_geo.py --all --force   rebuild every state
"""
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

SERVICE = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Legislative/MapServer/"
LAYER = (0, "CD120", "U.S. Census Bureau TIGERweb, 120th Congressional Districts (January 1, 2026 vintage)")
OVERRIDE = {
    "mo": (8, "CD119", "U.S. Census Bureau TIGERweb, 119th Congressional Districts (Missouri's 2022 map, kept in use "
                       "for the November 2026 general election by the U.S. Supreme Court's September 25, 2026 stay)"),
}
FIPS = {"al": "01", "ak": "02", "az": "04", "ar": "05", "ca": "06", "co": "08", "ct": "09", "de": "10", "fl": "12",
        "ga": "13", "hi": "15", "id": "16", "il": "17", "in": "18", "ia": "19", "ks": "20", "ky": "21", "la": "22",
        "me": "23", "md": "24", "ma": "25", "mi": "26", "mn": "27", "ms": "28", "mo": "29", "mt": "30", "ne": "31",
        "nv": "32", "nh": "33", "nj": "34", "nm": "35", "ny": "36", "nc": "37", "nd": "38", "oh": "39", "ok": "40",
        "or": "41", "pa": "42", "ri": "44", "sc": "45", "sd": "46", "tn": "47", "tx": "48", "ut": "49", "vt": "50",
        "va": "51", "wa": "53", "wv": "54", "wi": "55", "wy": "56"}


def build(st, fips):
    layer, field, source = OVERRIDE.get(st, LAYER)
    q = urllib.parse.urlencode({
        "where": "STATE='%s'" % fips, "outFields": field + ",NAME", "returnGeometry": "true",
        "outSR": "4326", "f": "geojson", "maxAllowableOffset": "0.002", "geometryPrecision": "4"})
    g = json.loads(C.http_get(SERVICE + "%d/query?" % layer + q, retries=2, timeout=120))
    feats = []
    for f in g["features"]:
        code = f["properties"][field]
        if not str(code).isdigit():   # 'ZZ' = water not in any district
            continue
        n = int(code)
        feats.append({"type": "Feature", "geometry": f["geometry"],
                      "properties": {"name": "CD%d" % n, "district_number": n, "fips": "%s-%02d" % (fips, n)}})
    feats.sort(key=lambda f: f["properties"]["district_number"])
    if not feats:
        raise SystemExit("no districts returned for state %s" % fips)
    out = os.path.join(ROOT, "assets", "%s-cd.geojson" % st)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": feats, "source": source}, fh, separators=(",", ":"))
    print("wrote %s: %d districts, %d KB" % (out, len(feats), os.path.getsize(out) // 1024))
    return len(feats)


def main():
    args = sys.argv[1:]
    if "--all" in args:
        force = "--force" in args
        for st, fips in FIPS.items():
            if force or not os.path.exists(os.path.join(ROOT, "assets", "%s-cd.geojson" % st)):
                build(st, fips)
        return
    build(args[0].lower(), args[1])


if __name__ == "__main__":
    main()
