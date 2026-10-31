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
import unicodedata

import common as C

BASE = "https://election.lab.ufl.edu/data-downloads/earlyvote/2026/"
PAGE = "https://election.lab.ufl.edu/early-vote/2026-early-voting/"
AGE = [("18-25", "1"), ("26-40", "2"), ("41-65", "3"), ("Over 65", "4")]
RACE = [("White", "nh_white"), ("Black", "nh_black"), ("Hispanic", "hispanic"), ("Asian American", "nh_asian"),
        ("Native American", "nh_native_american"), ("Other/unknown", "nh_other")]
KINDS = ("request", "accept", "inperson")
# current county names -> the (older) names in our map files
ALIASES = {"oglalalakota": "shannon",   # SD: Shannon County renamed Oglala Lakota in 2015
           "stlouiscounty": "stlouis",   # MO: the map has "St. Louis" (the county) and "St. Louis City"
           "jeffdavis": "jeffersondavis"}   # MS: the Lab's "Jeff Davis" (GA's and TX's real Jeff Davis still match: both sides are normalized)


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()   # Doña Ana == Dona Ana
    n = re.sub(r"[^a-z0-9]", "", s.lower().replace("&", "and"))
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
    # a file can split ballots returned by party but not ballots issued (CA's district files: request_all only):
    # the outstanding ballots' party is then unknown -- not "other", and no per-party return rates
    req_blind = partisan and code not in NO_REQUESTS and not any(k in row for k in ("request_dem", "request_rep", "request_none"))
    out = {k: max(0, req[k] - ret[k]) for k in req}
    cast = {k: ret[k] + inp[k] for k in ret}
    e = {"mail_voted": _blk(ret, partisan),
         "mail_provided": C.party_block(0, 0, 0, 0, unk=out["total"]) if req_blind else _blk(out, partisan),
         "early_voted": _blk(inp, partisan), "cast": _blk(cast, partisan), "registered": 0, "turnout_pct": None}
    m = None if code in NO_REQUESTS else C.compute_mail(e)
    if m and req_blind:
        m.pop("parties", None)
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


def _county_units(crow, rules):
    """The Lab's county rows -> one row per map unit, for a turnout-only state whose file needs it (cfg
    lab_county_rows). MO's rows are its 116 election authorities: "Greene County", plus Kansas City's board and
    "Rest of Jackson County", which are both Jackson on the map. A row of all zeros hasn't reported (AP's county feed
    fills in as clerks report), so it's left out: gray on the map, not a county with no ballots.
    -> (rows, sentences for the map note)."""
    groups = {}
    for r in crow:
        raw = r.get("county") or ""
        groups.setdefault(rules.get("merge", {}).get(raw) or re.sub(r"\s+County$", "", raw), []).append(r)
    rows, notes = [], []
    for name, parts in groups.items():
        live = [r for r in parts if any(_n(r, k + "_all") for k in KINDS)]
        if not live:
            continue
        rows.append(dict({k + "_all": sum(_n(r, k + "_all") for r in live) for k in KINDS}, county=name))
        if len(live) < len(parts):
            notes.append("%s shows %s only so far; %s hasn't reported."
                         % (name, " and ".join(r["county"] for r in live),
                            " and ".join(r["county"] for r in parts if r not in live)))
    gaps = [(sum(1 for r in rows if r["inperson_all"] and not r["accept_all"]), "in-person votes but no mail ballots"),
            (sum(1 for r in rows if r["accept_all"] and not r["inperson_all"]), "mail ballots but no in-person votes"),
            (sum(1 for r in rows if not r["request_all"]), "no mail-ballot requests, so none read as outstanding")]
    gaps = ["%d %s %s" % (n, "shows" if n == 1 else "show", what) for n, what in gaps if n]
    if gaps:
        notes.append("Some counties are incomplete: %s." % "; ".join(gaps))
    return rows, notes


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

    counties, note, partial_note = {}, "", ""
    try:
        crow = list(csv.DictReader(io.StringIO(C.http_get(BASE + st + "_county.csv", no_cache=True, retries=1))))
    except Exception:  # noqa: BLE001 - most states have no county file (404)
        crow = []
    rules, unit_notes = cfg.get("lab_county_rows"), []
    if crow and rules:
        crow, unit_notes = _county_units(crow, rules)
    if crow:
        geo = _load(geo_path, {"features": []})
        gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}
        matched = {r.get("county"): gidx.get(_norm(r.get("county"))) for r in crow}
        unit_mail_only = bool(cfg.get("lab_county_mail_only"))   # CT: the town file has mail ballots only
        sums_ok = all(sum(_n(r, k + "_all") for r in crow) == _n(row, k + "_all") for k in KINDS
                      if not (unit_mail_only and k == "inperson"))
        if all(matched.values()) and sums_ok:
            for r in crow:
                g = matched[r.get("county")]
                counties[g["name"]] = dict(_entity(r, partisan, code), fips=g["fips"])
                if rules and _n(r, "accept_all") and not _n(r, "request_all"):   # the Lab's "Inf %": no return rate
                    counties[g["name"]].pop("mail", None)
            # e.g. MO 10/23: St. Louis County only -- say so, the totals aren't statewide
            if len(counties) < (len(gidx) if rules else 0.9 * len(gidx)):
                names = sorted(r.get("county") for r in crow)   # the Lab's names: "St. Louis County", not the map's "St. Louis"
                listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if len(names) < 8 \
                    else "%d of the state's %d counties%s" % (len(names), len(gidx), (rules or {}).get("units_note", ""))
                partial_note = ("Only %s %s so far in the Election Lab's %s data (source: %s), so every figure here covers "
                                "%s, not the whole state; the rest of the map is gray until more counties report."
                                % (listed, "reports" if len(names) == 1 else "report", cfg.get("state_name", st),
                                   re.sub(r"\s+-\s+see .*$", "", row.get("data_source") or "state election office"),
                                   "that county" if len(names) == 1 else "those counties"))
            partial_note = " ".join(x for x in [partial_note] + unit_notes if x)
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
    if counties and cfg.get("lab_county_mail_only") and _n(row, "inperson_all"):
        partial_note = ((partial_note + " ") if partial_note else "") + (
            "%s show mail ballots only: the Election Lab's file for them doesn't include the %s in-person early votes, "
            "which are reported statewide only." % (cfg.get("unit_label_plural") or "Counties",
                                                   format(_n(row, "inperson_all"), ",")))
    if partial_note:
        body["map_note"] = partial_note
    if rules and counties:
        body["units_fill_in"] = True   # a unit that's missing hasn't reported (app.js): not "0 ballots"
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


def reconcile(parts, row, code=None, mail_only=False):
    """Lab district rows vs its statewide row. Ballots cast without a district
    may be up to 10% of the total (DE's senate file leaves ~8% as "NA"); mail
    requests without one up to 1% (not checked where the Lab has no real
    request counts). mail_only: the district file has mail ballots only (ID), so
    only those are compared. -> (ok, unassigned requests, unassigned ballots cast)."""
    def cast_of(r):
        return _n(r, "accept_all") + (0 if mail_only else _n(r, "inperson_all"))
    cast = cast_of(row)
    cast_gap = cast - sum(cast_of(r) for r in parts)
    req_gap = _n(row, "request_all") - sum(_n(r, "request_all") for r in parts)
    ok_cast = 0 <= cast_gap <= max(10, cast // 10)
    ok_req = code in NO_REQUESTS or 0 <= req_gap <= max(50, _n(row, "request_all") // 100)
    return ok_cast and ok_req, req_gap, cast_gap


KIND_LABELS = {   # Lab file suffix / column -> (unit label, plural, description)
    "cd": ("District", "Districts", "U.S. House district"),
    "sdl": ("House District", "House Districts", "state house district"),
    "sdu": ("Senate District", "Senate Districts", "state senate district"),
}


def _dnum(v):
    """District number from a Lab district cell: '7', '07', or a label like
    'United States Representative District 1' / 'State Senator District 02'
    (Iowa). None for 'NA' / blank."""
    v = str(v or "").strip()
    if not v or v.upper().startswith("NA"):
        return None
    if v.isdigit():
        return int(v)
    m = re.search(r"(?:District|Congressional)\s+0*(\d+)\s*$", v, re.I)   # ... "Congressional 3" (CO)
    return int(m.group(1)) if m else None


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
    dcode = code
    if cfg.get("lab_district_no_requests"):   # e.g. UT: the district file's "requests" just repeat ballots accepted
        dcode = code + ":districts"
        NO_REQUESTS.add(dcode)
    data_dir = os.path.dirname(out_path)
    label, plural, desc = (cfg.get("lab_district_labels") or {}).get(kind) or KIND_LABELS[kind]   # e.g. CA's Assembly
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
    wanted = partisan
    partisan = partisan and any(_n(row, k) for k in ("request_dem", "accept_dem", "request_rep", "accept_rep"))
    geo = _load(cd_geo_path, {"features": []})
    gidx = {f["properties"]["district_number"]: f["properties"] for f in geo["features"]}
    placed = [r for r in parts if _dnum(r.get(kind)) is not None]
    mail_only = bool(cfg.get("lab_district_mail_only"))   # ID: the district files have mail ballots only
    ok, gap, cast_gap = reconcile(placed, row, dcode, mail_only)
    sums = {}
    unknown = sorted({_dnum(r[kind]) for r in placed if _dnum(r[kind]) not in gidx})
    for r in placed:
        acc = sums.setdefault(_dnum(r[kind]), {})
        for k, v in r.items():
            if k not in ("cd", "sdl", "sdu", "county", "return_rate"):
                acc[k] = acc.get(k, 0) + _n(r, k)
    # an older snapshot than the statewide row: every mail request is there but fewer ballots, behind by a similar
    # share in every county (NV 10/22: 227,226 of 269,277). Used, with its own totals and a note, rather than kept
    # frozen at an old file (NV's had stayed at 10/19 for three days).
    req_ok = dcode in NO_REQUESTS or 0 <= gap <= max(50, _n(row, "request_all") // 100)
    # ... and when its mail requests trail too (NV 10/26: 94.5% of the requests, 92.7% of the ballots; the file had
    # been kept at 10/22 for four days): a file behind by up to a quarter is used, with its own totals and a note
    behind = 0 <= gap <= _n(row, "request_all") // 4 and cast_gap <= (_n(row, "accept_all") + _n(row, "inperson_all")) // 4
    lag = not ok and cast_gap > 0 and not unknown and (req_ok or behind)
    if (not ok and not lag) or unknown or not gidx:
        print("%s: Lab district file doesn't reconcile (districts not on the map: %s) — keeping the previous one."
              % (st, unknown[:5]), file=sys.stderr)
        return
    out_units = {}
    for num in sorted(gidx):
        a = sums.get(num, {})
        ent = dict(_entity(a, partisan, dcode), fips=gidx[num]["fips"])
        d = _demographics(a)
        if d:
            ent["demographics"] = d
        out_units[gidx[num]["name"]] = ent
    if lag or mail_only:   # the district file's own totals, so the cards match the map
        tot = {}
        for a in sums.values():
            for k, v in a.items():
                tot[k] = tot.get(k, 0) + v
        statewide = _entity(tot, partisan, dcode)
    else:
        statewide = _entity(row, partisan, dcode)
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
    note = None if dcode in NO_REQUESTS else unassigned_note(gap, _n(row, "request_all"), "district")
    if lag and not req_ok:   # the whole file is an older snapshot: requests and ballots both behind
        cast_all = _n(row, "accept_all") + _n(row, "inperson_all")
        req_all = _n(row, "request_all")
        note = ("The Election Lab's %s file is behind its statewide count: it has %s of the %s ballots (%s%%) and %s of "
                "the %s mail-ballot requests (%s%%), so these figures trail the statewide totals until the Lab updates "
                "the file." % (desc, format(cast_all - cast_gap, ","), format(cast_all, ","),
                               round(100.0 * (cast_all - cast_gap) / cast_all, 1), format(req_all - gap, ","),
                               format(req_all, ","), round(100.0 * (req_all - gap) / req_all, 1)))
    elif lag:
        cast_all = _n(row, "accept_all") + _n(row, "inperson_all")
        na_cast = sum(_n(r, "accept_all") + _n(r, "inperson_all") for r in parts if _dnum(r.get(kind)) is None)
        note = ((note + " ") if note else "") + (
            "The Election Lab's %s file has %s of the %s ballots in its statewide count (%s%%)%s. These figures "
            "trail the statewide totals until the Lab updates the file."
            % (desc, format(cast_all - cast_gap, ","), format(cast_all, ","), round(100.0 * (cast_all - cast_gap) / cast_all, 1),
               "; %s have no district" % format(na_cast, ",") if na_cast else
               "; every mail request is in it, so it's an older snapshot of the ballots"))
    elif cast_gap:
        cast_all = _n(row, "accept_all") + _n(row, "inperson_all")
        note = ((note + " ") if note else "") + (
            "%s of the %s ballots cast in the Election Lab's data (%s%%) have no %s and appear only in the statewide "
            "total." % (format(cast_gap, ","), format(cast_all, ","), round(100.0 * cast_gap / cast_all, 1), desc))
    if mail_only and _n(row, "inperson_all"):
        note = ((note + " ") if note else "") + (
            "District figures are mail ballots only: the Election Lab's %s file doesn't include the %s in-person early "
            "votes, so these totals run below the statewide ones."
            % (desc, format(_n(row, "inperson_all"), ",")))
    if wanted and not partisan:   # a party state whose Lab district file has no party (OH): say where party is
        body["no_party_text"] = ("The Election Lab's %s file has no party breakdown, so %s show turnout; party is on "
                                 "the county map." % (desc, desc + "s"))
    if note:
        body["map_note"] = note
    prev = _load(out_path, {}) or {}
    bad = suspicious_drop(prev, statewide["cast"]["total"])
    if bad:
        print("%s districts: %s" % (code, bad), file=sys.stderr)
        return
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
    runs every 10 minutes, so: check roughly hourly (C.checked_recently, keyed
    "lab"), and every ~3 hours once we hold today's update -- not never: the
    Lab sometimes re-posts the same day (NV 10/15: a half-written morning file
    with 507 ballots, corrected to 14,589 within the hour)."""
    from datetime import datetime, timedelta, timezone
    if force:
        return True
    now = datetime.now(timezone.utc)
    et = now - timedelta(hours=4)
    today = "%d/%d/%d" % (et.month, et.day, et.year)
    if not prev.get("lab_standin"):
        return True
    have_today = (prev.get("source") or {}).get("as_of") == today
    if data_dir:
        return not C.checked_recently(data_dir, "lab", minutes=180 if have_today else 50)
    return now.minute < 12 and not have_today


def suspicious_drop(prev, new_cast, now=None):
    """Ballots cast only grow (bar small corrections). A fall of more than 20%
    from what we last published is treated as a partial / mid-update Lab file
    and held back -- for up to 36 hours, after which a drop that persists is
    accepted as a real restatement. -> reason string, or None if it's fine."""
    from datetime import datetime, timezone
    try:
        old = int(((prev.get("statewide") or {}).get("cast") or {}).get("total") or 0)
    except (TypeError, ValueError):
        return None
    if old < 200 or new_cast >= 0.8 * old:
        return None
    try:
        age_h = ((now or datetime.now(timezone.utc)) - datetime.strptime(prev.get("generated_at", ""), "%Y-%m-%dT%H:%M:%SZ")
                 .replace(tzinfo=timezone.utc)).total_seconds() / 3600
    except ValueError:
        age_h = 0
    if age_h > 36:
        return None
    return "ballots cast fell from %s to %s; holding the previous figures (probably a partial or mid-update source file)" % (
        format(old, ","), format(new_cast, ","))


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
    bad = prev.get("lab_standin") and suspicious_drop(prev, snap["statewide"]["cast"]["total"])
    if bad:
        print("%s: %s" % (code, bad), file=sys.stderr)
        return True
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
