#!/usr/bin/env python3
"""Arizona early ballots (ABEV) by county, congressional and legislative
district, and registered party.

Source: Stealth Analytics' "2026 Arizona ABEV Tracker" (stealth-analytics.com/
early-ballots; "free to cite and reuse with attribution"), compiled every two
hours from the counties' own early-ballot files and reconciled against Arizona
Secretary of State registration. Its data file (config source.stealth.data)
has, per county / congressional district / legislative district: registered
voters, early ballots issued, and a daily cumulative series of ballots
returned, each split R / D / Other (Other = independents and minor parties ->
shown as NPA). Only counties that have started reporting appear; the map note
names the rest. Fetched with If-None-Match, so unchanged checks cost a 304.

mail_voted = returned (newest day in the series); mail_provided = issued -
returned (outstanding) -> ballot chase; registered -> turnout %.
Writes data/az/latest.json (counties), districts_cd.json and districts_ld.json
(Arizona's 30 legislative districts elect both chambers).

Maricopa: the county's own ArcGIS feature services (config source.maricopa;
early-ballot requests and signature-verified returns for the active election,
all voters + R + D layers, rest -> NPA) are checked every run once they carry
the general. Returned-ballot counts only grow, so whichever source reports
more returned ballots for Maricopa is the more current and is used (ties go to
the more recent data date, then the county's own feed); the map note says
which. Registration (turnout %) stays the tracker's; district views stay the
tracker's.

Fallback: the UF Election Lab's Arizona files (which republish the tracker).

Run:  python scripts/az_update.py [--force]
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "az"
CONFIG_PATH = os.path.join(ROOT, "config", "az.json")
GEO_PATH = os.path.join(ROOT, "assets", "az-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
PARTY = {"R": "rep", "D": "dem", "Other": "npa"}
VIEWS = {"cd": ("az-cd", "CD", "Congressional District", "Congressional Districts"),
         "ld": ("az-ld", "LD", "Legislative District", "Legislative Districts")}


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def fetch_tracker(url, etag):
    """-> (data or None if unchanged, etag)."""
    headers = {"User-Agent": C.USER_AGENT, "Accept": "application/json"}
    if etag:
        headers["If-None-Match"] = etag
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120,
                                    context=C._SSL_CTX) as r:
            return json.loads(r.read().decode("utf-8")), r.headers.get("ETag", "")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag
        raise


def _arcgis_sums_by_election(base, service):
    """Sum Requests / Returns of an ArcGIS feature layer, grouped by
    ElectionDescription -> {description: (requests, returns)}."""
    stats = json.dumps([{"statisticType": "sum", "onStatisticField": f, "outStatisticFieldName": f.lower()}
                        for f in ("Requests", "Returns")])
    url = ("%s%s/FeatureServer/0/query?where=1%%3D1&groupByFieldsForStatistics=ElectionDescription"
           "&outStatistics=%s&f=json" % (base, urllib.parse.quote(service), urllib.parse.quote(stats)))
    j = json.loads(C.http_get(url, no_cache=True))
    if "error" in j:
        raise RuntimeError("ArcGIS: %s" % j["error"].get("message"))
    out = {}
    for ft in j.get("features", []):
        a = ft["attributes"]
        if a.get("ElectionDescription"):
            out[a["ElectionDescription"].strip()] = (int(a.get("requests") or 0), int(a.get("returns") or 0))
    return out


def maricopa_official(mc):
    """Maricopa County Elections GIS feature services -> {"ret": parties,
    "req": parties, "total_ret": n, "as_of": "YYYY-MM-DD HH:MM UTC"} or None
    while the layers still hold another election (e.g. the July primary)."""
    if not mc:
        return None
    base, layers = mc["arcgis_base"], mc["layers"]
    tokens = [t.upper() for t in mc.get("election_tokens", [])]
    sums = {}
    for key in ("all", "rep", "dem"):
        hit = [v for d, v in _arcgis_sums_by_election(base, layers[key]).items() if all(t in d.upper() for t in tokens)]
        if not hit:
            return None
        sums[key] = hit[0]
    (areq, aret), (rreq, rret), (dreq, dret) = sums["all"], sums["rep"], sums["dem"]
    info = json.loads(C.http_get("%s%s/FeatureServer/0?f=json" % (base, urllib.parse.quote(layers["all"])), no_cache=True))
    edit = (info.get("editingInfo") or {}).get("lastEditDate")
    as_of = datetime.fromtimestamp(edit / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if edit else ""
    return {"ret": {"rep": rret, "dem": dret, "oth": 0, "npa": max(0, aret - rret - dret)},
            "req": {"rep": rreq, "dem": dreq, "oth": 0, "npa": max(0, areq - rreq - dreq)},
            "total_ret": aret, "as_of": as_of}


def maricopa_entity(mc, tracker, page):
    """Our county entity from the county's own feed; registration from the tracker."""
    reg = tracker["registered"] if tracker else 0

    def pb(d):
        return C.party_block(d["rep"], d["dem"], d["oth"], d["npa"])
    e = {"mail_voted": pb(mc["ret"]), "cast": pb(mc["ret"]),
         "mail_provided": pb({p: max(0, mc["req"][p] - mc["ret"][p]) for p in mc["ret"]}),
         "registered": reg, "turnout_pct": C.pct(mc["total_ret"], reg) if reg else None,
         "last_updated": mc["as_of"][:10], "source": "county-feed", "source_url": page,
         "source_label": "Maricopa County Elections", "fips": "04013"}
    m = C.compute_mail(e)
    if m:
        e["mail"] = m
    return e


def _parties(d):
    out = {"rep": 0, "dem": 0, "oth": 0, "npa": 0}
    for k, v in (d or {}).items():
        out[PARTY.get(k, "npa")] += int(v or 0)
    return out


def unit(rec):
    """Tracker record {registered, issued, series{date: {R, D, Other}}, lastUpdated} -> our entity."""
    series = rec.get("series") or {}
    ret = _parties(series[max(series)]) if series else _parties({})
    iss = _parties(rec.get("issued"))
    reg = sum(_parties(rec.get("registered")).values())

    def pb(d):
        return C.party_block(d["rep"], d["dem"], d["oth"], d["npa"])
    e = {"mail_voted": pb(ret), "mail_provided": pb({k: max(0, iss[k] - ret[k]) for k in ret}), "cast": pb(ret),
         "registered": reg, "turnout_pct": C.pct(sum(ret.values()), reg) if reg else None,
         "last_updated": rec.get("lastUpdated", "")}
    m = C.compute_mail(e)
    if m:
        e["mail"] = m
    return e


def total(units):
    keys = ("mail_voted", "mail_provided", "cast")
    sw = {k: C.add_blocks(*[u[k] for u in units]) for k in keys} if units else {k: C.party_block(0, 0, 0, 0) for k in keys}
    sw["registered"] = sum(u["registered"] for u in units)
    sw["turnout_pct"] = C.pct(sw["cast"]["total"], sw["registered"]) if sw["registered"] else None
    m = C.compute_mail(sw)
    if m:
        sw["mail"] = m
    return sw


def write(path, body, prev, extra):
    h = C.data_hash(body)
    changed = h != (prev or {}).get("data_hash")
    now = C.utc_now_iso()
    out = dict(body, data_hash=h, generated_at=now if changed else (prev or {}).get("generated_at", now), **extra)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    return changed, out


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg.get("source", {}).get("stealth", {})
    prev = load(LATEST_PATH, {}) or {}
    os.makedirs(DATA_DIR, exist_ok=True)
    if not force and prev.get("counties") and C.checked_recently(DATA_DIR, "stealth", minutes=20):
        print("az: checked under 20 minutes ago.")
        return 0
    try:
        mc = maricopa_official(cfg.get("source", {}).get("maricopa"))
    except Exception as e:  # noqa: BLE001 - the county feed is optional
        print("AZ: Maricopa feed unavailable: %s" % str(e)[:120], file=sys.stderr)
        mc = None
    prev_mc = prev.get("maricopa_official") or {}
    mc_changed = bool(mc) and (mc["total_ret"], mc["as_of"]) != (prev_mc.get("total_ret"), prev_mc.get("as_of"))
    try:   # re-fetch the tracker in full when Maricopa's own numbers moved, so the comparison is redone
        doc, etag = fetch_tracker(src["data"], None if (force or mc_changed) else prev.get("source_etag"))
    except Exception as e:  # noqa: BLE001
        print("AZ tracker unavailable: %s" % str(e)[:140], file=sys.stderr)
        import lab_standin as LAB
        LAB.run(STATE, cfg, GEO_PATH, LATEST_PATH, HISTORY_PATH, partisan=True, force=force,
                role="stand-in while the Stealth Analytics tracker is unreachable")
        return 0
    if doc is None:
        print("NOCHANGE  (tracker file unchanged)")
        return 0

    geo = load(GEO_PATH, {"features": []})
    gidx = {f["properties"]["fips"]: f["properties"] for f in geo["features"]}
    series = doc.get("SERIES", {})
    counties = {}
    for fips, rec in (series.get("counties") or {}).items():
        if fips in gidx:
            counties[gidx[fips]["name"]] = dict(unit(rec), fips=fips)
    if not counties:
        print("AZ: tracker has no reporting counties — keeping the previous snapshot.", file=sys.stderr)
        return 0
    mc_note = ""
    if mc:
        tr = counties.get("Maricopa")
        tr_ret = tr["cast"]["total"] if tr else -1
        tr_date = (tr or {}).get("last_updated", "")
        if mc["total_ret"] > tr_ret or (mc["total_ret"] == tr_ret and mc["as_of"][:10] >= tr_date):
            counties["Maricopa"] = maricopa_entity(mc, tr, cfg["source"]["maricopa"].get("page"))
            mc_note = (" Maricopa uses the county's own feed (%s returned, as of %s), which is ahead of or level with "
                       "the tracker (%s)." % (format(mc["total_ret"], ","), mc["as_of"], format(max(tr_ret, 0), ",")))
        else:
            mc_note = (" Maricopa uses the tracker (%s returned), which is ahead of the county's own feed (%s, as of %s)."
                       % (format(tr_ret, ","), format(mc["total_ret"], ","), mc["as_of"]))
    statewide = total(list(counties.values()))
    waiting = sorted(g["name"] for f, g in gidx.items() if g["name"] not in counties)
    as_of = max(c["last_updated"] for c in counties.values())
    stale = sorted("%s %s" % (n, c["last_updated"][5:].replace("-", "/")) for n, c in counties.items()
                   if c["last_updated"] and c["last_updated"] < as_of)
    note = "%d of %d counties reporting so far" % (len(counties), len(gidx))
    if waiting:
        note += "; not yet reporting (gray, not zero): " + ", ".join(waiting)
    note += ". Statewide totals cover the reporting counties."
    if stale:
        note += " Some counties last reported earlier: " + ", ".join(stale) + "."
    note += mc_note
    source = ("Stealth Analytics, 2026 Arizona ABEV Tracker (stealth-analytics.com/early-ballots), compiled from the "
              "counties' early ballot files; as of %s" % as_of)
    body = {
        "state": STATE, "state_name": cfg.get("state_name", "Arizona"), "election": cfg.get("election", {}),
        "partisan": True, "methods_present": [k for k in ("mail_voted", "mail_provided") if statewide[k]["total"]],
        "method_labels": cfg.get("method_labels", {}), "mail_base_label": cfg.get("mail_base_label", "Ballots issued"),
        "statewide": statewide, "counties": counties, "map_note": note,
        "source": {"primary": source, "url": src.get("page"), "as_of": as_of},
        "source_compiled": as_of,
    }
    changed, snap = write(LATEST_PATH, body, prev, {"source_compiled_iso": C.utc_now_iso(), "source_etag": etag,
                                                     "maricopa_official": mc or {}})
    c = statewide["cast"]
    if changed and c["total"]:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"generated_at": snap["generated_at"], "statewide": {
                "cast": [c["rep"], c["dem"], c["oth"], c["npa"], c["total"]]}}, separators=(",", ":")) + "\n")

    for kind, (geo_name, prefix, label, plural) in VIEWS.items():
        dgeo = load(os.path.join(ROOT, "assets", geo_name + ".geojson"), {"features": []})
        didx = {f["properties"]["district_number"]: f["properties"] for f in dgeo["features"]}
        units = {}
        for k, rec in (series.get(kind) or {}).items():
            if str(k).isdigit() and int(k) in didx:
                units[didx[int(k)]["name"]] = dict(unit(rec), fips=didx[int(k)]["fips"])
        if not units:
            continue
        dsw = total(list(units.values()))
        dbody = {"state": STATE, "election": cfg.get("election", {}), "unit_label": label, "unit_label_plural": plural,
                 "partisan": True, "source": source,
                 "methods_present": body["methods_present"], "method_labels": body["method_labels"],
                 "mail_base_label": body["mail_base_label"],
                 "coverage": {"ok": len(didx), "total": len(didx), "unmatched": []},
                 "as_of": as_of, "statewide": dsw, "counties": units,
                 "map_note": "District totals include only the %d counties reporting so far." % len(counties)}
        path = os.path.join(DATA_DIR, "districts_%s.json" % kind)
        write(path, dbody, load(path, {}), {"source_compiled": as_of, "source_compiled_iso": C.utc_now_iso()})
    print("%s  AZ tracker as of %s: %d counties | returned=%d of %d issued (R%d D%d Other%d) margin=%s | turnout %s%%"
          % ("CHANGED" if changed else "NOCHANGE", as_of, len(counties), c["total"],
             (statewide.get("mail") or {}).get("requested", 0), c["rep"], c["dem"], c["npa"], c["margin"],
             statewide["turnout_pct"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
