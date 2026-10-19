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


# ---- counties' own live turnout pages (intra-day; the SoS figures above are daily) ----
# ElectionLink widgets: updates.electionlink.net/widgets/<county>tx/2026-11-03/BallotTypeTurnout.html,
# refreshed every few minutes. Its "Early Vote" = in-person early-vote check-ins (mail ballots
# aren't in it), plus Election Day and provisional counts. Which counties have one is found by
# trying every county's address about once a day (data/tx/_live_counties.json).
EL_URL = "https://updates.electionlink.net/widgets/{slug}tx/{date}/BallotTypeTurnout.html"
LIVE_LIST = os.path.join(DATA_DIR, "_live_counties.json")


def _slug(name):
    return re.sub(r"[^a-z]", "", name.lower())


def discover_live(county_names, eday):
    """-> sorted list of counties with an ElectionLink widget for this election."""
    import urllib.error
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    def probe(n):
        u = EL_URL.format(slug=_slug(n), date=eday)
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": C.USER_AGENT}), timeout=20,
                                        context=C._SSL_CTX) as r:
                return n if r.status == 200 else None
        except Exception:  # noqa: BLE001 - 404: no widget
            return None
    with ThreadPoolExecutor(8) as ex:
        return sorted(n for n in ex.map(probe, county_names) if n)


def live_counties(county_names, eday, force=False):
    cache = load(LIVE_LIST, {}) or {}
    fresh = cache.get("checked") and (datetime.now(timezone.utc) - datetime.fromisoformat(cache["checked"])).total_seconds() < 86400
    if force or not fresh or cache.get("election") != eday:
        found = discover_live(county_names, eday)
        if found or not cache.get("counties"):
            cache = {"election": eday, "checked": datetime.now(timezone.utc).isoformat(timespec="seconds"), "counties": found}
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(LIVE_LIST, "w", encoding="utf-8", newline="\n") as f:
                json.dump(cache, f, indent=1)
    return cache.get("counties") or []


def read_live(name, eday):
    """-> {"early": n, "eday": n, "provisional": n, "as_of": iso, "url"} or None."""
    u = EL_URL.format(slug=_slug(name), date=eday)
    try:
        h = C.http_get(u, no_cache=True, retries=1, timeout=30)
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"arrayToDataTable\(\[\['Ballot',\s*'Count'\],(.*?)\]\)", h, re.S)
    if not m:
        return None
    vals = {k.strip().lower(): int(v) for k, v in re.findall(r"\['([^']+)',\s*(\d+)\]", m.group(1))}
    ts = re.search(r"var lastRefreshed = ([\d.]+)", h)
    when = datetime.fromtimestamp(float(ts.group(1)) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else ""
    if "early vote" not in vals:
        return None
    return {"early": vals["early vote"], "eday": vals.get("election day", 0),
            "provisional": vals.get("provisional", vals.get("provisional application", vals.get("affidavit", 0))),
            "as_of": when, "url": u.replace("BallotTypeTurnout.html", "index.html")}


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {})
    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}
    prev = load(LATEST_PATH, {}) or {}
    if not force and prev.get("statewide", {}).get("cast", {}).get("total") and C.checked_recently(DATA_DIR, "sos", minutes=10):
        print("tx: checked under 10 minutes ago -- skipping")
        return 0

    sos_down = None
    try:
        elec = find_election(cfg)
        if not elec:
            print("tx: the %s general isn't in the SoS turnout app yet" % cfg["election"]["date"])
            return 0
        today = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%m/%d/%Y")   # Texas (Central) date
        dates = [d["date"] for d in elec.get("early_voting_dates", [])]
        ask = max([d for d in dates if datetime.strptime(d, "%m/%d/%Y") <= datetime.strptime(today, "%m/%d/%Y")] or dates[:1])
        rep = get_file(type="EVR_EARLYVOTING", electionId=elec["id"], electionDate=ask)
        rows = (rep or {}).get("turnout_by_county") or []
    except Exception as e:  # noqa: BLE001 - the SoS app was 502 under first-day load: rebuild its last figures from our file
        if not prev.get("counties"):
            raise
        sos_down = str(e)[:120]
        print("tx: SoS turnout app unavailable (%s) -- using its last figures; live county pages still update" % sos_down)
        elec = {"election_name": (prev.get("source") or {}).get("election")}
        rep = {"date_updated": (prev.get("source") or {}).get("as_of", "")}
        rows = []
        for n, c in prev["counties"].items():
            ip = (c.get("live") or {}).get("sos_in_person", (c.get("early_voted") or {}).get("total", 0))
            rows.append({"name": n, "registered_voters": c.get("registered", 0), "total_in_person_votes_for_election": ip,
                         "total_mail_votes_for_election": (c.get("mail_voted") or {}).get("total", 0)})
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
    # counties' own live pages: in-person early votes (and Election Day) more current than the SoS's daily figures
    eday = cfg["election"]["date"]
    live_used, live_latest = [], ""
    for name in live_counties(sorted(counties), eday, force="--rediscover" in sys.argv):
        lv = read_live(name, eday)
        ent = counties.get(name)
        if not lv or not ent:
            continue
        sos_ip = (ent.get("early_voted") or {}).get("total", 0)
        if lv["early"] <= sos_ip and not lv["eday"]:
            continue   # the SoS figure is as current
        mail = (ent.get("mail_voted") or {}).get("total", 0)
        t["ip"] += lv["early"] - sos_ip
        ent["early_voted"] = blk(max(lv["early"], sos_ip))
        if lv["eday"]:
            ent["election_day"] = blk(lv["eday"])
            t["eday"] = t.get("eday", 0) + lv["eday"]
        cast_c = ent["early_voted"]["total"] + mail + lv["eday"]
        ent["cast"] = blk(cast_c)
        ent["turnout_pct"] = round(100.0 * cast_c / ent["registered"], 2) if ent.get("registered") else None
        ent["live"] = {"source": "county turnout page (ElectionLink)", "url": lv["url"], "as_of": lv["as_of"],
                       "in_person": lv["early"], "sos_in_person": sos_ip}
        live_used.append(name)
        live_latest = max(live_latest, lv["as_of"])
    if live_used:
        print("tx: live county pages used for %d counties: %s" % (len(live_used), ", ".join(live_used)))
    cast = t["ip"] + t["mail"] + t.get("eday", 0)
    statewide = {"cast": blk(cast), "registered": t["reg"], "turnout_pct": round(100.0 * cast / t["reg"], 2) if t["reg"] else None}
    if t["ip"]:
        statewide["early_voted"] = blk(t["ip"])
    if t["mail"]:
        statewide["mail_voted"] = blk(t["mail"])
    if t.get("eday"):
        statewide["election_day"] = blk(t["eday"])
    import lab_standin as LAB
    bad = LAB.suspicious_drop(prev, cast)
    if bad:
        print("tx: %s" % bad, file=sys.stderr)
        return 0
    as_of = rep.get("date_updated") or ""
    body = {
        "state": STATE, "state_name": cfg.get("state_name", "Texas"), "election": cfg.get("election", {}),
        "partisan": False, "methods_present": [m for m in ("early_voted", "mail_voted", "election_day") if statewide.get(m)],
        "method_labels": cfg.get("method_labels", {}), "statewide": statewide, "counties": counties,
    }
    if live_used:
        body["map_note"] = ("In-person early votes for %s come from the county's own live turnout page (updated through the day). "
                            "Every other county's come from the Secretary of State's daily report, which adds each day's "
                            "in-person votes the next morning, so during the day those counties lag behind."
                            % (", ".join(live_used) if len(live_used) < 4 else "%d counties (%s)" % (len(live_used), ", ".join(live_used))))
    h = C.data_hash(body)
    changed = force or h != prev.get("data_hash")
    now = C.utc_now_iso()
    snap = dict(body, source={"primary": "Texas Secretary of State, Election Information & Turnout Data (unofficial early voting "
                                         "turnout by county, as submitted by county election officials)",
                              "url": PAGE, "election": elec.get("election_name"), "as_of": as_of,
                              "live_counties": live_used, "live_as_of": live_latest,
                              **({"sos_unavailable": sos_down} if sos_down else {})},
                source_compiled=as_of, source_compiled_iso=live_latest or ((as_of + "T00:00:00") if as_of else now),
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
