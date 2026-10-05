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

Fallback: the UF Election Lab's Arizona files (which republish the same
tracker). Not used while the tracker works: Maricopa's own ArcGIS feature
services (services.arcgis.com/ykpntM6e3tHvzKRJ, Maricopa_County_EV_Return_
Statistics and party layers), which still held the July primary on 10/5.

Run:  python scripts/az_update.py [--force]
"""
import json
import os
import sys
import urllib.error
import urllib.request

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
        doc, etag = fetch_tracker(src["data"], None if force else prev.get("source_etag"))
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
    changed, snap = write(LATEST_PATH, body, prev, {"source_compiled_iso": C.utc_now_iso(), "source_etag": etag})
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
