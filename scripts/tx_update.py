#!/usr/bin/env python3
"""Texas early-voting turnout by county (turnout-only; Texas has no party
registration).

Source: the Texas Secretary of State's "Election Information & Turnout Data"
app (goelect.txelections.civixapps.com/ivis-evr-ui/evr, linked from
sos.state.tx.us/elections/historical as "Early Voting Turnout (current)"). The
app reads two JSON endpoints, both returning {"upload": <base64 JSON>}:
  /api-ivis-system/api/v1/getFile?type=EVR_ELECTION
      the election list: id, election_name, election_date, early_voting_dates
  /api-ivis-system/api/v1/getFile?type=EVR_EARLYVOTING&electionId=<id>&electionDate=MM/DD/YYYY
      date_updated + turnout_by_county: name, registered_voters,
      in_person_votes_on_date, total_in_person_votes_for_election,
      total_mail_votes_for_election (running totals through the date asked,
      or through the latest report for a later date)
All 254 counties; figures are unofficial, as submitted by county election
officials. Ballots cast = in-person early votes + mail ballots received.

Early voting: October 19 - 30, 2026 (mail ballots are counted before that).

Run:  python scripts/tx_update.py [--force]
"""
import base64
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "tx"
CONFIG_PATH = os.path.join(ROOT, "config", "tx.json")
GEO_PATH = os.path.join(ROOT, "assets", "tx-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
API = "https://goelect.txelections.civixapps.com/api-ivis-system/api/v1/getFile?"
PAGE = "https://goelect.txelections.civixapps.com/ivis-evr-ui/evr"


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _int(v):
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def get_file(**params):
    j = json.loads(C.http_get(API + urllib.parse.urlencode(params), no_cache=True, retries=2, timeout=60))
    up = j.get("upload")
    return json.loads(base64.b64decode(up).decode("utf-8").strip()) if up else None


def find_election(cfg):
    want = [t.upper() for t in cfg["source"].get("election_match", ["2026 NOVEMBER GENERAL"])]
    day = datetime.strptime(cfg["election"]["date"], "%Y-%m-%d").strftime("%m/%d/%Y")
    for e in (get_file(type="EVR_ELECTION") or {}).get("elections", []):
        if e.get("election_date") == day and all(t in (e.get("election_name") or "").upper() for t in want):
            return e
    return None


def blk(t):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(t), "rep_pct": None,
            "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {})
    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}
    prev = load(LATEST_PATH, {}) or {}
    if not force and prev.get("statewide", {}).get("cast", {}).get("total") and C.checked_recently(DATA_DIR, "sos", minutes=25):
        print("tx: checked under 25 minutes ago -- skipping")
        return 0

    elec = find_election(cfg)
    if not elec:
        print("tx: the %s general isn't in the SoS turnout app yet" % cfg["election"]["date"])
        return 0
    today = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%m/%d/%Y")   # Texas (Central) date
    dates = [d["date"] for d in elec.get("early_voting_dates", [])]
    ask = max([d for d in dates if datetime.strptime(d, "%m/%d/%Y") <= datetime.strptime(today, "%m/%d/%Y")] or dates[:1])
    rep = get_file(type="EVR_EARLYVOTING", electionId=elec["id"], electionDate=ask)
    rows = (rep or {}).get("turnout_by_county") or []
    if len(rows) != len(gidx):
        print("tx: %d county rows (expected %d) -- not used" % (len(rows), len(gidx)), file=sys.stderr)
        return 0
    counties, unmatched, t = {}, [], {"ip": 0, "mail": 0, "reg": 0}
    for r in rows:
        g = gidx.get(_norm(r.get("name")))
        if not g:
            unmatched.append(r.get("name"))
            continue
        ip, mail, reg = _int(r.get("total_in_person_votes_for_election")), _int(r.get("total_mail_votes_for_election")), _int(r.get("registered_voters"))
        cast = ip + mail
        t["ip"] += ip; t["mail"] += mail; t["reg"] += reg
        ent = {"fips": g["fips"], "cast": blk(cast), "registered": reg,
               "turnout_pct": round(100.0 * cast / reg, 2) if reg else None}
        if ip:
            ent["early_voted"] = blk(ip)
        if mail:
            ent["mail_voted"] = blk(mail)
        counties[g["name"]] = ent
    if unmatched:
        print("tx: county names not on the map: %s -- not used" % unmatched[:8], file=sys.stderr)
        return 0
    cast = t["ip"] + t["mail"]
    statewide = {"cast": blk(cast), "registered": t["reg"], "turnout_pct": round(100.0 * cast / t["reg"], 2) if t["reg"] else None}
    if t["ip"]:
        statewide["early_voted"] = blk(t["ip"])
    if t["mail"]:
        statewide["mail_voted"] = blk(t["mail"])
    import lab_standin as LAB
    bad = LAB.suspicious_drop(prev, cast)
    if bad:
        print("tx: %s" % bad, file=sys.stderr)
        return 0
    as_of = rep.get("date_updated") or ""
    body = {
        "state": STATE, "state_name": cfg.get("state_name", "Texas"), "election": cfg.get("election", {}),
        "partisan": False, "methods_present": [m for m in ("early_voted", "mail_voted") if statewide.get(m)],
        "method_labels": cfg.get("method_labels", {}), "statewide": statewide, "counties": counties,
    }
    h = C.data_hash(body)
    changed = force or h != prev.get("data_hash")
    now = C.utc_now_iso()
    snap = dict(body, source={"primary": "Texas Secretary of State, Election Information & Turnout Data (unofficial early voting "
                                         "turnout by county, as submitted by county election officials)",
                              "url": PAGE, "election": elec.get("election_name"), "as_of": as_of},
                source_compiled=as_of, source_compiled_iso=(as_of + "T00:00:00") if as_of else now,
                data_hash=h, generated_at=now if changed else prev.get("generated_at", now))
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    if changed:
        with open(HISTORY_PATH, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"generated_at": now, "cast": cast, "registered": t["reg"], "data_hash": h},
                               separators=(",", ":")) + "\n")
    print("%s  %s as of %s | counties=%d cast=%d (in person %d, mail %d) turnout=%s%%"
          % ("CHANGED" if changed else "NOCHANGE", elec.get("election_name"), as_of, len(counties), cast, t["ip"], t["mail"],
             statewide["turnout_pct"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
