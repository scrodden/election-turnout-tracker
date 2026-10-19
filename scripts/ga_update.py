#!/usr/bin/env python3
"""Georgia turnout by county (turnout-only; GA has no party registration).

Source: GA Secretary of State "Election Data Hub - Unofficial Turnout", a Qlik
Cloud app (tenant sos-ga-gov.us.qlikcloudgov.com). Its sos.ga.gov page is
behind a Cloudflare browser challenge; the Qlik tenant and the public token
endpoint the page uses for every visitor are not. scripts/qlik.py talks to the
app the way the page does (see that module).

The app holds every recent GA election (Election Name) with per-voter ballot
counters by County and Ballot Style, plus twice-monthly registration
snapshots. For the configured election we pull, per county:
  early_voted   Ballots_Accepted_Counter, style 'Early In-Person'
  mail_voted    Ballots_Accepted_Counter, styles 'Absentee by mail' + 'Electronic Ballot Delivery'
  mail_provided mail issued - accepted - rejected - other (outstanding) -> ballot chase
  election_day  Ballots_Accepted_Counter, styles 'Election Day*'
  registered    active voters in the newest registration snapshot on/before today

Polling: the hub reloads about every 10-20 minutes during voting, so once
source.every_minutes is set the hub is read on every run that far apart (each
read opens one anonymous session, as each page visit does); before that, once
per window in source.fetch_hours_utc. The app's last reload time is kept as
the data's "as of" time.

Run:  python scripts/ga_update.py [--force] [--dry-run] [--election "NOVEMBER 5, 2024 - GENERAL ELECTION"]
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402
from qlik import QlikSession  # noqa: E402

STATE = "ga"
CONFIG_PATH = os.path.join(ROOT, "config", "ga.json")
GEO_PATH = os.path.join(ROOT, "assets", "ga-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
CD_GEO_PATH = os.path.join(ROOT, "assets", "ga-cd.geojson")
DISTRICTS_PATH = os.path.join(DATA_DIR, "districts.json")
RACE_SHARE_PATH = os.path.join(DATA_DIR, "race_share.json")

MAIL_STYLES = ["Absentee by mail", "Electronic Ballot Delivery"]
EIP_STYLES = ["Early In-Person"]


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _block(total):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(total),
            "rep_pct": None, "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def _arg(name):
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    return None


def due(last_iso, hours):
    """True when no fetch has happened since the most recent fetch window."""
    now = datetime.now(timezone.utc)
    marks = [now.replace(hour=h, minute=0, second=0, microsecond=0) - timedelta(days=d)
             for h in hours for d in (0, 1)]
    boundary = max(m for m in marks if m <= now)
    try:
        last = datetime.strptime(last_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return last < boundary


def pick_elections(names, tokens):
    """Every Election Name matching all tokens. The hub can carry one election
    under several names (2026: 'NOVEMBER 3, 2026 - GENERAL ELECTION', '... -
    GENERAL & SPECIAL ELECTIONS', '... - GENERAL AND SPECIAL ELECTIONS'), with
    counties' ballots split among them, so all of them are summed."""
    return sorted(n for n in names if all(t.upper() in n.upper() for t in tokens))


MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def election_snapshot_date(name):
    """'NOVEMBER 3, 2026 - GENERAL ELECTION' -> '11/03/2026'. The hub's
    registration snapshot dated on an election day is the one linked to
    counties (the twice-monthly ones are statewide only)."""
    m = re.match(r"\s*([A-Z]+)\s+(\d{1,2}),\s*(\d{4})", name or "")
    if not m or m.group(1)[:3] not in MONTHS:
        return None
    return "%02d/%02d/%s" % (MONTHS.index(m.group(1)[:3]) + 1, int(m.group(2)), m.group(3))


# the hub's voter demographics (DVB_AGG, joined to each ballot's voter record)
DEMO_FIELDS = (("age", "Age Group"), ("gender", "Gender_Clean"), ("race", "RACE_DESC"))


def _age_key(label):
    m = re.match(r"(\d+)", label or "")
    return int(m.group(1)) if m else 999


def hub_demographics(s, e):
    """Accepted ballots by age group / gender / race, statewide and by county,
    in the hub's own categories ('-' = no linked voter record -> 'Unknown').
    -> {"statewide": {age: [...], ...}, "counties": {COUNTY: {...}}}"""
    meas = ["Sum({1<%s>} Ballots_Accepted_Counter)" % e]
    out = {"statewide": {}, "counties": {}}

    def norm_list(pairs, key):
        acc = {}
        for lab, v in pairs:
            lab = "Unknown" if (not lab or lab == "-") else lab
            acc[lab] = acc.get(lab, 0) + v
        items = [{"label": k, "count": v} for k, v in acc.items() if v]
        if key == "age":
            items.sort(key=lambda x: (_age_key(x["label"]), x["label"]))
        else:   # named groups by size, then the hub's catch-alls
            items.sort(key=lambda x: (x["label"] == "Unknown", "unknown" in x["label"].lower() or x["label"] == "Other", -x["count"]))
        return items

    for key, field in DEMO_FIELDS:
        rows = s.cube([field], meas)
        out["statewide"][key] = norm_list([(r[0], int(r[1] or 0)) for r in rows], key)
        by = {}
        for r in s.cube(["County", field], meas, max_rows=5000):
            if r[0] and r[0] != "-":
                by.setdefault(r[0], []).append((r[1], int(r[2] or 0)))
        for cty, pairs in by.items():
            out["counties"].setdefault(cty, {})[key] = norm_list(pairs, key)
    return out


def election_day_of(name):
    """'NOVEMBER 3, 2026 - GENERAL ELECTION' -> date(2026, 11, 3), else None."""
    try:
        return datetime.strptime(name.split(" - ")[0].strip().title(), "%B %d, %Y").date()
    except ValueError:
        return None


def race_by_day(s, e, eday, last_day):
    """Accepted ballots per Ballot Accepted Date x race (the hub's RACE_DESC on
    each voter's record; '-' = no linked record -> Unknown), one entry per day
    through min(Election Day, last_day). Ballots with no accepted date, or one
    after that (late-cured ballots, a few mistyped years), are added to the last
    day so the running total matches the demographics table.
    -> {"election_day", "dates": [iso...], "counts": {race: [per-day...]}, "total"}"""
    rows = s.cube(["Ballot Accepted Date", "RACE_DESC"], ["Sum({1<%s>} Ballots_Accepted_Counter)" % e], max_rows=20000)
    stop = min(eday, last_day) if eday else last_day
    days, extra = {}, {}
    for d, race, v in rows:
        v = int(v or 0)
        if not v:
            continue
        race = "Unknown" if (not race or race == "-") else race
        try:
            dt = datetime.strptime(d, "%m/%d/%Y").date()
        except (TypeError, ValueError):
            dt = None
        bucket = extra if (dt is None or dt > stop) else days.setdefault(dt, {})
        bucket[race] = bucket.get(race, 0) + v
    early = {}   # mistyped years (1924, 1951 ...): more than 120 days out -> the first real day
    if eday:
        for dt in [x for x in days if x < eday - timedelta(days=120)]:
            for race, v in days.pop(dt).items():
                early[race] = early.get(race, 0) + v
    if not days:
        return None
    first, last = min(days), max(days)
    for race, v in early.items():
        days[first][race] = days[first].get(race, 0) + v
    for race, v in extra.items():
        days[last][race] = days[last].get(race, 0) + v
    dates = sorted(days)
    races = sorted({r for d in days.values() for r in d}, key=lambda r: (r == "Unknown", r == "Other/Unknown", r))
    return {"election_day": eday.isoformat() if eday else None, "dates": [d.isoformat() for d in dates],
            "counts": {r: [days[d].get(r, 0) for d in dates] for r in races},
            "total": sum(sum(d.values()) for d in days.values())}


def fetch(src, election_override=None):
    """-> (election_name, snapshot_date, {COUNTY: {...counts}}, {CD: {...counts}}) or
    (None, None, {}, {}) if the configured election isn't in the hub yet. The
    district rows come from each ballot's registration record
    (Snapshot.CONGRESSIONAL_DISTRICT)."""
    s = QlikSession(src["token_url"], src["qlik_host"], src["app_id"])
    try:
        reload_at = (s.call(s.doc, "GetAppLayout", {})["qLayout"].get("qLastReloadTime") or "")[:19] + "Z"
        names = s.values("Election Name")
        elecs = [election_override] if election_override else pick_elections(names, src["election_match"])
        if not elecs:
            return None, None, {}, {}, reload_at, {}
        elec = " + ".join(elecs)
        snap_date = election_snapshot_date(elecs[0])
        if snap_date not in s.values("Snapshot.AsOfDate"):
            snap_date = None

        def styles(lst):
            return ",".join("'%s'" % x for x in lst)
        e = "[Election Name]={%s}" % ",".join("'%s'" % n.replace("'", "''") for n in elecs)
        mail = "{1<%s,[Ballot Style]={%s}>}" % (e, styles(MAIL_STYLES))
        eip = "{1<%s,[Ballot Style]={%s}>}" % (e, styles(EIP_STYLES))
        eday = '{1<%s,[Ballot Style]={"Election Day*"}>}' % e
        cols = ["eip", "mail", "mail_issued", "mail_rejected", "mail_other", "eday"]
        measures = [
            "Sum(%s Ballots_Accepted_Counter)" % eip,
            "Sum(%s Ballots_Accepted_Counter)" % mail,
            "Sum(%s Ballots_Issued_Counter)" % mail,
            "Sum(%s Ballots_Rejected_Counter)" % mail,
            "Sum(%s Ballots_Other_Counter)" % mail,
            "Sum(%s Ballots_Accepted_Counter)" % eday]
        rows = s.cube(["County"], measures)
        out = {}
        for r in rows:
            if not r[0] or r[0] == "-":
                continue
            out[r[0]] = {k: int(v or 0) for k, v in zip(cols, r[1:])}
        demo = hub_demographics(s, e)
        today_et = (datetime.now(timezone.utc) - timedelta(hours=4)).date()
        demo["race_by_day"] = {"current": race_by_day(s, e, election_day_of(elecs[0]), today_et), "compare": {}}
        prev_share = load(RACE_SHARE_PATH, {}) or {}
        for yr, name in (src.get("race_compare") or {}).items():   # past generals: fixed, fetched once
            old = (prev_share.get("series") or {}).get(yr)
            if old and old.get("election") == name:
                demo["race_by_day"]["compare"][yr] = old
            elif name in names:
                ser = race_by_day(s, "[Election Name]={'%s'}" % name.replace("'", "''"), election_day_of(name), election_day_of(name))
                if ser:
                    demo["race_by_day"]["compare"][yr] = dict(ser, election=name)
        dist = {}
        for r in s.cube(["Snapshot.CONGRESSIONAL_DISTRICT"], measures):
            if r[0] and r[0].strip().isdigit():
                dist["CD%d" % int(r[0])] = {k: int(v or 0) for k, v in zip(cols, r[1:])}
        if snap_date:
            status = ",".join("'%s'" % x for x in src.get("registered_status", ["Active"]))
            reg = s.cube(["County"], ["Sum({1<[Snapshot.AsOfDate]={'%s'},[Snapshot.Voter Status]={%s}>} [Snapshot.VoterCount])"
                                      % (snap_date, status)])
            linked = {r[0]: int(r[1] or 0) for r in reg if r[0] in out}
            total = sum(int(r[1] or 0) for r in reg)
            # only trust it once (nearly) every voter in the snapshot is tied to a county
            if total and sum(linked.values()) >= 0.98 * total:
                for cn, v in linked.items():
                    out[cn]["registered"] = v
            else:
                snap_date = None
        return elec, snap_date, out, dist, reload_at, demo
    finally:
        s.close()


def build(cfg, elec, snap_date, rows):
    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}
    counties, unmatched = {}, []
    t = {k: 0 for k in ("eip", "mail", "mail_out", "eday", "reg")}
    for name, r in rows.items():
        g = gidx.get(_norm(name))
        if not g:
            unmatched.append(name)
            continue
        out = max(0, r["mail_issued"] - r["mail"] - r["mail_rejected"] - r["mail_other"])
        cast = r["eip"] + r["mail"] + r["eday"]
        reg = r.get("registered", 0)
        ent = {"fips": g["fips"], "cast": _block(cast), "early_voted": _block(r["eip"]),
               "mail_voted": _block(r["mail"]), "mail_provided": _block(out),
               "registered": reg, "turnout_pct": (round(100.0 * cast / reg, 2) if reg else None)}
        if r["eday"]:
            ent["election_day"] = _block(r["eday"])
        m = C.compute_mail(ent)
        if m:
            ent["mail"] = m
        counties[g["name"]] = ent
        t["eip"] += r["eip"]; t["mail"] += r["mail"]; t["mail_out"] += out
        t["eday"] += r["eday"]; t["reg"] += reg
    cast = t["eip"] + t["mail"] + t["eday"]
    statewide = {"cast": _block(cast), "early_voted": _block(t["eip"]), "mail_voted": _block(t["mail"]),
                 "mail_provided": _block(t["mail_out"]), "registered": t["reg"],
                 "turnout_pct": (round(100.0 * cast / t["reg"], 2) if t["reg"] else None)}
    if t["eday"]:
        statewide["election_day"] = _block(t["eday"])
    m = C.compute_mail(statewide)
    if m:
        statewide["mail"] = m
    methods_present = [k for k, v in (("early_voted", t["eip"]), ("mail_voted", t["mail"]),
                                      ("election_day", t["eday"]), ("mail_provided", t["mail_out"])) if v]
    return statewide, counties, methods_present, unmatched


def write_districts(cfg, elec, drows, now):
    """data/ga/districts.json (the map's Districts view) from the hub's
    by-district counts, in the same shape the Election Lab stand-in used."""
    geo = load(CD_GEO_PATH, {"features": []})
    names = {f["properties"]["name"] for f in geo["features"]}
    units, t = {}, {k: 0 for k in ("eip", "mail", "mail_out", "eday")}
    for name, r in sorted(drows.items()):
        if name not in names:
            continue
        out = max(0, r["mail_issued"] - r["mail"] - r["mail_rejected"] - r["mail_other"])
        ent = {"cast": _block(r["eip"] + r["mail"] + r["eday"]), "early_voted": _block(r["eip"]),
               "mail_voted": _block(r["mail"]), "mail_provided": _block(out)}
        if r["eday"]:
            ent["election_day"] = _block(r["eday"])
        m = C.compute_mail(ent)
        if m:
            ent["mail"] = m
        units[name] = ent
        t["eip"] += r["eip"]; t["mail"] += r["mail"]; t["mail_out"] += out; t["eday"] += r["eday"]
    if not units:
        return False
    statewide = {"cast": _block(t["eip"] + t["mail"] + t["eday"]), "early_voted": _block(t["eip"]),
                 "mail_voted": _block(t["mail"]), "mail_provided": _block(t["mail_out"])}
    if t["eday"]:
        statewide["election_day"] = _block(t["eday"])
    body = {"state": STATE, "election": cfg.get("election", {}), "unit_label": "District", "unit_label_plural": "Districts",
            "partisan": False,
            "source": "GA SoS Election Data Hub (Qlik): accepted ballots by U.S. House district of each voter's registration",
            "methods_present": [k for k, v in (("early_voted", t["eip"]), ("mail_voted", t["mail"]),
                                                ("election_day", t["eday"]), ("mail_provided", t["mail_out"])) if v],
            "method_labels": cfg.get("method_labels", {}),
            "coverage": {"ok": len(units), "total": len(names), "unmatched": sorted(set(drows) - names)},
            "statewide": statewide, "counties": units}
    h = C.data_hash(dict(body, hub_election=elec))
    prev = load(DISTRICTS_PATH, {}) or {}
    changed = h != prev.get("data_hash")
    body.update({"as_of": now[:10], "source_compiled": now[:10] if changed else prev.get("source_compiled", now[:10]),
                 "source_compiled_iso": now if changed else prev.get("source_compiled_iso", now),
                 "data_hash": h, "generated_at": now if changed else prev.get("generated_at", now)})
    with open(DISTRICTS_PATH, "w", encoding="utf-8") as f:
        json.dump(body, f, separators=(",", ":"))
    return True


LAB_AGE = [("18-25", "1"), ("26-40", "2"), ("41-65", "3"), ("Over 65", "4")]
LAB_RACE = [("White", "nh_white"), ("Black", "nh_black"), ("Hispanic", "hispanic"), ("Asian American", "nh_asian"),
            ("Native American", "nh_native_american"), ("Other/unknown", "nh_other")]


def _lab_int(v):
    v = str(v or "").strip()
    try:
        return int(float(v))
    except ValueError:
        return 0


def lab_ga_row(src):
    """The UF Election Lab's Georgia row from US.csv, or None."""
    url = src.get("lab_csv")
    if not url:
        return None
    try:
        return next((r for r in lab_rows(url) if (r.get("state_abbv") or "").upper() == "GA"), None)
    except Exception as e:  # noqa: BLE001
        print("GA: Election Lab CSV unavailable: %s" % str(e)[:100], file=sys.stderr)
        return None


def lab_rows(url):
    import csv
    import io
    return list(csv.DictReader(io.StringIO(C.http_get(url, no_cache=True))))


def lab_entity(req, ret, inp):
    e = {"mail_voted": _block(ret), "mail_provided": _block(max(0, req - ret)), "early_voted": _block(inp),
         "cast": _block(ret + inp), "registered": 0, "turnout_pct": None}
    mm = C.compute_mail(e)
    if mm:
        e["mail"] = mm
    return e


def lab_demo(get):
    """Age / gender / race of ballots cast from a Lab row (voted_* columns); None if none cast."""
    if not get("voted_all"):
        return None
    return {"age": [{"label": lab, "count": get("voted_age_" + k)} for lab, k in LAB_AGE],
            "gender": [{"label": "Female", "count": get("voted_female")}, {"label": "Male", "count": get("voted_male")},
                       {"label": "Unknown", "count": get("voted_gender_unknown")}],
            "race": [{"label": lab, "count": get("voted_" + k)} for lab, k in LAB_RACE]}


def reconcile(parts, total_get):
    """Lab county/district rows vs its statewide row. Every ballot cast must be
    attributed (accepted + in-person sums match exactly); a few mail-ballot
    requests may carry no county/district (<= 1%). -> (ok, unassigned requests)."""
    exact = all(sum(_lab_int(r.get(k)) for r in parts) == total_get(k) for k in ("accept_all", "inperson_all"))
    gap = total_get("request_all") - sum(_lab_int(r.get("request_all")) for r in parts)
    return exact and 0 <= gap <= max(50, total_get("request_all") // 100), gap


def unassigned_note(gap, total):
    if not gap:
        return None
    return ("%s of the %s mail-ballot requests in the Election Lab's data have no county or district, so "
            "request figures on the map add up to %s." % (format(gap, ","), format(total, ","), format(total - gap, ",")))


def lab_fallback(cfg, src, now, status, row=None):
    """Snapshot from the UF Election Lab (its source is the SoS absentee voter
    file): statewide from its Georgia row, counties from GA_county.csv when
    they reconcile. Used unaltered, with attribution, under CC BY-NC-ND 4.0,
    only while the SoS hub lacks the general. None if absent."""
    row = row or lab_ga_row(src)

    def n(k):
        return _lab_int((row or {}).get(k))
    if not row or not n("request_all"):
        return None
    statewide = lab_entity(n("request_all"), n("accept_all"), n("inperson_all"))
    counties, gap = {}, 0
    try:
        crow = lab_rows(src["lab_county_csv"])
        geo = load(GEO_PATH, {"features": []})
        gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}
        ok, gap = reconcile(crow, n)
        for r in crow:
            g = gidx.get(_norm(r.get("county")))
            if g:
                ent = dict(lab_entity(_lab_int(r.get("request_all")), _lab_int(r.get("accept_all")),
                                      _lab_int(r.get("inperson_all"))), fips=g["fips"])
                d = lab_demo(lambda k, r=r: _lab_int(r.get(k)))
                if d:
                    ent["demographics"] = d
                counties[g["name"]] = ent
        if not ok or len(counties) != len(crow):
            print("GA: Lab county file doesn't reconcile with the statewide row — statewide only.", file=sys.stderr)
            counties, gap = {}, 0
    except Exception as e:  # noqa: BLE001
        print("GA: Lab county file unavailable: %s" % str(e)[:100], file=sys.stderr)
        counties, gap = {}, 0
    body = {
        "state": STATE, "state_name": cfg.get("state_name", "Georgia"), "election": cfg.get("election", {}),
        "partisan": False, "statewide_only": not counties,
        "methods_present": [k for k in ("mail_voted", "early_voted", "mail_provided") if statewide[k]["total"]],
        "method_labels": cfg.get("method_labels", {}), "statewide": statewide, "counties": counties,
        "demographics": lab_demo(n),
    }
    note = unassigned_note(gap, n("request_all"))
    if note:
        body["map_note"] = note
    if cfg.get("mail_base_label"):   # the Lab counts absentee applications, not ballots mailed
        body["mail_base_label"] = cfg["mail_base_label"]
    snap = dict(body)
    snap["source"] = {"primary": "UF Election Lab early-vote tracker (M. McDonald), from the GA SoS absentee voter file; "
                                 "CC BY-NC-ND 4.0 -- stand-in until the SoS hub loads the general",
                      "url": src.get("lab_page"), "as_of": row.get("last_update", ""),
                      "fetched_at": now, "status": status + "; using Election Lab fallback"}
    snap["source_compiled"] = row.get("last_update", "")
    snap["source_compiled_iso"] = now
    snap["data_hash"] = C.data_hash(body)
    return snap


def write_lab(snap, prev, now):
    changed = snap["data_hash"] != prev.get("data_hash")
    snap["generated_at"] = now if changed else prev.get("generated_at", now)
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    cast = snap["statewide"]["cast"]["total"]
    if changed and cast:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"generated_at": now, "cast": cast, "data_hash": snap["data_hash"]},
                               separators=(",", ":")) + "\n")
    print("%s  Election Lab fallback: %d counties | returned=%d of %d requested (as of %s)"
          % ("CHANGED" if changed else "NOCHANGE", len(snap["counties"]), cast,
             (snap["statewide"].get("mail") or {}).get("requested", 0), snap["source"].get("as_of")))


def write_race_share(cfg, elec, rbd, as_of):
    """data/ga/race_share.json: ballots by accepted date and race for this
    election and the comparison generals (config source.race_compare), for the
    page's "share of ballots over time" chart. Rewritten only when it changes."""
    if not rbd or not rbd.get("current"):
        return
    yr = (cfg.get("election", {}).get("date") or "")[:4] or "current"
    series = {yr: dict(rbd["current"], election=elec)}
    series.update(rbd.get("compare") or {})
    body = {"state": STATE, "series": series, "markers": (cfg.get("source") or {}).get("race_markers", []),
            "compare_note": "Past elections use today's voter records, so voters since removed from the rolls count as Unknown.",
            "source": "GA SoS Election Data Hub (Qlik): accepted ballots by Ballot Accepted Date and the race on each "
                      "voter's registration record (the hub's categories; Unknown = no linked voter record). Past "
                      "elections use today's voter records, so voters since removed from the rolls count as Unknown."}
    prev = load(RACE_SHARE_PATH, {}) or {}
    h = C.data_hash(body)
    if h == prev.get("data_hash"):
        return
    with open(RACE_SHARE_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(dict(body, as_of=as_of, data_hash=h), f, separators=(",", ":"))
    print("ga: race_share.json updated (%s)" % ", ".join("%s: %d ballots" % (k, v["total"]) for k, v in sorted(series.items())))


def main():
    force = "--force" in sys.argv
    dry = "--dry-run" in sys.argv
    override = _arg("--election")
    if override and not dry:
        print("--election is for testing against past elections; use it with --dry-run.")
        return 2
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg.get("source", {})
    prev = load(LATEST_PATH, {}) or {}
    psrc = prev.get("source") or {}

    now = C.utc_now_iso()
    if not dry:
        os.makedirs(DATA_DIR, exist_ok=True)
        on_hub = str(psrc.get("primary", "")).startswith("GA SoS")
        if not on_hub:   # district view: the Lab's GA_cd.csv, ~hourly, until the hub has the general
            import lab_standin as LAB
            LAB.districts(STATE, cfg, CD_GEO_PATH, DISTRICTS_PATH, partisan=False, force=force)

    hub_last = psrc.get("hub_checked_at") or psrc.get("fetched_at")
    if src.get("every_minutes"):   # live: read the hub every run, at most this often
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(hub_last.replace("Z", "+00:00"))).total_seconds() / 60
        except (AttributeError, ValueError):
            age = None
        hub_due = age is None or age >= float(src["every_minutes"])
    else:
        hub_due = due(hub_last, src.get("fetch_hours_utc", [11, 23]))
    if not (force or dry) and not hub_due:
        # the hub is only tried twice a day; while we're on the Lab fallback,
        # re-check the Lab about hourly in between
        on_lab = "Election Lab" in str(psrc.get("primary", "")) or not prev.get("statewide")
        if on_lab and not C.checked_recently(DATA_DIR, "lab"):
            snap = lab_fallback(cfg, src, now, str(psrc.get("status", "waiting")).split("; using")[0])
            if snap:
                snap["source"]["hub_checked_at"] = hub_last
                write_lab(snap, prev, now)
                if str(psrc.get("status", "")).startswith("error"):   # keep the hub error on the status page
                    print("ga hub failed at its last check (%s) -- showing the Election Lab fallback" % str(psrc["status"])[:160])
                    return 2
                return 0
        print("ga: hub checked %s; next window not reached — skipping." % hub_last)
        return 0

    try:
        elec, snap_date, rows, drows, reload_at, demo = fetch(src, override)
        status = "ok" if elec else "waiting: no election matching %s in the hub yet" % src["election_match"]
    except Exception as e:  # noqa: BLE001
        elec, snap_date, rows, drows, reload_at, demo, status = None, None, {}, {}, "", {}, "error: %s" % str(e)[:520]
    print("ga: %s" % status)

    if dry:
        if rows:
            sw, cty, mp, um = build(cfg, elec, snap_date, rows)
            print("DRY RUN  %s | reg snapshot %s | counties=%d | cast=%d (EIP %d, mail %d, eday %d) | "
                  "mail issued-outstanding=%d | registered=%d turnout=%s%% | unmatched=%s"
                  % (elec, snap_date, len(cty), sw["cast"]["total"], sw["early_voted"]["total"],
                     sw["mail_voted"]["total"], (sw.get("election_day") or {}).get("total", 0),
                     sw["mail_provided"]["total"], sw["registered"], sw["turnout_pct"], um))
        return 0

    if not rows:
        # Until the hub carries the general, fall back to the UF Election Lab's
        # Georgia figures (built from the SoS absentee voter file).
        snap = lab_fallback(cfg, src, now, status)
        if snap:
            snap["source"]["hub_checked_at"] = now
            write_lab(snap, prev, now)
            if status.startswith("error"):   # the page stays current, but flag the hub on the status page
                print("ga hub failed (%s) -- showing the Election Lab fallback" % status)   # (10/18: new token URL / app id)
                return 2
            return 0
        # record the attempt so the next try waits for the next window
        snap = prev or {"state": STATE, "state_name": cfg.get("state_name", "Georgia"),
                        "election": cfg.get("election", {}), "partisan": False,
                        "methods_present": [], "method_labels": cfg.get("method_labels", {}),
                        "statewide": {"cast": _block(0)}, "counties": {}}
        snap["source"] = dict(psrc, fetched_at=now, hub_checked_at=now, status=status)
        with open(LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        return 0

    statewide, counties, methods_present, unmatched = build(cfg, elec, snap_date, rows)
    if drows and sum(r["eip"] + r["mail"] + r["eday"] for r in drows.values()) == statewide["cast"]["total"]:
        write_districts(cfg, elec, drows, reload_at if len(reload_at) > 10 else now)   # only when they add up to the county total
    body = {
        "state": STATE, "state_name": cfg.get("state_name", "Georgia"), "election": cfg.get("election", {}),
        "partisan": False, "methods_present": methods_present, "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties,
    }
    if demo.get("statewide", {}).get("age"):
        dm = dict(demo["statewide"])
        dm["note"] = ("Ballots cast so far by the age group, gender and race on each voter's registration record, "
                      "from the GA SoS Election Data Hub (its categories; Unknown = no linked voter record).")
        body["demographics"] = dm
        for cname, ent in counties.items():
            d = demo["counties"].get(cname.upper()) or demo["counties"].get(cname)
            if d:
                ent["demographics"] = d
    h = C.data_hash(dict(body, hub_election=elec, reg_snapshot=snap_date))
    changed = force or h != prev.get("data_hash")
    snap = dict(body)
    snap["source"] = {"primary": "GA SoS Election Data Hub (Qlik): accepted ballots by county & method; turnout-only",
                      "hub_election": elec, "registration_snapshot": snap_date, "fetched_at": now,
                      "hub_checked_at": now, "hub_reloaded_at": reload_at, "status": status}
    as_of = reload_at if len(reload_at) > 10 else now   # the hub's own refresh time
    snap["source_compiled"] = as_of[:10] if changed else prev.get("source_compiled", as_of[:10])
    snap["source_compiled_iso"] = as_of if changed else prev.get("source_compiled_iso", as_of)
    snap["data_hash"] = h
    snap["generated_at"] = now if changed else prev.get("generated_at", now)
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    write_race_share(cfg, elec, demo.get("race_by_day"), as_of)
    cast = statewide["cast"]["total"]
    try:   # a new trend point only when the counts move (not when only demographics / labels change)
        with open(HISTORY_PATH, encoding="utf-8") as f:
            last = json.loads([x for x in f.read().splitlines() if x.strip()][-1])
    except (OSError, ValueError, IndexError):
        last = {}
    if changed and cast and (last.get("cast"), last.get("registered")) != (cast, statewide["registered"]):
        with open(HISTORY_PATH, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"generated_at": now, "cast": cast, "registered": statewide["registered"],
                                "data_hash": h}, separators=(",", ":")) + "\n")
    print("%s  %s | counties=%d cast=%d turnout=%s%%%s"
          % ("CHANGED" if changed else "NOCHANGE", elec, len(counties), cast, statewide["turnout_pct"],
             ("  unmatched: %s" % unmatched[:10]) if unmatched else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
