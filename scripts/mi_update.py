#!/usr/bin/env python3
"""Michigan absentee and early-voting turnout by county (turnout-only; MI has
no party registration).

Official source: the MI Secretary of State "Michigan Voting Dashboard", a Power
BI publish-to-web report (US Government cloud, iowa cluster), queried the way
the page does (see oh_update.PowerBI). Per county, for the election whose name
matches source.election_match:
  mail_voted    COUNTY_AV_BALLOT_COUNT.AV_BALLOT_COUNT (returned), newest COUNT_DATE
  mail_provided AV_BALLOT_SENT_COUNT - returned (outstanding) -> ballot chase
  early_voted   COUNTY_IN_PERSON_EV_VOTE_COUNT.IN_PERSON_VOTE_COUNT, summed over days
  election_day  COUNTY_IN_PERSON_ED_VOTE_COUNT, newest COUNT_DATE (0 until Election Day)
  registered    COUNTY_VOTER_COUNT.VOTER_COUNT, newest COUNT_DATE -> turnout %
The AV and Election Day tables are daily cumulative snapshots; the early
in-person table holds daily counts (its sum equals its running-total column).

The dashboard adds an election some time after absentee voting opens. Until it
carries the general, Michigan shows the UF Election Lab's county figures (data
provided to the Lab by the Secretary of State): absentee applications and
returned ballots; used unaltered under CC BY-NC-ND 4.0, with the dashboard's
current registration counts added for turnout %.

Run:  python scripts/mi_update.py [--force] [--dry-run --election "2026 August Primary"]
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "mi"
CONFIG_PATH = os.path.join(ROOT, "config", "mi.json")
GEO_PATH = os.path.join(ROOT, "assets", "mi-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def _norm(s):
    s = str(s).lower().replace("saint ", "st ").replace("st. ", "st ")
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\s+county$", "", s.strip()))


def _day(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def _lit(ms):
    return "datetime'%s'" % _day(ms).strftime("%Y-%m-%dT%H:%M:%S")


def _block(total):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(total),
            "rep_pct": None, "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def session(src):
    from oh_update import PowerBI
    return PowerBI(src["api"], src["report_key"])


def registered_by_county(pbi):
    """{normalized county: registered voters (the dashboard's VOTER_COUNT)} from the newest snapshot, and its date."""
    last = max(r[0] for r in pbi.query("COUNTY_VOTER_COUNT", ["COUNT_DATE"], [], {}) if r[0])
    rows = pbi.query("COUNTY_VOTER_COUNT", ["COUNTY_NAME"], ["VOTER_COUNT"], {"COUNT_DATE": [_lit(last)]})
    return {_norm(n): int(v or 0) for n, v in rows if n}, _day(last).strftime("%Y-%m-%d")


def fetch_official(pbi, match):
    """-> (election name, as-of date, {normalized county: counts}) or (None, None, {})."""
    elecs = pbi.query("MDOSElectionDates", ["ELECTION_DATE", "ElectionName"], [], {})
    hits = [(d, n) for d, n in elecs if n and all(t.lower() in n.lower() for t in match)]
    if not hits:
        return None, None, {}
    edate, ename = max(hits)
    ew = {"ELECTION_DATE": [_lit(edate)]}

    def latest(ent):
        ds = [r[0] for r in pbi.query(ent, ["COUNT_DATE"], [], ew) if r[0]]
        return max(ds) if ds else None
    out = {}

    def put(rows, *keys):
        for r in rows:
            if not r[0]:
                continue
            slot = out.setdefault(_norm(r[0]), {"av_app": 0, "av_sent": 0, "av_ret": 0, "ev": 0, "ed": 0})
            for k, v in zip(keys, r[1:]):
                slot[k] += int(v or 0)
    as_of = None
    d = latest("COUNTY_AV_BALLOT_COUNT")
    if d:
        as_of = d
        put(pbi.query("COUNTY_AV_BALLOT_COUNT", ["COUNTY_NAME"], ["AV_BALLOT_COUNT", "AV_BALLOT_SENT_COUNT"],
                      dict(ew, COUNT_DATE=[_lit(d)])), "av_ret", "av_sent")
    d = latest("COUNTY_AV_APP_COUNT")
    if d:
        put(pbi.query("COUNTY_AV_APP_COUNT", ["COUNTY_NAME"], ["AV_APP_COUNT"], dict(ew, COUNT_DATE=[_lit(d)])), "av_app")
    d = latest("COUNTY_IN_PERSON_EV_VOTE_COUNT")   # daily counts: sum over days
    if d:
        as_of = max(as_of or d, d)
        put(pbi.query("COUNTY_IN_PERSON_EV_VOTE_COUNT", ["COUNTY_NAME"], ["IN_PERSON_VOTE_COUNT"], ew), "ev")
    d = latest("COUNTY_IN_PERSON_ED_VOTE_COUNT")   # daily cumulative snapshots: newest one
    if d:
        put(pbi.query("COUNTY_IN_PERSON_ED_VOTE_COUNT", ["COUNTY_NAME"], ["IN_PERSON_VOTE_COUNT"],
                      dict(ew, COUNT_DATE=[_lit(d)])), "ed")
    return ename, (_day(as_of).strftime("%Y-%m-%d") if as_of else None), out


def entity(v, reg):
    e = {"mail_voted": _block(v["av_ret"]), "mail_provided": _block(max(0, v["av_sent"] - v["av_ret"])),
         "early_voted": _block(v["ev"]), "cast": _block(v["av_ret"] + v["ev"] + v["ed"]),
         "registered": reg, "turnout_pct": C.pct(v["av_ret"] + v["ev"] + v["ed"], reg) if reg else None}
    if v["ed"]:
        e["election_day"] = _block(v["ed"])
    m = C.compute_mail(e)
    if m:
        e["mail"] = m
    return e


def add_turnout(snap, reg, reg_date):
    """Registered voters and turnout % (ballots cast / registered) per county and statewide."""
    total = 0
    gidx = {_norm(n): n for n in snap.get("counties", {})}
    for k, name in gidx.items():
        c = snap["counties"][name]
        r = reg.get(k, 0)
        total += r
        c["registered"] = r
        c["turnout_pct"] = C.pct(c["cast"]["total"], r) if r else None
    sw = snap["statewide"]
    sw["registered"] = total or sum(reg.values())
    sw["turnout_pct"] = C.pct(sw["cast"]["total"], sw["registered"]) if sw["registered"] else None
    snap["registration_as_of"] = reg_date


def write(snap, prev):
    snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k not in ("source_compiled_iso", "generated_at")})
    changed = snap["data_hash"] != prev.get("data_hash")
    now = C.utc_now_iso()
    snap["generated_at"] = now if changed else prev.get("generated_at", now)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    c = snap["statewide"]["cast"]["total"]
    if changed and c:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"generated_at": now, "cast": c, "registered": snap["statewide"].get("registered", 0),
                                "data_hash": snap["data_hash"]}, separators=(",", ":")) + "\n")
    return changed


def main():
    force = "--force" in sys.argv
    dry = "--dry-run" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg.get("source", {})
    prev = load(LATEST_PATH, {}) or {}
    if not (force or dry) and prev.get("statewide", {}).get("cast", {}).get("total") and C.checked_recently(DATA_DIR):
        print("mi: checked under an hour ago.")
        return 0

    match = [_arg("--election")] if _arg("--election") else src.get("election_match", ["2026", "November", "General"])
    reg, reg_date, ename, as_of, rows = {}, None, None, None, {}
    try:
        pbi = session(src)
        reg, reg_date = registered_by_county(pbi)
        ename, as_of, rows = fetch_official(pbi, match)
    except Exception as e:  # noqa: BLE001
        print("MI dashboard unavailable: %s" % str(e)[:160], file=sys.stderr)

    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}
    if rows:
        unmatched = [k for k in rows if k not in gidx]
        counties = {gidx[k]["name"]: dict(entity(v, reg.get(k, 0)), fips=gidx[k]["fips"])
                    for k, v in rows.items() if k in gidx}
        tot = {k: sum(v[k] for v in rows.values()) for k in ("av_app", "av_sent", "av_ret", "ev", "ed")}
        statewide = entity(tot, sum(reg.values()))
        if dry:
            print("DRY RUN  %s | as of %s | %d counties (unmatched %s) | returned %d of %d sent (%d applications) | "
                  "early in person %d | election day %d | registered %d (%s) | turnout %s%%"
                  % (ename, as_of, len(counties), unmatched, tot["av_ret"], tot["av_sent"], tot["av_app"], tot["ev"],
                     tot["ed"], statewide["registered"], reg_date, statewide["turnout_pct"]))
            return 0
        if unmatched:
            print("MI: unmatched counties %s — keeping previous snapshot." % unmatched[:5], file=sys.stderr)
            return 0
        snap = {
            "state": STATE, "state_name": cfg.get("state_name", "Michigan"), "election": cfg.get("election", {}),
            "partisan": False,
            "source": {"primary": "Michigan Secretary of State Voting Dashboard (%s), data through %s; registered "
                                  "voters as of %s" % (ename, as_of, reg_date),
                       "url": src.get("dashboard"), "as_of": as_of},
            "source_compiled": as_of, "source_compiled_iso": C.utc_now_iso(),
            "methods_present": [k for k in ("mail_voted", "early_voted", "election_day", "mail_provided")
                                if statewide.get(k, {}).get("total")],
            "method_labels": cfg.get("method_labels", {}), "statewide": statewide, "counties": counties,
            "registration_as_of": reg_date,
        }
        changed = write(snap, prev)
        print("%s  MI dashboard %s through %s: %d counties | cast=%d (returned %d, early %d) of %d registered = %s%%"
              % ("CHANGED" if changed or force else "NOCHANGE", ename, as_of, len(counties), statewide["cast"]["total"],
                 tot["av_ret"], tot["ev"], statewide["registered"], statewide["turnout_pct"]))
        return 0

    if dry:
        print("DRY RUN  no election matching %s on the dashboard; registered %d (%s)"
              % (match, sum(reg.values()), reg_date))
        return 0
    # dashboard doesn't carry the general yet: UF Election Lab stand-in, plus registration for turnout %
    import lab_standin as LAB
    snap = LAB.build(STATE, cfg, GEO_PATH, partisan=False,
                     role="stand-in until the SoS Voting Dashboard carries the general")
    if not snap:
        print("mi: no Election Lab figures for Michigan yet.")
        return 0
    if reg:
        add_turnout(snap, reg, reg_date)
        snap["source"]["primary"] += "; registered voters: MI SoS Voting Dashboard, %s" % reg_date
    changed = write(snap, prev)
    sw = snap["statewide"]
    print("%s  Election Lab stand-in as of %s: %d counties | returned=%d of %s applications | turnout %s%%"
          % ("CHANGED" if changed else "NOCHANGE", snap["source"].get("as_of"), len(snap["counties"]),
             sw["cast"]["total"], (sw.get("mail") or {}).get("requested"), sw.get("turnout_pct")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
