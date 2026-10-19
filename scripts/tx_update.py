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

Two more layers sit on top of the SoS figures:
  * counties' own live pages (in-person early votes through the day): ElectionLink
    widgets (found by probing every county) and Dallas County's vote-center map
    (an ArcGIS layer with each site's votes; config live_arcgis).
  * party by primary history (config party_history): theoelections.com publishes,
    per county, the early voters on the counties' rosters split by the last party
    primary each voted in (2020-2026). Used unaltered, with attribution, for the
    all-ballots figures only (it isn't split by voting method). Ballots the rosters
    don't include yet are counted as party not reported; where the rosters count
    more voters than the state's own report (e.g. Bexar's mail ballots), the extra
    voters are added to that county's total (on top of any live in-person count).

Run:  python scripts/tx_update.py [--force]
"""
import base64
import gzip
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


def read_arcgis(name, src):
    """A county vote-center map's feature layer (Dallas): each site's running total of
    in-person early votes -> {"early", "eday", "provisional", "as_of", "url", "source"} or None.
    Election Day rows aren't read yet (their fields' meaning on Nov 3 is unverified)."""
    lay = src["layer"].rstrip("/")
    tf, df = src["total_field"], src.get("daily_field", src["total_field"])
    try:
        meta = json.loads(C.http_get(lay + "?f=json", no_cache=True, retries=1, timeout=30))
        q = json.loads(C.http_get(lay + "/query?" + urllib.parse.urlencode({
            "where": "1=1", "outFields": "%s,%s,%s" % (src["type_field"], tf, df),
            "returnGeometry": "false", "resultRecordCount": 5000, "f": "json"}), no_cache=True, retries=1, timeout=30))
    except Exception as e:  # noqa: BLE001
        print("tx: %s vote-center map unavailable (%s)" % (name, str(e)[:100]), file=sys.stderr)
        return None
    rows = [f.get("attributes") or {} for f in q.get("features") or []]
    ev = [r for r in rows if r.get(src["type_field"]) == src.get("ev", "EV")]
    if q.get("exceededTransferLimit") or len(ev) < src.get("min_sites", 10):
        print("tx: %s vote-center map: %d early-voting sites -- not used" % (name, len(ev)), file=sys.stderr)
        return None
    if any(not isinstance(r.get(tf), int) or r[tf] < 0 or (isinstance(r.get(df), int) and r[df] > r[tf]) for r in ev):
        print("tx: %s vote-center map: a site's running total is missing or below its daily count -- not used" % name,
              file=sys.stderr)
        return None
    edit = ((meta.get("editingInfo") or {}).get("dataLastEditDate") or 0) / 1000
    when = datetime.fromtimestamp(edit, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if edit else ""
    return {"early": sum(r[tf] for r in ev), "eday": 0, "provisional": 0, "as_of": when,
            "url": src.get("page", lay), "source": src.get("label", "county vote-center map"), "sites": len(ev)}


PARTY_CACHE = os.path.join(DATA_DIR, "_party_history.json")


def party_history(cfg, elec_id, force=False):
    """theoelections.com's early voters by last party primary, per county (FIPS without 48)
    -> {"as_of", "url", "page", "counties": {fips3: {"voters","dem","rep","both","none","as_of"}}} or None.
    Kept in data/tx/_party_history.json; refetched (If-None-Match) every party_history.every_minutes,
    and the last good copy is kept if a fetch fails."""
    import urllib.error
    import urllib.request
    ph = cfg.get("party_history") or {}
    if not ph.get("url") or not elec_id:
        return None
    url = ph["url"].format(id=elec_id)
    cache = load(PARTY_CACHE, {}) or {}
    if cache.get("url") != url:
        cache = {}
    now = datetime.now(timezone.utc)
    age = (now - datetime.fromisoformat(cache["checked"])).total_seconds() / 60 if cache.get("checked") else 1e9
    if cache.get("counties") and age < ph.get("every_minutes", 30) and not force:
        return cache

    def save(c):
        c["checked"] = now.isoformat(timespec="seconds")
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(PARTY_CACHE, "w", encoding="utf-8", newline="\n") as f:
            json.dump(c, f, indent=0, separators=(",", ":"))
        return c
    hdr = {"User-Agent": C.USER_AGENT}
    if cache.get("etag") and cache.get("counties"):
        hdr["If-None-Match"] = cache["etag"]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=90, context=C._SSL_CTX) as r:
            raw, etag = r.read(), r.headers.get("ETag", "")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return save(cache)
        print("tx: party history (theoelections) HTTP %s -- %s"
              % (e.code, "keeping the last copy" if cache.get("counties") else "none yet"), file=sys.stderr)
        return cache or None
    except Exception as e:  # noqa: BLE001
        print("tx: party history (theoelections) unavailable (%s)" % str(e)[:100], file=sys.stderr)
        return cache or None
    try:
        d = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
        assert str(d.get("id")) == str(elec_id), "election id %s" % d.get("id")
        assert {"dem", "rep", "none"} <= {r.get("k") for r in d.get("rows") or []}, "rows"
        picks, units = d.get("picks") or [], (d.get("units") or {}).get("county") or {}
        year = cfg["election"]["date"][:4]
        out, latest = {}, ""
        for f3, c in units.items():
            v = c.get("voters") or []
            idx = max([i for i, x in enumerate(v) if x is not None] or [-1])
            if idx < 0 or not v[idx]:
                continue
            g = {k: (c.get(k) or [None] * len(v))[idx] for k in ("dem", "rep", "both", "none")}
            if any(g[k] is None for k in ("dem", "rep", "none")) or sum(x or 0 for x in g.values()) != v[idx]:
                continue   # the parts don't add up: skip the county rather than guess
            md = (picks[idx] if idx < len(picks) else "").split("/")
            when = "%s-%02d-%02d" % (year, int(md[0]), int(md[1])) if len(md) == 2 and md[0].isdigit() and md[1].isdigit() else ""
            out[f3.zfill(3)] = {"voters": v[idx], "dem": g["dem"], "rep": g["rep"], "both": g["both"] or 0,
                                "none": g["none"], "as_of": when}
            latest = max(latest, when)
        assert len(out) >= 5, "only %d counties" % len(out)
    except Exception as e:  # noqa: BLE001
        print("tx: party history (theoelections) unreadable (%s) -- %s"
              % (str(e)[:100], "keeping the last copy" if cache.get("counties") else "not used"), file=sys.stderr)
        return cache or None
    return save({"url": url, "page": ph.get("page", ""), "etag": etag, "as_of": latest, "counties": out})


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
        elec = {"election_name": (prev.get("source") or {}).get("election"),
                "id": (prev.get("source") or {}).get("election_id") or cfg["source"].get("election_id")}
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
    readers = {n: (lambda n=n: read_live(n, eday))
               for n in live_counties(sorted(counties), eday, force="--rediscover" in sys.argv)}
    for n, src in (cfg.get("live_arcgis") or {}).items():
        if not n.startswith("_"):
            readers[n] = (lambda n=n, src=src: read_arcgis(n, src))
    for name in sorted(readers):
        lv = readers[name]()
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
        ent["live"] = {"source": lv.get("source", "county turnout page (ElectionLink)"), "url": lv["url"], "as_of": lv["as_of"],
                       "in_person": lv["early"], "sos_in_person": sos_ip}
        live_used.append(name)
        live_latest = max(live_latest, lv["as_of"])
    if live_used:
        print("tx: live county pages used for %d counties: %s" % (len(live_used), ", ".join(live_used)))
    cast = t["ip"] + t["mail"] + t.get("eday", 0)
    # party by primary history (theoelections), on the all-ballots figures
    ph = party_history(cfg, elec.get("id") or (prev.get("source") or {}).get("election_id") or cfg["source"].get("election_id"),
                       force=force)
    party_used, roster_ahead, ph_known = [], [], 0
    if ph and ph.get("counties"):
        for name, ent in counties.items():
            p = ph["counties"].get(ent["fips"][2:])
            tot = ent["cast"]["total"]
            if not p:
                ent["cast"] = C.party_block(0, 0, 0, 0, unk=tot)
                continue
            # the rosters ahead of the state's own count (mail + in-person, before any live page):
            # those extra voters are added (a live page only replaces the state's in-person figure)
            sos = (ent.get("mail_voted") or {}).get("total", 0) + \
                (ent["live"]["sos_in_person"] if ent.get("live") else (ent.get("early_voted") or {}).get("total", 0))
            if p["voters"] > sos:
                roster_ahead.append((name, p["voters"], sos))
                cast += p["voters"] - sos
                tot += p["voters"] - sos
            # "both" (voted in both parties' primaries; none so far) counts with no party
            ent["cast"] = C.party_block(p["rep"], p["dem"], 0, p["none"] + p.get("both", 0), unk=tot - p["voters"])
            ent["turnout_pct"] = round(100.0 * tot / ent["registered"], 2) if ent.get("registered") else None
            ent["party_history"] = {"voters": p["voters"], "as_of": p["as_of"]}
            party_used.append(name)
            ph_known += p["voters"]
    if party_used:
        print("tx: party by primary history (theoelections) for %d counties, %d voters; roster counts ahead of the SoS in %d: %s"
              % (len(party_used), ph_known, len(roster_ahead),
                 ", ".join("%s %d>%d" % r for r in sorted(roster_ahead, key=lambda r: -r[1])[:6])))
        for c in counties.values():
            c["cast"].pop("compiled", None)
            c["cast"].pop("compiled_iso", None)
    statewide = {"cast": C.add_blocks(*[c["cast"] for c in counties.values()]) if party_used else blk(cast),
                 "registered": t["reg"], "turnout_pct": round(100.0 * cast / t["reg"], 2) if t["reg"] else None}
    if party_used:
        statewide["cast"].pop("compiled", None)
        statewide["cast"].pop("compiled_iso", None)
        if statewide["cast"]["total"] != cast:
            print("tx: county party blocks sum to %d, not %d -- not used" % (statewide["cast"]["total"], cast), file=sys.stderr)
            return 1
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
        "partisan": bool(party_used), "methods_present": [m for m in ("early_voted", "mail_voted", "election_day") if statewide.get(m)],
        "method_labels": cfg.get("method_labels", {}), "statewide": statewide, "counties": counties,
    }
    notes = []
    if live_used:
        notes.append("In-person early votes for %s come from the county's own live turnout page (updated through the day). "
                     "Every other county's come from the Secretary of State's daily report, which adds each day's "
                     "in-person votes the next morning, so during the day those counties lag behind."
                     % (", ".join(live_used) if len(live_used) < 4 else "%d counties (%s)" % (len(live_used), ", ".join(live_used))))
    if party_used:
        body.update({
            "party_methods": ["cast"],
            "party_basis": "the last party primary each voter voted in (2020-2026)",
            "party_methods_note": "Party (the last party primary each voter voted in) is shown for all ballots combined; "
                                  "it isn't split by voting method, so this view is turnout only.",
            "party_hints": {"cast": "Everyone who has voted so far, by the last party primary they voted in (2020-2026), "
                                    "as matched by theoelections.com from the counties' early-voting rosters. "
                                    "No party = no primary since 2020, including new voters."},
            "unk_label": "counted, but not yet on the rosters matched to primary history",
            "party_labels": {"rep": "Republican primary voters", "dem": "Democratic primary voters",
                             "npa": "No primary since 2020"},
        })
        notes.append("Party is the last party primary each voter voted in (2020-2026), from theoelections.com's match of "
                     "the counties' early-voting rosters to primary voting history (%d counties, %s voters as of %s); "
                     "ballots not on those rosters yet, such as today's in-person votes, count as party not reported."
                     % (len(party_used), format(ph_known, ","), ph.get("as_of") or "?")
                     + (" Where the rosters count more voters than the state's report, the extra voters are added: %s."
                        % ", ".join("%s %s (state %s)" % (n, format(v, ","), format(s_, ","))
                                    for n, v, s_ in sorted(roster_ahead, key=lambda r: -r[1])[:8])
                        if roster_ahead else ""))
    if notes:
        body["map_note"] = " ".join(notes)
    h = C.data_hash(body)
    changed = force or h != prev.get("data_hash")
    now = C.utc_now_iso()
    snap = dict(body, source={"primary": "Texas Secretary of State, Election Information & Turnout Data (unofficial early voting "
                                         "turnout by county, as submitted by county election officials)",
                              "url": PAGE, "election": elec.get("election_name"), "as_of": as_of,
                              "election_id": elec.get("id"), "live_counties": live_used, "live_as_of": live_latest,
                              **({"party_history": {"label": "theoelections.com (early voters by last party primary)",
                                                    "url": ph.get("page") or ph.get("url"), "as_of": ph.get("as_of"),
                                                    "counties": len(party_used), "voters": ph_known}} if party_used else {}),
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
