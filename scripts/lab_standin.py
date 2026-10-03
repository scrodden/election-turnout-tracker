"""UF Election Lab stand-in for states whose official early-vote source isn't
wired or hasn't posted yet.

The Lab (Dr. Michael McDonald) publishes a statewide CSV and, for many states,
a county CSV:
  /data-downloads/earlyvote/2026/US.csv          one row per state
  /data-downloads/earlyvote/2026/<ST>_county.csv  one row per county (optional)
with request_/accept_/inperson_/voted_ columns (x dem/rep/none/all where the
state has party registration) plus age/gender/race counts. Used unaltered,
with attribution, under CC BY-NC-ND 4.0 (non-commercial).

A connector calls run(...) only when its official source has nothing; as soon
as the official source posts, the connector writes official data instead and
the stand-in drops out. County rows are used only if every county matches the
state's map and their sums equal the statewide row; otherwise statewide only.

    import lab_standin as LAB
    if not official_rows and LAB.run("co", cfg, GEO_PATH, LATEST_PATH, HISTORY_PATH, partisan=True):
        return 0
"""
import csv
import io
import json
import os
import re
import sys

import common as C

BASE = "https://election.lab.ufl.edu/data-downloads/earlyvote/2026/"
PAGE = "https://election.lab.ufl.edu/early-vote/2026-early-voting/"
AGE = [("18-25", "1"), ("26-40", "2"), ("41-65", "3"), ("Over 65", "4")]
RACE = [("White", "nh_white"), ("Black", "nh_black"), ("Hispanic", "hispanic"), ("Asian American", "nh_asian"),
        ("Native American", "nh_native_american"), ("Other/unknown", "nh_other")]
KINDS = ("request", "accept", "inperson")
# current county names -> the (older) names in our map files
ALIASES = {"oglalalakota": "shannon"}   # SD: Shannon County renamed Oglala Lakota in 2015


def _norm(s):
    n = re.sub(r"[^a-z0-9]", "", str(s).lower().replace("&", "and"))
    return ALIASES.get(n, n)


def _n(row, k):
    v = str((row or {}).get(k, "") or "").replace(",", "").strip()
    try:
        return int(float(v)) if v and v.upper() != "NA" else 0
    except ValueError:
        return 0


def _load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _parties(row, kind, partisan):
    a = _n(row, kind + "_all")
    if not partisan:
        return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": a}
    d, r, u = _n(row, kind + "_dem"), _n(row, kind + "_rep"), _n(row, kind + "_none")
    return {"rep": r, "dem": d, "npa": u, "oth": max(0, a - d - r - u), "total": a}


def _blk(p, partisan):
    if partisan:
        return C.party_block(p["rep"], p["dem"], p["oth"], p["npa"])
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(p["total"]),
            "rep_pct": None, "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


NO_REQUESTS = set()   # states whose Lab "requested" figure just repeats ballots received (cfg lab_no_requests)


def _entity(row, partisan, code=None):
    req, ret, inp = (_parties(row, k, partisan) for k in KINDS)
    if code in NO_REQUESTS:
        req = ret   # no real request counts: no outstanding / ballot chase
    out = {k: max(0, req[k] - ret[k]) for k in req}
    cast = {k: ret[k] + inp[k] for k in ret}
    e = {"mail_voted": _blk(ret, partisan), "mail_provided": _blk(out, partisan), "early_voted": _blk(inp, partisan),
         "cast": _blk(cast, partisan), "registered": 0, "turnout_pct": None}
    m = None if code in NO_REQUESTS else C.compute_mail(e)
    if m:
        e["mail"] = m
    return e


def _demographics(row):
    groups = {"age": [{"label": l, "count": _n(row, "voted_age_" + k)} for l, k in AGE],
              "gender": [{"label": "Female", "count": _n(row, "voted_female")},
                         {"label": "Male", "count": _n(row, "voted_male")},
                         {"label": "Unknown", "count": _n(row, "voted_gender_unknown")}],
              "race": [{"label": l, "count": _n(row, "voted_" + k)} for l, k in RACE]}
    groups = {g: v for g, v in groups.items() if sum(x["count"] for x in v)}
    return groups or None


def build(code, cfg, geo_path, partisan, role="stand-in until the official source is wired"):
    """-> snapshot dict (without generated_at) or None if the Lab has nothing."""
    st = code.upper()
    if cfg.get("lab_no_requests"):
        NO_REQUESTS.add(code)
    try:
        row = next((r for r in csv.DictReader(io.StringIO(C.http_get(BASE + "US.csv", no_cache=True)))
                    if (r.get("state_abbv") or "").upper() == st), None)
    except Exception as e:  # noqa: BLE001
        print("%s: Election Lab CSV unavailable: %s" % (st, str(e)[:100]), file=sys.stderr)
        return None
    if not row or not (_n(row, "request_all") or _n(row, "voted_all")):
        return None
    partisan = partisan and any(_n(row, k) for k in ("request_dem", "accept_dem", "request_rep", "accept_rep"))

    counties, note = {}, ""
    try:
        crow = list(csv.DictReader(io.StringIO(C.http_get(BASE + st + "_county.csv", no_cache=True, retries=1))))
    except Exception:  # noqa: BLE001 - most states have no county file (404)
        crow = []
    if crow:
        geo = _load(geo_path, {"features": []})
        gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}
        matched = {r.get("county"): gidx.get(_norm(r.get("county"))) for r in crow}
        sums_ok = all(sum(_n(r, k + "_all") for r in crow) == _n(row, k + "_all") for k in KINDS)
        if all(matched.values()) and sums_ok:
            for r in crow:
                g = matched[r.get("county")]
                counties[g["name"]] = dict(_entity(r, partisan, code), fips=g["fips"])
        else:
            bad = [c for c, g in matched.items() if not g]
            note = "county file not used (%s)" % ("unmatched: %s" % bad[:5] if bad else "doesn't sum to statewide")
            print("%s: Lab %s" % (st, note), file=sys.stderr)

    statewide = _entity(row, partisan, code)
    body = {
        "state": code, "state_name": cfg.get("state_name", st), "election": cfg.get("election", {}),
        "partisan": partisan, "statewide_only": not counties, "lab_standin": True,
        "methods_present": [k for k in ("mail_voted", "early_voted", "mail_provided") if statewide[k]["total"]],
        "method_labels": cfg.get("method_labels", {}), "statewide": statewide, "counties": counties,
    }
    demo = _demographics(row)
    if demo:
        body["demographics"] = demo
    if cfg.get("mail_base_label"):   # e.g. "Ballots requested" where the Lab's count is applications
        body["mail_base_label"] = cfg["mail_base_label"]
    for k in ("unit_label", "unit_label_plural"):   # e.g. RI maps cities/towns, not counties
        if cfg.get(k):
            body[k] = cfg[k]
    snap = dict(body)
    snap["source"] = {"primary": "UF Election Lab early-vote tracker (M. McDonald), %s; CC BY-NC-ND 4.0 -- %s"
                                 % (row.get("data_source") or "state election office", role),
                      "url": PAGE, "as_of": row.get("last_update", ""), "county_detail": bool(counties), "note": note}
    snap["source_compiled"] = row.get("last_update", "")
    snap["source_compiled_iso"] = C.utc_now_iso()
    snap["data_hash"] = C.data_hash(body)
    return snap


def unassigned_note(gap, total, unit="county or district"):
    if not gap:
        return None
    return ("%s of the %s mail-ballot requests in the Election Lab's data have no %s, so request figures on the "
            "map add up to %s." % (format(gap, ","), format(total, ","), unit, format(total - gap, ",")))


def reconcile(parts, row, code=None):
    """Lab district rows vs its statewide row. Ballots cast without a district
    may be up to 10% of the total (DE's senate file leaves ~8% as "NA"); mail
    requests without one up to 1% (not checked where the Lab has no real
    request counts). -> (ok, unassigned requests, unassigned ballots cast)."""
    cast = _n(row, "accept_all") + _n(row, "inperson_all")
    cast_gap = cast - sum(_n(r, "accept_all") + _n(r, "inperson_all") for r in parts)
    req_gap = _n(row, "request_all") - sum(_n(r, "request_all") for r in parts)
    ok_cast = 0 <= cast_gap <= max(10, cast // 10)
    ok_req = code in NO_REQUESTS or 0 <= req_gap <= max(50, _n(row, "request_all") // 100)
    return ok_cast and ok_req, req_gap, cast_gap


KIND_LABELS = {   # Lab file suffix / column -> (unit label, plural, description)
    "cd": ("District", "Districts", "U.S. House district"),
    "sdl": ("House District", "House Districts", "state house district"),
    "sdu": ("Senate District", "Senate Districts", "state senate district"),
}


def districts(code, cfg, cd_geo_path, out_path, partisan, force=False, kind="cd"):
    """District view for the site's map toggle from the Lab's <ST>_<kind>.csv
    (kind cd = U.S. House, sdl / sdu = state house / senate), whose rows are
    district x county pieces, summed per district; rows without a district
    ("NA") are left out and noted. Checked about hourly; the previous file is
    kept if the Lab file is missing or doesn't reconcile with the statewide row.
    Assets: scripts/build_cd_geo.py (<st>-cd) / build_sld_geo.py (<st>-sh, -ss)."""
    st = code.upper()
    if cfg.get("lab_no_requests"):
        NO_REQUESTS.add(code)
    data_dir = os.path.dirname(out_path)
    label, plural, desc = KIND_LABELS[kind]
    if not force and C.checked_recently(data_dir, "lab_" + kind):
        return
    try:
        row = next((r for r in csv.DictReader(io.StringIO(C.http_get(BASE + "US.csv", no_cache=True)))
                    if (r.get("state_abbv") or "").upper() == st), None)
        parts = list(csv.DictReader(io.StringIO(C.http_get(BASE + st + "_" + kind + ".csv", no_cache=True, retries=1))))
    except Exception as e:  # noqa: BLE001
        print("%s: Lab district file unavailable: %s" % (st, str(e)[:100]), file=sys.stderr)
        return
    if not row or not parts:
        return
    partisan = partisan and any(_n(row, k) for k in ("request_dem", "accept_dem", "request_rep", "accept_rep"))
    geo = _load(cd_geo_path, {"features": []})
    gidx = {f["properties"]["district_number"]: f["properties"] for f in geo["features"]}
    placed = [r for r in parts if str(r.get(kind) or "").strip().isdigit()]
    ok, gap, cast_gap = reconcile(placed, row, code)
    sums = {}
    unknown = sorted({int(r[kind]) for r in placed if int(r[kind]) not in gidx})
    for r in placed:
        acc = sums.setdefault(int(r[kind]), {})
        for k, v in r.items():
            if k not in ("cd", "sdl", "sdu", "county", "return_rate"):
                acc[k] = acc.get(k, 0) + _n(r, k)
    if not ok or unknown or not gidx:
        print("%s: Lab district file doesn't reconcile (districts not on the map: %s) — keeping the previous one."
              % (st, unknown[:5]), file=sys.stderr)
        return
    out_units = {}
    for num in sorted(gidx):
        a = sums.get(num, {})
        ent = dict(_entity(a, partisan, code), fips=gidx[num]["fips"])
        d = _demographics(a)
        if d:
            ent["demographics"] = d
        out_units[gidx[num]["name"]] = ent
    statewide = _entity(row, partisan, code)
    body = {
        "state": code, "election": cfg.get("election", {}), "unit_label": label, "unit_label_plural": plural,
        "partisan": partisan,
        "source": "UF Election Lab early-vote tracker (M. McDonald), %s file (%s_%s.csv; source: %s); "
                  "CC BY-NC-ND 4.0" % (desc, st, kind, row.get("data_source") or "state election office"),
        "methods_present": [k for k in ("mail_voted", "early_voted", "mail_provided") if statewide[k]["total"]],
        "method_labels": cfg.get("method_labels", {}),
        "coverage": {"ok": len(gidx), "total": len(gidx), "unmatched": []},
        "as_of": row.get("last_update", ""), "statewide": statewide, "counties": out_units,
    }
    note = None if code in NO_REQUESTS else unassigned_note(gap, _n(row, "request_all"), "district")
    if cast_gap:
        cast_all = _n(row, "accept_all") + _n(row, "inperson_all")
        note = ((note + " ") if note else "") + (
            "%s of the %s ballots cast in the Election Lab's data (%s%%) have no %s and appear only in the statewide "
            "total." % (format(cast_gap, ","), format(cast_all, ","), round(100.0 * cast_gap / cast_all, 1), desc))
    if note:
        body["map_note"] = note
    prev = _load(out_path, {}) or {}
    h = C.data_hash(body)
    if h == prev.get("data_hash") and not force:
        print("%s districts: NOCHANGE (Lab as of %s)" % (code, body["as_of"]))
        return
    now = C.utc_now_iso()
    out = dict(body, source_compiled=body["as_of"], source_compiled_iso=now, data_hash=h, generated_at=now)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    print("%s districts: CHANGED %d districts, cast=%d (Lab as of %s)"
          % (code, len(out_units), statewide["cast"]["total"], body["as_of"]))


def due(prev, force=False, data_dir=None):
    """The Lab updates about once a day (mornings ET) and the turnout workflow
    runs every 10 minutes, so: skip once we hold today's update, otherwise
    check roughly hourly (C.checked_recently, keyed "lab")."""
    from datetime import datetime, timedelta, timezone
    if force:
        return True
    now = datetime.now(timezone.utc)
    et = now - timedelta(hours=4)
    today = "%d/%d/%d" % (et.month, et.day, et.year)
    if prev.get("lab_standin") and (prev.get("source") or {}).get("as_of") == today:
        return False
    if not prev.get("lab_standin"):
        return True
    if data_dir:
        return not C.checked_recently(data_dir, "lab")
    return now.minute < 12


def run(code, cfg, geo_path, latest_path, history_path, partisan, force=False, role=None):
    """Build + write the stand-in. Returns True if the state has Lab data
    (including when an existing stand-in is kept because no check is due)."""
    prev0 = _load(latest_path, {}) or {}
    if not due(prev0, force, os.path.dirname(latest_path)):
        if prev0.get("lab_standin"):
            print("%s: holding the Election Lab's %s update (stand-in) — next check not due."
                  % (code, (prev0.get("source") or {}).get("as_of")))
        return bool(prev0.get("lab_standin"))
    snap = build(code, cfg, geo_path, partisan, **({"role": role} if role else {}))
    if not snap:
        return bool(prev0.get("lab_standin"))   # Lab unreachable: keep the last stand-in
    prev = prev0
    changed = force or snap["data_hash"] != prev.get("data_hash")
    now = C.utc_now_iso()
    snap["generated_at"] = now if changed else prev.get("generated_at", now)
    if not changed:
        print("NOCHANGE  (Election Lab stand-in, as of %s)" % snap["source"]["as_of"])
        return True
    os.makedirs(os.path.dirname(latest_path), exist_ok=True)
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    c = snap["statewide"]["cast"]
    if c["total"]:
        with open(history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"generated_at": now,
                                "statewide": {"cast": [c["rep"], c["dem"], c["oth"], c["npa"], c["total"]]}},
                               separators=(",", ":")) + "\n")
    print("CHANGED  Election Lab stand-in as of %s: %d counties | cast=%d | requested=%s%s"
          % (snap["source"]["as_of"], len(snap["counties"]), c["total"],
             (snap["statewide"].get("mail") or {}).get("requested"),
             (" | R%d D%d NPA%d margin=%s" % (c["rep"], c["dem"], c["npa"], c["margin"])) if snap["partisan"] else ""))
    return True
