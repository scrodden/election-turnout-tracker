#!/usr/bin/env python3
"""Official election-night results feeds, one parser per state.

There is no free national results feed, so each state's own feed is read here:
    parse_<usps>() -> {race_id: {"candidates": [{name, party, votes}], "reporting_pct": float or None}}
    measures_<usps>() -> {measure_id: {"yes": int, "no": int, "reporting": float or None}}
    county_<usps>() -> {county: {"sen" or "gov": {rep, dem, other}}}
registered in SOURCES / MEASURES / COUNTIES. A state missing from SOURCES has no
readable feed: the page marks it "No live feed" and links its official results
(config/results_links.json).

Feeds carry test data before the election (California's API on 10/31: every race
exactly 2-to-1), so results_update.py reads a state only after POLLS_CLOSE. Nothing
here can call a race: state feeds report votes, not winners.

Race ids: sen-<ST>, sen-<ST>-special, gov-<ST>, house-<ST>-<NN or AL>.
"""
import json
import os
import re
import unicodedata
from datetime import datetime, timezone

import common as C

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# When each state's first polls close on Nov 3, 2026, in UTC (Eastern is UTC-5 by then).
_T = {
    "2026-11-03T23:00:00Z": "IN KY",                                       # 6:00 pm ET
    "2026-11-04T00:00:00Z": "FL GA NH SC VA VT",                           # 7:00
    "2026-11-04T00:30:00Z": "NC OH WV",                                    # 7:30
    "2026-11-04T01:00:00Z": "AL CT DE IL KS MA MD ME MI MO MS ND NJ OK PA RI SD TN TX",   # 8:00
    "2026-11-04T01:30:00Z": "AR",                                          # 8:30
    "2026-11-04T02:00:00Z": "AZ CO IA LA MN NE NM NY WI WY",               # 9:00
    "2026-11-04T03:00:00Z": "ID MT NV UT",                                 # 10:00
    "2026-11-04T04:00:00Z": "CA OR WA",                                    # 11:00
    "2026-11-04T05:00:00Z": "AK HI",                                       # midnight
}
POLLS_CLOSE = {st: t for t, sts in _T.items() for st in sts.split()}


def polls_closed(usps, now=None):
    """False until the state's first polls close: what a feed shows before then is test data."""
    now = now or datetime.now(timezone.utc)
    t = datetime.strptime(POLLS_CLOSE[usps], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return now >= t


# ---- shared helpers ---------------------------------------------------------
def party(p):
    """A feed's party label -> DEM / REP / a code that can't be read as either (the page goes by first letter)."""
    s = re.sub(r"[^a-z]", "", str(p or "").lower())
    if s in ("d", "dfl", "dem") or s.startswith(("democrat", "democratic")):
        return "DEM"
    if s in ("r", "rep", "gop") or s.startswith("republican"):
        return "REP"
    if s.startswith(("lib", "l")) and s in ("l", "lib", "libertarian", "lbt"):
        return "LIB"
    if s in ("g", "gp", "gre", "grn", "green"):
        return "GRN"
    if s in ("i", "ind", "independent", "npa", "np", "una", "unaffiliated", "non", "noparty", "nonpartisan"):
        return "IND"
    return "OTH"


def num(v):
    """'218,636' -> 218636; blank or junk -> 0."""
    s = re.sub(r"[^\d.]", "", str(v or ""))
    try:
        return int(float(s)) if s else 0
    except ValueError:
        return 0


def share(a, b):
    """Precincts reporting as a percent, or None if the feed gives no total."""
    a, b = num(a), num(b)
    return round(100.0 * a / b, 1) if b else None


def race(cands, reporting):
    """One race for results_update, or None while it has no votes (feeds list every race at 0 before results)."""
    out = [{"name": n, "party": p, "votes": v} for n, p, v in cands]
    return {"candidates": out, "reporting_pct": reporting} if sum(c["votes"] for c in out) else None


def house_id(usps, n):
    """house-<ST>-<NN>, or -AL for a state's single seat (n = 0 or 1 there)."""
    n = int(n)
    if usps in AT_LARGE:
        return "house-%s-AL" % usps
    return "house-%s-%02d" % (usps, n)


AT_LARGE = {"AK", "DE", "ND", "SD", "VT", "WY"}


def _measures_for(usps):
    with open(os.path.join(ROOT, "config", "measures.json"), encoding="utf-8") as f:
        return [m for m in json.load(f).get("measures", []) if m.get("state") == usps]


def _surname(name):
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", " ", s.replace(",", " "))
    toks = re.findall(r"[a-z'-]+", s)
    return toks[-1] if toks else ""


def names_match(feed_cands, ballot):
    """Does a feed's race have someone from the 2026 ballot? Guards against a feed still showing another election
    or a race mapped to the wrong seat. No ballot list -> nothing to check."""
    if not ballot:
        return True
    want = {_surname(b.get("name", "")) for b in ballot} - {""}
    if not want:
        return True
    text = " ".join(str(c.get("name", "")) for c in feed_cands)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    have = set(re.findall(r"[a-z'-]+", text))
    return bool(want & have)


# ---- North Carolina ---------------------------------------------------------
# The State Board's own results-site data. Its 2022 file matches the certified counts exactly (Budd 1,905,786, NC-1
# Davis 134,996), with real precinct counts. Don't total the precinct zip with "Real Precinct" == Y: that drops every
# absentee and early vote counted outside regular precincts (Budd 1,533,344; NC-1's wrong candidate ahead).
NC_URL = "https://er.ncsbe.gov/enr/20261103/data/results_0.txt"
NC_MEASURES = {"PROPERTY TAX LEVY LIMIT": "Property Tax Levy Limit Amendment",
               "MAXIMUM INCOME TAX RATE": "Reduce Income Tax Rate Cap from 7% to 3.5% Amendment",
               "REQUIRE PHOTO ID": "Require Voter Identification Amendment"}


def _nc_rows():
    return json.loads(C.http_get(NC_URL, no_cache=True, timeout=60))


def parse_NC():
    groups = {}
    for r in _nc_rows():
        name = r.get("cnm", "")
        m = re.match(r"US HOUSE OF REPRESENTATIVES DISTRICT (\d+)", name)
        rid = house_id("NC", m.group(1)) if m else ("sen-NC" if name.startswith("US SENATE") else None)
        if rid:
            groups.setdefault(rid, []).append(r)
    out = {}
    for rid, rows in groups.items():
        got = race([(r["bnm"], party(r.get("pty")), num(r.get("vct"))) for r in rows],
                   share(rows[0].get("prt"), rows[0].get("ptl")))
        if got:
            out[rid] = got
    return out


def measures_NC():
    ids = {m["title"]: m["id"] for m in _measures_for("NC")}
    out = {}
    for r in _nc_rows():
        name = r.get("cnm", "")
        if not name.startswith("CONSTITUTIONAL AMENDMENT"):
            continue
        title = next((t for k, t in NC_MEASURES.items() if k in name), None)
        if title not in ids:
            continue
        e = out.setdefault(ids[title], {"yes": 0, "no": 0, "reporting": share(r.get("prt"), r.get("ptl"))})
        side = "yes" if str(r.get("bnm", "")).strip().upper().startswith(("YES", "FOR")) else "no"
        e[side] += num(r.get("vct"))
    return {k: v for k, v in out.items() if v["yes"] + v["no"]}


# ---- Minnesota --------------------------------------------------------------
# The Secretary of State's media files: state;county;precinct;office id;office;district;candidate id;name;suffix;
# incumbent;party;precincts reporting;precincts;votes;percent;total votes. Its results site is behind a captcha;
# these files aren't, and they're what the office publishes for newsrooms.
MN_BASE = "https://electionresultsfiles.sos.mn.gov/20261103/"


def _mn(fname):
    return [ln.split(";") for ln in C.http_get(MN_BASE + fname, no_cache=True, timeout=45).splitlines() if ln.count(";") >= 15]


def _mn_race(rows):
    return race([(f[7].split(" and ")[0].strip(), party(f[10]), num(f[13])) for f in rows], share(rows[0][11], rows[0][12]))


def parse_MN():
    out = {}
    sen, gov = _mn("ussenate.txt"), _mn("governor.txt")
    if sen and _mn_race(sen):
        out["sen-MN"] = _mn_race(sen)
    if gov and _mn_race(gov):
        out["gov-MN"] = _mn_race(gov)
    groups = {}
    for f in _mn("ushouse.txt"):
        groups.setdefault(num(f[5]), []).append(f)
    for n, rows in groups.items():
        got = _mn_race(rows)
        if got:
            out[house_id("MN", n)] = got
    return out


def measures_MN():
    rows = _mn("constamend.txt")
    ids = [m["id"] for m in _measures_for("MN")]
    yes = sum(num(f[13]) for f in rows if f[7].upper() == "YES")
    no = sum(num(f[13]) for f in rows if f[7].upper() == "NO")
    return {ids[0]: {"yes": yes, "no": no, "reporting": share(rows[0][11], rows[0][12])}} if ids and yes + no else {}


def county_MN():
    """Minnesota's county numbers are its FIPS codes' order: Hennepin 053 = 27, Ramsey 123 = 62 (not name order: McLeod = 43)."""
    with open(os.path.join(ROOT, "assets", "mn-counties.geojson"), encoding="utf-8") as f:
        names = {"%02d" % ((int(ft["properties"]["fips"][-3:]) + 1) // 2): ft["properties"]["name"]
                 for ft in json.load(f)["features"]}
    out = {}
    for f in _mn("ussenatecty.txt"):
        c = names.get(f[1])
        if c:
            e = out.setdefault(c, {}).setdefault("sen", {"rep": 0, "dem": 0, "other": 0})
            p = party(f[10])
            e["rep" if p == "REP" else "dem" if p == "DEM" else "other"] += num(f[13])
    return {c: v for c, v in out.items() if sum(v["sen"].values())}


# ---- California -------------------------------------------------------------
# The Secretary of State's results API (api.sos.ca.gov/returns/...).
CA_BASE = "https://api.sos.ca.gov/returns/"


def _ca_reporting(s):
    m = re.match(r"\s*([\d.]+)%", str(s or ""))
    return float(m.group(1)) if m else None


def _ca_race(d):
    return race([(c.get("Name", ""), party(c.get("Party")), num(c.get("Votes"))) for c in d.get("candidates", [])],
                _ca_reporting(d.get("Reporting")))


def parse_CA():
    out = {}
    got = _ca_race(json.loads(C.http_get(CA_BASE + "governor", no_cache=True, timeout=45)))
    if got:
        out["gov-CA"] = got
    for d in json.loads(C.http_get(CA_BASE + "us-rep/district/all", no_cache=True, timeout=45)):
        m = re.search(r"District (\d+)", d.get("raceTitle", ""))
        got = _ca_race(d) if m else None
        if got:
            out[house_id("CA", m.group(1))] = got
    return out


def measures_CA():
    d = json.loads(C.http_get(CA_BASE + "ballot-measures", no_cache=True, timeout=45))
    ids = {re.sub(r"\D", "", m["title"]): m["id"] for m in _measures_for("CA")}
    rep = _ca_reporting(d.get("Reporting"))
    out = {}
    for m in d.get("ballot-measures", []):
        mid = ids.get(str(int(num(m.get("Number")))))
        if mid and num(m.get("yesVotes")) + num(m.get("noVotes")):
            out[mid] = {"yes": num(m.get("yesVotes")), "no": num(m.get("noVotes")), "reporting": rep}
    return out


# ---- registries -------------------------------------------------------------
SOURCES = {"NC": parse_NC, "MN": parse_MN, "CA": parse_CA}
MEASURES = {"NC": measures_NC, "CA": measures_CA, "MN": measures_MN}
COUNTIES = {"MN": county_MN}
