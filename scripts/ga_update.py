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

Polite polling: each fetch creates an anonymous user on the state's Qlik
tenant, so we fetch at most once per window in source.fetch_hours_utc (twice a
day) and record the attempt in latest.json.

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


def pick_election(names, tokens):
    hits = [n for n in names if all(t.upper() in n.upper() for t in tokens)]
    return hits[0] if hits else None


MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def election_snapshot_date(name):
    """'NOVEMBER 3, 2026 - GENERAL ELECTION' -> '11/03/2026'. The hub's
    registration snapshot dated on an election day is the one linked to
    counties (the twice-monthly ones are statewide only)."""
    m = re.match(r"\s*([A-Z]+)\s+(\d{1,2}),\s*(\d{4})", name or "")
    if not m or m.group(1)[:3] not in MONTHS:
        return None
    return "%02d/%02d/%s" % (MONTHS.index(m.group(1)[:3]) + 1, int(m.group(2)), m.group(3))


def fetch(src, election_override=None):
    """-> (election_name, snapshot_date, {COUNTY: {...counts}}) or (None, None, {}) if
    the configured election isn't in the hub yet."""
    s = QlikSession(src["token_url"], src["qlik_host"], src["app_id"])
    try:
        names = s.values("Election Name")
        elec = election_override or pick_election(names, src["election_match"])
        if not elec:
            return None, None, {}
        snap_date = election_snapshot_date(elec)
        if snap_date not in s.values("Snapshot.AsOfDate"):
            snap_date = None

        def styles(lst):
            return ",".join("'%s'" % x for x in lst)
        e = "[Election Name]={'%s'}" % elec
        mail = "{1<%s,[Ballot Style]={%s}>}" % (e, styles(MAIL_STYLES))
        eip = "{1<%s,[Ballot Style]={%s}>}" % (e, styles(EIP_STYLES))
        eday = '{1<%s,[Ballot Style]={"Election Day*"}>}' % e
        cols = ["eip", "mail", "mail_issued", "mail_rejected", "mail_other", "eday"]
        rows = s.cube(["County"], [
            "Sum(%s Ballots_Accepted_Counter)" % eip,
            "Sum(%s Ballots_Accepted_Counter)" % mail,
            "Sum(%s Ballots_Issued_Counter)" % mail,
            "Sum(%s Ballots_Rejected_Counter)" % mail,
            "Sum(%s Ballots_Other_Counter)" % mail,
            "Sum(%s Ballots_Accepted_Counter)" % eday])
        out = {}
        for r in rows:
            if not r[0] or r[0] == "-":
                continue
            out[r[0]] = {k: int(v or 0) for k, v in zip(cols, r[1:])}
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
        return elec, snap_date, out
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

    if not (force or dry) and not due(psrc.get("fetched_at"), src.get("fetch_hours_utc", [11, 23])):
        print("ga: fetched %s; next window not reached — skipping." % psrc.get("fetched_at"))
        return 0

    now = C.utc_now_iso()
    try:
        elec, snap_date, rows = fetch(src, override)
        status = "ok" if elec else "waiting: no election matching %s in the hub yet" % src["election_match"]
    except Exception as e:  # noqa: BLE001
        elec, snap_date, rows, status = None, None, {}, "error: %s" % str(e)[:160]
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

    os.makedirs(DATA_DIR, exist_ok=True)
    if not rows:
        # record the attempt so the next try waits for the next window
        snap = prev or {"state": STATE, "state_name": cfg.get("state_name", "Georgia"),
                        "election": cfg.get("election", {}), "partisan": False,
                        "methods_present": [], "method_labels": cfg.get("method_labels", {}),
                        "statewide": {"cast": _block(0)}, "counties": {}}
        snap["source"] = dict(psrc, fetched_at=now, status=status)
        with open(LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        return 0

    statewide, counties, methods_present, unmatched = build(cfg, elec, snap_date, rows)
    body = {
        "state": STATE, "state_name": cfg.get("state_name", "Georgia"), "election": cfg.get("election", {}),
        "partisan": False, "methods_present": methods_present, "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties,
    }
    h = C.data_hash(dict(body, hub_election=elec, reg_snapshot=snap_date))
    changed = force or h != prev.get("data_hash")
    snap = dict(body)
    snap["source"] = {"primary": "GA SoS Election Data Hub (Qlik): accepted ballots by county & method; turnout-only",
                      "hub_election": elec, "registration_snapshot": snap_date, "fetched_at": now, "status": status}
    snap["source_compiled"] = now[:10] if changed else prev.get("source_compiled", now[:10])
    snap["source_compiled_iso"] = now if changed else prev.get("source_compiled_iso", now)
    snap["data_hash"] = h
    snap["generated_at"] = now if changed else prev.get("generated_at", now)
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    cast = statewide["cast"]["total"]
    if changed and cast:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"generated_at": now, "cast": cast, "registered": statewide["registered"],
                                "data_hash": h}, separators=(",", ":")) + "\n")
    print("%s  %s | counties=%d cast=%d turnout=%s%%%s"
          % ("CHANGED" if changed else "NOCHANGE", elec, len(counties), cast, statewide["turnout_pct"],
             ("  unmatched: %s" % unmatched[:10]) if unmatched else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
