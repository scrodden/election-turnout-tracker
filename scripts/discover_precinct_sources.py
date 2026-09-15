#!/usr/bin/env python3
"""Discover and VERIFY per-county precinct-boundary GIS sources for the
statewide precinct map.

For each county it:
  1. Pulls the county's TQV precinct roster (ground truth ids) from data.json.
  2. Searches ArcGIS Online for candidate precinct feature layers.
  3. For each candidate polygon layer + precinct-like field, samples the ids and
     measures overlap with the TQV roster.
  4. Accepts the best candidate when overlap >= THRESHOLD and appends it to
     config/fl_precinct_sources.json (existing/hand-verified entries are kept).

Run:  python scripts/discover_precinct_sources.py                # all missing counties
      python scripts/discover_precinct_sources.py BAY OKA LEE    # only these codes
      python scripts/discover_precinct_sources.py --dry          # report only, no config write

This does NOT fetch geometry; after it updates the config, run
`python scripts/build_precincts.py --verify` to build the GeoJSON.
"""
import os
import re
import sys
import json
import math
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

CONFIG_PATH = os.path.join(ROOT, "config", "fl.json")
COUNTIES_PATH = os.path.join(ROOT, "config", "fl_counties.json")
GEO_PATH = os.path.join(ROOT, "assets", "fl-counties.geojson")


def _merc_to_lonlat(x, y):
    lon = x / 20037508.34 * 180.0
    lat = y / 20037508.34 * 180.0
    lat = 180.0 / math.pi * (2 * math.atan(math.exp(lat * math.pi / 180.0)) - math.pi / 2)
    return lon, lat


def county_bboxes():
    """code/name -> (minlon,minlat,maxlon,maxlat) from the bundled county geojson."""
    geo = json.load(open(GEO_PATH, encoding="utf-8"))
    out = {}
    for ft in geo["features"]:
        mnx = mny = 1e9; mxx = mxy = -1e9

        def walk(o):
            nonlocal mnx, mny, mxx, mxy
            if isinstance(o, list) and o and isinstance(o[0], (int, float)):
                mnx = min(mnx, o[0]); mxx = max(mxx, o[0])
                mny = min(mny, o[1]); mxy = max(mxy, o[1])
            elif isinstance(o, list):
                for x in o:
                    walk(x)
        walk(ft["geometry"]["coordinates"])
        out[ft["properties"]["name"]] = (mnx, mny, mxx, mxy)
    return out


def layer_extent_lonlat(meta):
    """Return (minlon,minlat,maxlon,maxlat) for a layer's extent, or None."""
    ext = meta.get("extent") or meta.get("fullExtent")
    if not ext or ext.get("xmin") is None:
        return None
    wkid = ((ext.get("spatialReference") or {}).get("latestWkid")
            or (ext.get("spatialReference") or {}).get("wkid"))
    xmin, ymin, xmax, ymax = ext["xmin"], ext["ymin"], ext["xmax"], ext["ymax"]
    if wkid in (102100, 3857, 900913):
        xmin, ymin = _merc_to_lonlat(xmin, ymin)
        xmax, ymax = _merc_to_lonlat(xmax, ymax)
    elif wkid not in (4326, None):
        return None  # unknown projection; skip the check
    return (xmin, ymin, xmax, ymax)


def bbox_overlaps(a, b, pad=0.3):
    if not a or not b:
        return True  # can't check -> don't block
    return not (a[2] < b[0] - pad or a[0] > b[2] + pad or a[3] < b[1] - pad or a[1] > b[3] + pad)
SOURCES_PATH = os.path.join(ROOT, "config", "fl_precinct_sources.json")
IDCACHE_PATH = os.path.join(ROOT, "data", "fl", "_tqv_ids.json")
THRESHOLD = 0.70
MAX_CANDIDATES = 6
MAX_WORKERS = 6


def load(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def precinct_key(s):
    s = str(s).strip()
    return s.split(".")[0] if "." in s else s


def norm(s):
    s = precinct_key(s)
    return str(int(s)) if re.match(r"^\d+$", s) else s.upper()


def tqv_universe(cfg, code, eid):
    """Precinct-level id set for a county from its TQV data.json."""
    base = cfg["tqv"]["base"]
    try:
        d = json.loads(C.http_get("%s%s/%s/data.json" % (base, code, eid), no_cache=True, retries=2))
    except Exception:  # noqa: BLE001
        return set()
    t = d.get("Turnout", {}) or {}
    ids = set()
    for src in (d.get("PrecinctSplit") or {}), (t.get("PrecinctType") or {}):
        for k in src.keys():
            ids.add(norm(k))
    return {i for i in ids if i}


def arcgis_search(query):
    q = urllib.parse.quote(query + " type:(Feature Service)", safe="")
    url = ("https://www.arcgis.com/sharing/rest/search?q=%s&f=json&num=%d"
           "&sortField=numviews&sortOrder=desc" % (q, MAX_CANDIDATES))
    try:
        d = json.loads(C.http_get(url, retries=2))
        return [r["url"] for r in d.get("results", []) if r.get("url")]
    except Exception:  # noqa: BLE001
        return []


def polygon_layers(service_url):
    """Return [(layer_url, [field_names], extent_lonlat)] for polygon layers."""
    out = []
    try:
        meta = json.loads(C.http_get(service_url + "?f=json", retries=1, timeout=25))
    except Exception:  # noqa: BLE001
        return out
    layers = meta.get("layers")
    if layers is None and meta.get("type"):  # url already points at a layer
        layers = [{"id": None}]
    for lyr in (layers or []):
        lid = lyr.get("id")
        lurl = service_url if lid is None else "%s/%d" % (service_url, lid)
        try:
            lm = json.loads(C.http_get(lurl + "?f=json", retries=1, timeout=25))
        except Exception:  # noqa: BLE001
            continue
        if lm.get("geometryType") not in ("esriGeometryPolygon", None):
            continue
        fields = [f["name"] for f in lm.get("fields", [])]
        if fields:
            out.append((lurl, fields, layer_extent_lonlat(lm)))
    return out


# reject demographic / geometry / admin fields that can numerically alias
# precinct numbers (e.g. PCT_MNRTY = percent minority, PCTUNDER18 = percent u18)
DENY_RE = re.compile(
    r"percent|pct_|_pct|under|over|mnrty|minorit|pop|vap|area|length|shape|dens|"
    r"black|white|hisp|asian|latin|male|female|age|median|avg|mean|rate|share|"
    r"ratio|income|hous|renter|owner|educ|bach|grad|emp|povert|margin|dem|rep|npa|"
    r"total|count|votes|turnout|object|global|edit|_date|_user|addr|zip|lat|lon|"
    r"acre|sqmi",
    re.I)
# a field only counts as a precinct id if its NAME says so (verification alone
# is fooled by demographic percentages that happen to fall in the precinct range)
PREC_NAME_RE = re.compile(r"precinct", re.I)
EXACT_SET = {"PCT", "PCTNUM", "PCTNBR", "PCTID", "VTD", "VTDST", "VTDKEY",
             "PREC", "PRECID", "PRECINCT", "PRECINCTID", "SPLIT", "SPLITID"}


def candidate_fields(fields):
    ok = [f for f in fields if not DENY_RE.search(f)]
    strong = [f for f in ok if PREC_NAME_RE.search(f) or f.upper() in EXACT_SET]
    generic = [f for f in ok if f.upper() in ("ID", "NAME", "NUMBER", "NUM")]
    seen, ordered = set(), []
    for f in strong + generic:
        if f not in seen:
            seen.add(f); ordered.append(f)
    return ordered[:5]


def sample_ids(layer_url, field):
    q = ("%s/query?where=1%%3D1&outFields=%s&returnGeometry=false&f=json"
         "&resultRecordCount=2500" % (layer_url, urllib.parse.quote(field, safe="")))
    try:
        d = json.loads(C.http_get(q, retries=1, timeout=30))
    except Exception:  # noqa: BLE001
        return set()
    ids = set()
    for feat in d.get("features", []):
        v = feat.get("attributes", {}).get(field)
        if v not in (None, ""):
            ids.add(norm(v))
    return ids


def discover_county(county, universe, bbox):
    """Return best {code, county, url, precinct_field, overlap, matched, universe} or None."""
    name = county["name"]
    queries = ["%s County Florida precinct" % name, "%s precinct" % name,
               "%s voting precincts" % name]
    services, seen = [], set()
    for q in queries:
        for u in arcgis_search(q):
            if u not in seen:
                seen.add(u); services.append(u)
    best = None
    for svc in services[:MAX_CANDIDATES]:
        for lurl, fields, extent in polygon_layers(svc):
            if not bbox_overlaps(extent, bbox):
                continue  # layer is not in this Florida county -> reject
            for field in candidate_fields(fields):
                ids = sample_ids(lurl, field)
                if not ids:
                    continue
                matched = len(ids & universe)
                recall = matched / len(universe) if universe else 0
                precision = matched / len(ids)          # guards against alias fields
                score = min(recall, precision)          # both must be high
                if best is None or score > best["score"]:
                    best = {"code": county["code"], "county": name, "url": lurl,
                            "precinct_field": field, "overlap": round(recall, 3),
                            "precision": round(precision, 3), "score": round(score, 3),
                            "matched": matched, "found": len(ids), "universe": len(universe)}
        if best and best["score"] >= 0.95:
            break
    return best


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    cfg = load(CONFIG_PATH)
    counties = load(COUNTIES_PATH)["counties"]
    id_cache = load(IDCACHE_PATH, {}) or {}
    sources = load(SOURCES_PATH, {"counties": []})
    have = {s["code"] for s in sources["counties"]}

    todo = [c for c in counties if (c["code"] in args) or (not args and c["code"] not in have)]
    print("Discovering %d counties (have %d already)..." % (len(todo), len(have)))

    # ground-truth rosters
    def roster(c):
        eid = id_cache.get(c["code"])
        return c["code"], (tqv_universe(cfg, c["code"], eid) if eid else set())
    universes = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for code, u in ex.map(roster, todo):
            universes[code] = u

    bboxes = county_bboxes()

    def work(c):
        u = universes.get(c["code"], set())
        if not u:
            return c["code"], None, "no TQV roster"
        b = discover_county(c, u, bboxes.get(c["name"]))
        return c["code"], b, ""

    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for code, best, note in ex.map(work, todo):
            results[code] = (best, note)

    accepted, review = [], []
    for c in todo:
        best, note = results[c["code"]]
        if best and best["score"] >= THRESHOLD:
            accepted.append(best)
            print("  OK   %-4s %-13s recall %.0f%% prec %.0f%% (%d found / %d roster) field=%s"
                  % (c["code"], c["name"], best["overlap"] * 100, best["precision"] * 100,
                     best["found"], best["universe"], best["precinct_field"]))
        else:
            review.append((c, best, note))

    print("\nNeeds review / not found (%d):" % len(review))
    for c, best, note in review:
        if best:
            print("  ??   %-4s %-13s recall %.0f%% prec %.0f%% field=%s %s"
                  % (c["code"], c["name"], best["overlap"] * 100, best["precision"] * 100,
                     best["precinct_field"], best["url"]))
        else:
            print("  --   %-4s %-13s %s" % (c["code"], c["name"], note or "no candidate"))

    if accepted and not dry:
        for b in accepted:
            sources["counties"].append({"code": b["code"], "county": b["county"],
                                        "url": b["url"], "precinct_field": b["precinct_field"]})
        sources["counties"].sort(key=lambda s: s["code"])
        with open(SOURCES_PATH, "w", encoding="utf-8") as f:
            json.dump(sources, f, indent=2)
        print("\nAdded %d counties to %s. Now run: python scripts/build_precincts.py --verify"
              % (len(accepted), os.path.relpath(SOURCES_PATH, ROOT)))
    elif dry:
        print("\n(dry run: %d would be added)" % len(accepted))


if __name__ == "__main__":
    main()
