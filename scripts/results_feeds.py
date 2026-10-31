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
import base64
import gzip
import html
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


KEEP_ZERO = False   # results_feeds_check.py: list races before they have votes, to check a feed covers the whole ballot


def race(cands, reporting):
    """One race for results_update, or None while it has no votes (feeds list every race at 0 before results)."""
    # a page's "Totals" row isn't a candidate (it would double every count)
    out = [{"name": n, "party": p, "votes": v} for n, p, v in cands if n and not re.match(r"(?i)totals?", n)]
    return {"candidates": out, "reporting_pct": reporting} if (KEEP_ZERO or sum(c["votes"] for c in out)) else None


def nice(name):
    """'GREG ABBOTT (I)' -> 'Greg Abbott'; a running mate after ' and ' or '/' is dropped."""
    n = re.sub(r"\s*\((?:I|INC|INCUMBENT)\)\s*$", "", html.unescape(str(name or "")).strip(), flags=re.I)
    n = re.split(r"\s+and\s+|\s*/\s*|\s*&\s*", n, flags=re.I)[0].strip()
    if n.isupper():
        n = re.sub(r"\bMc([a-z])", lambda m: "Mc" + m.group(1).upper(), n.title())
    return n


def text_of(h):
    """HTML -> plain text with single spaces."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h))).strip()


SPECIAL_SENATE = {"FL", "OH"}   # the slate's ids: sen-FL-special, sen-OH-special


def race_id(usps, title):
    """A feed's office title -> our race id, or None if it isn't a U.S. Senate, Governor or U.S. House race."""
    t = re.sub(r"[^A-Z0-9 ]", " ", str(title).upper())
    t = " ".join(t.split())
    t = re.sub(r"\bU S\b", "US", t)
    if re.search(r"\b(US|UNITED STATES)\b.*\bSENAT", t):
        return "sen-%s%s" % (usps, "-special" if usps in SPECIAL_SENATE else "")
    if re.match(r"^(STATE OF [A-Z]+ )?GOVERNOR\b", t):
        return "gov-" + usps
    if re.search(r"\b(US|UNITED STATES)\b.*\b(REP|REPRESENTATIVE|REPRESENTATIVES|HOUSE|CONGRESS)\b|REPRESENTATIVE IN CONGRESS|"
                 r"CONGRESSIONAL DISTRICT|MEMBER HOUSE OF REPRESENTATIVES|MEMBER OF CONGRESS", t):
        if usps in AT_LARGE or "AT LARGE" in t:
            return "house-%s-AL" % usps
        m = re.search(r"\b(\d+)(?:ST|ND|RD|TH)?\b", t)
        return house_id(usps, m.group(1)) if m else None
    return None


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


def ballot_names(cands, ballot):
    """Prefer the ballot list's name where a feed gives only a surname ('Begich') or all capitals."""
    by_surname = {_surname(b.get("name", "")): b.get("name") for b in ballot or []}
    for c in cands:
        n = str(c.get("name", ""))
        if (" " not in n or n.isupper() or n != n.title() and n.upper() == n) and by_surname.get(_surname(n)):
            c["name"] = by_surname[_surname(n)]
    return cands


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
    return {k: v for k, v in out.items() if v["yes"] + v["no"] or KEEP_ZERO}


# ---- Minnesota --------------------------------------------------------------
# The Secretary of State's media files: state;county;precinct;office id;office;district;candidate id;name;suffix;
# incumbent;party;precincts reporting;precincts;votes;percent;total votes. Its results site is behind a captcha;
# these files aren't, and they're what the office publishes for newsrooms.
MN_BASE = "https://electionresultsfiles.sos.mn.gov/20261103/"


def _mn(fname):
    return [ln.split(";") for ln in C.http_get(MN_BASE + fname, no_cache=True, timeout=45).splitlines() if ln.count(";") >= 15]


def _mn_race(rows):
    return race([(nice(f[7]), party(f[10]), num(f[13])) for f in rows], share(rows[0][11], rows[0][12]))


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
    return {ids[0]: {"yes": yes, "no": no, "reporting": share(rows[0][11], rows[0][12])}} if ids and (yes + no or KEEP_ZERO) else {}


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
        if mid and (num(m.get("yesVotes")) + num(m.get("noVotes")) or KEEP_ZERO):
            out[mid] = {"yes": num(m.get("yesVotes")), "no": num(m.get("noVotes")), "reporting": rep}
    return out


# ---- Enhanced Voting states: Georgia, Washington, Virginia, Utah -------------
# The same results platform and JSON: /results/public/api/jurisdictions/<name> lists elections;
# /results/public/api/elections/<name>/<election id>/data has every contest.
EV_SITES = {"GA": ("https://results.sos.ga.gov", "Georgia"), "WA": ("https://results.votewa.gov", "washington"),
            "VA": ("https://enr.elections.virginia.gov", "Virginia"), "UT": ("https://electionresults.utah.gov", "Utah"),
            "ID": ("https://results.voteidaho.gov", "Idaho")}


def _en(items):
    return next((x.get("text", "") for x in items or [] if x.get("languageId") == "en"), "")


def _ev_items(usps):
    dom, jur = EV_SITES[usps]
    j = json.loads(C.http_get("%s/results/public/api/jurisdictions/%s" % (dom, jur), no_cache=True, timeout=60))
    today = [e for e in j.get("elections", []) if e.get("electionDate") == "2026-11-03"]
    if not today:
        raise RuntimeError("no Nov 3 election listed yet")
    e = next((x for x in today if "general" in _en(x.get("name")).lower()), today[0])
    d = json.loads(C.http_get("%s/results/public/api/elections/%s/%s/data" % (dom, jur, e["publicElectionId"]), no_cache=True, timeout=90))
    return d.get("ballotItems") or []


def _ev_reporting(b):
    st = b.get("reportingStatus") or {}
    return share(st.get("reportingUnits"), st.get("totalUnits"))


def _ev_options(b):
    return (b.get("summaryResults") or {}).get("ballotOptions") or []


def _ev_parse(usps):
    out = {}
    for b in _ev_items(usps):
        rid = race_id(usps, _en(b.get("name"))) if b.get("contestType") == "Candidate" else None
        if not rid:
            continue
        got = race([(nice(_en(o.get("name"))), party((o.get("party") or {}).get("abbreviation")), num(o.get("voteCount")))
                    for o in _ev_options(b)], _ev_reporting(b))
        if got:
            out[rid] = got
    return out


EV_MEASURE_KEYS = {   # a statewide measure's name in the feed -> its title in config/measures.json
    "VA": (r"Question (\d+)", "Question %s"), "WA": (r"No\. I[LP]\d+-0*(\d+)", "Initiative %s"),
    "GA": (r"Amendment\s*(?:No\.?\s*|#\s*)?(\d+)", "Amendment %s"), "UT": (r"Amendment\s+([A-Z])\b", "Constitutional Amendment %s"),
    "ID": (r"\b(HJR\s*\d+|Proposition\s*\d+)", "%s"),
}


def _ev_measures(usps):
    ids = {m["title"]: m["id"] for m in _measures_for(usps)}
    pat, title = EV_MEASURE_KEYS[usps]
    out = {}
    for b in _ev_items(usps):
        m = re.search(pat, _en(b.get("name")))   # by name: GA's 2024 measures were typed "Candidate"
        mid = ids.get(title % m.group(1)) if m else None
        if not mid:
            continue
        v = {_en(o.get("name")).strip().lower(): num(o.get("voteCount")) for o in _ev_options(b)}
        yes = next((n for k, n in v.items() if k.startswith(("yes", "for"))), 0)
        no = next((n for k, n in v.items() if k.startswith(("no", "against"))), 0)
        if yes + no or KEEP_ZERO:
            out[mid] = {"yes": yes, "no": no, "reporting": _ev_reporting(b)}
    return out


def parse_GA(): return _ev_parse("GA")
def parse_WA(): return _ev_parse("WA")
def parse_VA(): return _ev_parse("VA")
def parse_UT(): return _ev_parse("UT")
def parse_ID(): return _ev_parse("ID")
def measures_GA(): return _ev_measures("GA")
def measures_WA(): return _ev_measures("WA")
def measures_VA(): return _ev_measures("VA")
def measures_UT(): return _ev_measures("UT")
def measures_ID(): return _ev_measures("ID")


# ---- Pennsylvania -----------------------------------------------------------
# The Department of State's official RSS feed ("Results by Office"): one item per race, one table row per county and
# candidate. The channel title gives districts reporting, and says "Testing" while it carries test data (10/30).
PA_FEED = "https://www.electionreturns.pa.gov/electionFeed.aspx?ID=83"
PA_SLOT = {}   # one fetch per run, shared by parse_PA and county_PA


def _pa_items():
    if not PA_SLOT:
        s = C.http_get(PA_FEED, no_cache=True, timeout=90)
        title = (re.search(r"<title>(.*?)</title>", s, re.S) or [None, ""])[1]
        if "testing" in title.lower():
            raise RuntimeError("feed is still marked Testing")
        m = re.search(r"\(([\d.]+)%\)", title)
        PA_SLOT["reporting"] = float(m.group(1)) if m else None
        PA_SLOT["items"] = re.findall(r"<item>\s*<title>(.*?)</title>\s*<description><!\[CDATA\[(.*?)\]\]>", s, re.S)
    return PA_SLOT["items"]


def _pa_rows(body):
    """[(county, candidate, party, votes)] from one item's table; columns: county, name, party, ED, mail, provisional, total, %."""
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S)[1:]:
        c = [text_of(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(c) >= 7:
            out.append((c[0], c[1], c[2], num(c[6])))
    return out


def parse_PA():
    out = {}
    items = _pa_items()
    for title, body in items:
        rid = race_id("PA", html.unescape(title))
        if not rid:
            continue
        cands = {}
        for _county, name, p, v in _pa_rows(body):
            e = cands.setdefault(name, [nice(name), party(p), 0])
            e[2] += v
        got = race([tuple(e) for e in cands.values()], PA_SLOT.get("reporting"))
        if got:
            out[rid] = got
    return out


# ---- Texas ------------------------------------------------------------------
# The Secretary of State's results data behind results.texas-election.com: base64 JSON per office group. Its 2026
# general is election 53815. Reporting is polling locations statewide (the feed has no per-race figure).
TX_URL = "https://goelect.txelections.civixapps.com/api-ivis-system/api/s3/enr/election/53815"


def parse_TX():
    d = json.loads(C.http_get(TX_URL, no_cache=True, timeout=120))
    def part(k):
        return json.loads(base64.b64decode(d[k])) if d.get(k) else {}
    home = part("Home")
    if str(home.get("ElecDate")) != "11032026":
        raise RuntimeError("not the Nov 3 election: %s" % home.get("ElecDate"))
    pl = home.get("PollingReporting") or {}
    rep_pct = share(pl.get("PLR"), pl.get("PLT"))
    out = {}
    for group in ("Federal", "StateWide"):
        for r in part(group).get("Races") or []:
            rid = race_id("TX", r.get("N", ""))
            got = race([(nice(c.get("N")), party(c.get("P")), num(c.get("V"))) for c in r.get("Candidates") or []], rep_pct) if rid else None
            if got:
                out[rid] = got
    return out


# ---- Florida ----------------------------------------------------------------
# Florida Election Watch, the Division of Elections' results site: server-rendered pages per office. Reporting comes
# from its County Reporting Status page (each county's precincts reporting, weighted by registered voters).
FL_BASE = "https://floridaelectionwatch.gov/"


def _fl_sections(page):
    """[(contest title, [(name, party, votes)])] from an office page."""
    s = C.http_get(FL_BASE + page, no_cache=True, timeout=60)
    parts = re.split(r'class="btn-block titleDescription"[^>]*>', s)
    out = []
    for part in parts[1:]:
        title = text_of(part[:part.find("</div>")])
        rows = []
        for tr in re.findall(r'<tr class="grid-row"[^>]*>(.*?)</tr>', part, re.S):
            c = [text_of(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(c) >= 3:
                rows.append((nice(c[0]), party(c[1]), num(c[2])))
        out.append((title, rows))
    return out


def _fl_reporting():
    s = C.http_get(FL_BASE + "CountyReportingStatus", no_cache=True, timeout=60)
    weight = done = 0
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", s, re.S):
        c = [text_of(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        # county, (website link), turnout, registered, turnout %, precincts reporting %
        pcts = [i for i, x in enumerate(c) if x.endswith("%")]
        if len(pcts) >= 2 and pcts[0] >= 2:
            reg = num(c[pcts[0] - 1])
            weight += reg
            done += reg * float(c[pcts[1]].rstrip("%") or 0)
    return round(done / weight, 1) if weight else None


def parse_FL():
    rep_pct = _fl_reporting()
    out = {}
    for page in ("FederalOffices/USSenator", "StateOffices/Governor", "FederalOffices/USRepresentative"):
        for title, rows in _fl_sections(page):
            rid = race_id("FL", "US Senator" if page.endswith("USSenator") else title)
            got = race(rows, rep_pct) if rid and rows else None
            if got:
                out[rid] = got
    return out


def measures_FL():
    t = text_of(C.http_get(FL_BASE + "Amendments", no_cache=True, timeout=60))
    ids = {m["title"]: m["id"] for m in _measures_for("FL")}
    rep_pct = _fl_reporting()
    out = {}
    for n, yes, no in re.findall(r"Amendment No\. (\d+)\b.*?Yes for Approval ([\d,]+).*?No for Rejection ([\d,]+)", t):
        mid = ids.get("Amendment " + n)
        if mid and (num(yes) + num(no) or KEEP_ZERO):
            out[mid] = {"yes": num(yes), "no": num(no), "reporting": rep_pct}
    return out


# ---- Louisiana --------------------------------------------------------------
# The Secretary of State's Live Election Results data: race list and votes as JSON, per election date.
LA_BASE = "https://voterportal.sos.la.gov/ElectionResults/ElectionResults/Data?blob=20261103/"


def _la_races():
    names = json.loads(C.http_get(LA_BASE + "RacesCandidates_Multiparish.htm", timeout=60))["Races"]["Race"]
    votes = {r["ID"]: r for r in json.loads(C.http_get(LA_BASE + "Votes_Multiparish.htm", no_cache=True, timeout=60))["Races"]["Race"]}
    return [(r, votes.get(r["ID"]) or {}) for r in names]


def _la_vote_map(v):
    return {c["ID"]: num(c.get("VoteTotal")) for c in v.get("Choice") or []}


def parse_LA():
    out = {}
    for r, v in _la_races():
        rid = race_id("LA", r.get("SpecificTitle", ""))
        if not rid:
            continue
        cands = []
        got_votes = _la_vote_map(v)
        for c in r.get("Choice") or []:
            m = re.match(r"(.*?)\s*\(([A-Za-z]+)\)\s*$", c.get("Desc", ""))
            name, p = (m.group(1), m.group(2)) if m else (c.get("Desc", ""), "")
            cands.append((nice(name.replace('"', "")), party(p), got_votes.get(c["ID"], 0)))
        got = race(cands, share(v.get("PrecinctsReporting"), v.get("PrecinctsExpected")))
        if got:
            out[rid] = got
    return out


def measures_LA():
    ids = {m["title"]: m["id"] for m in _measures_for("LA")}
    out = {}
    for r, v in _la_races():
        m = re.match(r"CA No\.\s*(\d+)", r.get("SpecificTitle", ""))
        mid = ids.get("Amendment " + m.group(1)) if m else None
        if not mid:
            continue
        vm = _la_vote_map(v)
        tot = {str(c.get("Desc", "")).strip().upper()[:3]: vm.get(c["ID"], 0) for c in r.get("Choice") or []}
        yes, no = tot.get("YES", 0), tot.get("NO", 0)
        if yes + no or KEEP_ZERO:
            out[mid] = {"yes": yes, "no": no, "reporting": share(v.get("PrecinctsReporting"), v.get("PrecinctsExpected"))}
    return out


# ---- Indiana ----------------------------------------------------------------
# The Election Division's results site data: one JSON per office category (1005 = U.S. Representative).
IN_BASE = "https://enr.indianavoters.in.gov/site/data/"


def _in_list(x):
    return x if isinstance(x, list) else [x] if x else []


def _in_reporting():
    d = json.loads(C.http_get(IN_BASE + "statewideElectionsC_A.json", no_cache=True, timeout=45))["Root"].get("JurisdictionsReporting") or {}
    return share(d.get("TOTAL_REPORTEDPRECINCTS") or d.get("PRECINCTS_REPORTED") or d.get("TOTAL_PRECINCTSREPORTED") or 0, d.get("TOTAL_ALLPRECINCTS"))


def parse_IN():
    settings = json.loads(C.http_get(IN_BASE + "settings.json", no_cache=True, timeout=45))["Root"]
    if settings.get("CurrentElection") != "11/03/2026":
        raise RuntimeError("not the Nov 3 election: %s" % settings.get("CurrentElection"))
    d = json.loads(C.http_get(IN_BASE + "OffCatC_1005_A.json", no_cache=True, timeout=45))["Root"]
    order = {"First": 1, "Second": 2, "Third": 3, "Fourth": 4, "Fifth": 5, "Sixth": 6, "Seventh": 7, "Eighth": 8, "Ninth": 9}
    rep_pct = _in_reporting()
    out = {}
    for r in _in_list(d["StatewideSummary"]["Race"]):
        m = re.search(r",\s*(\w+) District", r.get("OFFICE_TITLE", ""))
        if not m or m.group(1) not in order:
            continue
        got = race([(nice(c.get("NAME_ON_BALLOT")), party(c.get("PARTY")), num(c.get("TOTAL"))) for c in _in_list((r.get("Candidates") or {}).get("Candidate"))], rep_pct)
        if got:
            out[house_id("IN", order[m.group(1)])] = got
    return out


# ---- Connecticut ------------------------------------------------------------
# The Secretary of the State's Election Center data. A candidate can run on several party lines (fusion): those votes
# are added together, under the major party's line when there is one. Reporting is polling places statewide.
CT_BASE = "https://ctemspublic.tgstg.net/ng-app/data/election/108/"


def _ct_base():
    ver = json.loads(C.http_get(CT_BASE + "Version.json", no_cache=True, timeout=30))["Version"]
    return "%s%s/" % (CT_BASE, ver)


def parse_CT():
    base = _ct_base()
    look = json.loads(C.http_get(base + "Lookupdata.json", timeout=60))
    if look.get("election", {}).get("DT") != "11/03/2026":
        raise RuntimeError("not the Nov 3 election")
    votes = json.loads(C.http_get(base + "stateVotes_Electiondata.json", no_cache=True, timeout=60))
    info = json.loads(C.http_get(base + "election_Electiondata.json", no_cache=True, timeout=30))
    m = re.search(r"([\d.]+)%", str(info.get("PR", "")))
    rep_pct = float(m.group(1)) if m else None
    out = {}
    for o in look.get("officeList", []):
        off = next(iter(o.values()))
        rid = race_id("CT", off.get("NM", ""))
        if not rid:
            continue
        cands = {}
        for row in votes.get(str(off["ID"])) or []:
            cid, v = next(iter(row.items()))
            c = look["candidateIds"].get(cid) or {}
            line = party((look["partyIds"].get(str(c.get("P"))) or {}).get("CD"))
            e = cands.setdefault(nice(c.get("NM", "")), [nice(c.get("NM", "")), line, 0])
            if line in ("DEM", "REP") and e[1] not in ("DEM", "REP"):
                e[1] = line
            e[2] += num(v.get("V"))
        got = race([tuple(e) for e in cands.values()], rep_pct)
        if got:
            out[rid] = got
    return out


# ---- Delaware ---------------------------------------------------------------
DE_URL = "https://elections.delaware.gov/results/enr/Election_StatewideResults_ID_GE2026.json"


def parse_DE():
    groups = {}
    for r in json.loads(C.http_get(DE_URL, no_cache=True, timeout=45)):
        rid = race_id("DE", r.get("Contest Title", ""))
        if rid:
            groups.setdefault(rid, []).append(r)
    out = {}
    for rid, rows in groups.items():
        got = race([(nice(r.get("Candidate Name")), party(r.get("Party Name")), num(r.get("Total Votes"))) for r in rows],
                   share(rows[0].get("Precincts Reported"), rows[0].get("Total Precincts")))
        if got:
            out[rid] = got
    return out


# ---- Maryland ---------------------------------------------------------------
# The State Board's results pages: Governor, then one page per congressional district; "NR" until reported.
MD_BASE = "https://elections.maryland.gov/elections/2026/general_results/"


def _md_page(page):
    s = C.http_get(MD_BASE + page, no_cache=True, timeout=45)
    m = re.search(r"\(([\d,]+) of ([\d,]+) election day precincts", s)
    table = s[s.find("table-banded"):]
    table = table[:table.find("</table>")]
    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", table, re.S)[1:]:
        c = [text_of(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(c) >= 7:
            rows.append((c[0], c[1], num(c[6])))
    return rows, (share(m.group(1), m.group(2)) if m else None)


def parse_MD():
    out = {}
    for rid, page in [("gov-MD", "gen_results_2026_1.html")] + [(house_id("MD", n), "gen_results_2026_4_%d.html" % n) for n in range(1, 9)]:
        rows, rep_pct = _md_page(page)
        got = race([(nice(re.sub(r"\s*\(Write-In\)", "", n)), party(p), v) for n, p, v in rows], rep_pct)
        if got:
            out[rid] = got
    return out


def measures_MD():
    ids = {m["title"]: m["id"] for m in _measures_for("MD")}
    out = {}
    for n in (1, 2, 3):
        rows, rep_pct = _md_page("gen_qresults_2026_%d.html" % n)
        tot = {name.strip().lower()[:3]: v for name, _p, v in rows if not name.lower().startswith("total")}
        yes = next((v for k, v in tot.items() if k.startswith(("for", "yes"))), 0)
        no = next((v for k, v in tot.items() if k.startswith(("aga", "no"))), 0)
        mid = ids.get("Question %d" % n)
        if mid and (yes + no or KEEP_ZERO):
            out[mid] = {"yes": yes, "no": no, "reporting": rep_pct}
    return out


# ---- Vermont ----------------------------------------------------------------
# The Secretary of State's static results: an index, then a time-stamped file for federal and for statewide offices,
# with votes by town. Summed here.
VT_BASE = "https://static.electionresults.vermont.gov/"
VT_ELECTION = "68d61e96-1d28-4210-8c1d-8e90512ae105"


def parse_VT():
    idx = json.loads(C.http_get(VT_BASE + "elections/%s.json" % VT_ELECTION, no_cache=True, timeout=45))
    if (idx.get("electionDetails") or {}).get("electionDate", "")[:10] != "2026-11-03":
        raise RuntimeError("not the Nov 3 election")
    rep_pct = idx.get("townReportingPercent")
    out = {}
    for key in ("federal", "stateWide"):
        d = json.loads(C.http_get(VT_BASE + idx[key]["path"].replace("\\", "/"), timeout=60))
        for group in d.get("d") or []:
            for o in group.get("o") or []:
                rid = race_id("VT", {"REPRESENTATIVE TO CONGRESS": "US Representative"}.get(o.get("on"), o.get("on", "")))
                if not rid:
                    continue
                cands = {}
                for town in o.get("cs") or []:
                    for c in town.get("rc") or []:
                        e = cands.setdefault(c.get("cn"), [nice(c.get("cn")), party(c.get("pn")), 0])
                        e[2] += num(c.get("vc"))
                got = race([tuple(e) for e in cands.values()], rep_pct)
                if got:
                    out[rid] = got
    return out


# ---- New Mexico -------------------------------------------------------------
# The Secretary of State's results site, server-rendered: type=FED (U.S. Senate, House), type=SW (Governor and other
# statewide offices), type=SWX (measures).
NM_BASE = "https://electionresults.sos.nm.gov/resultsSW.aspx?map=CTY&type="


def _nm_sections(kind):
    s = C.http_get(NM_BASE + kind, no_cache=True, timeout=60)
    m = re.search(r'hidElectionDate[^>]*value="([^"]+)"', s)
    if not m or m.group(1) != "11/3/2026":
        raise RuntimeError("not the Nov 3 election: %s" % (m.group(1) if m else "no date"))
    out = []
    for part in s.split('display-results-box-a">')[1:]:
        title = text_of(part[:part.find("</h1>")])
        extra = re.search(r"DISTRICT\s+\d+", text_of(part[:1500]))
        pr = re.search(r"Precincts Fully:\s*([\d,]+)\s*/\s*([\d,]+)", text_of(part[:6000]))
        rows = []
        for m2 in re.finditer(r'display-results-box-d">\s*<h1>(.*?)</h1>\s*<h2[^>]*>(.*?)</h2>.*?display-results-box-f">\s*<h1>(.*?)</h1>', part, re.S):
            rows.append((text_of(m2.group(1)), text_of(m2.group(2)), num(text_of(m2.group(3)))))
        out.append((title, extra.group(0) if extra else "", share(pr.group(1), pr.group(2)) if pr else None, rows))
    return out


def parse_NM():
    out = {}
    for kind in ("FED", "SW"):
        for title, extra, rep_pct, rows in _nm_sections(kind):
            rid = race_id("NM", (title + " " + extra).strip())
            got = race([(nice(n), party(p), v) for n, p, v in rows], rep_pct) if rid else None
            if got:
                out[rid] = got
    return out


# ---- Nebraska, South Dakota, North Dakota (and Oregon): one vendor's results site ----
# Server-rendered pages per office group. North Dakota's still showed 2024 on 10/31, so every page's hidElectionDate is
# checked; the parser was tested on that 2024 page against its figures. Nebraska's and South Dakota's pages list no
# contests until results start ("Testing In Progress"), and Oregon's site is offline until election night.
VENDOR = {
    "NE": ("https://electionresults.nebraska.gov/resultsSW.aspx?", ["text=Race&type=SW&map=CTY", "text=Race&type=CG&map=DIST"], ["text=Race&type=PA&map=CTY"]),
    "SD": ("https://electionresults.sd.gov/resultsSW.aspx?", ["text=Race&type=SWR&map=CTY", "text=Race&type=FED&map=CTY"], ["text=Race&type=BQ&map=CTY"]),
    "ND": ("https://results.sos.nd.gov/ResultsSW.aspx?", ["text=All&type=SW&map=CTY"], ["text=BQ&type=SW&map=CTY"]),
    "OR": ("https://results.oregonvotes.gov/ResultsSW.aspx?", ["text=Race&type=FED&map=CTY", "text=Race&type=SW&map=CTY"], []),
}
VENDOR_DATE = "11/3/2026"


def _vendor_sections(url, date=None):
    """[(contest title, reporting %, [(name, party, votes)])] from one results page."""
    s = C.http_get(url, no_cache=True, timeout=60)
    m = re.search(r'hidElectionDate[^>]*value="([^"]+)"', s)
    if not m or m.group(1) != (date or VENDOR_DATE):
        raise RuntimeError("not the Nov 3 election: %s" % (m.group(1) if m else "no date on the page"))
    parts = s.split('id="ContestGraphCands')
    out = []
    for i, part in enumerate(parts[1:]):
        head = text_of(parts[i][-9000:])
        ms = list(re.finditer(r"([^|]{3,160}?)\s+Vote For \d+\s+Precincts Fully:\s*([\d,]+)\s*/\s*([\d,]+)", head))
        if not ms:
            continue
        title = re.split(r"/>|\bTop\s|Export Page\s", ms[-1].group(1))[-1].strip()
        rows = [(text_of(a), text_of(b), num(text_of(c))) for a, b, c in re.findall(
            r'display-results-box-d"[^>]*>\s*<h2>(.*?)</h2>.*?display-results-box-e">\s*<div class="[^"]*"\s*>(.*?)</div>.*?display-results-box-g"\s*>(.*?)</h2>', part, re.S)]
        out.append((title, share(ms[-1].group(2), ms[-1].group(3)), rows))
    return out


def _vendor_parse(usps, date=None):
    base, pages, _m = VENDOR[usps]
    out = {}
    for pg in pages:
        for title, rep_pct, rows in _vendor_sections(base + pg, date):
            rid = race_id(usps, title)
            got = race([(nice(n), party(p), v) for n, p, v in rows], rep_pct) if rid else None
            if got:
                out[rid] = got
    return out


def _vendor_measures(usps, date=None):
    base, _p, pages = VENDOR[usps]
    wanted = {re.sub(r"[^a-z0-9]", "", m["title"].lower()): m["id"] for m in _measures_for(usps)}
    out = {}
    for pg in pages:
        for title, rep_pct, rows in _vendor_sections(base + pg, date):
            key = re.sub(r"[^a-z0-9]", "", title.lower())
            mid = next((v for k, v in wanted.items() if k in key), None)
            if mid:
                yes = sum(v for n, _x, v in rows if n.strip().lower().startswith(("yes", "for")))
                no = sum(v for n, _x, v in rows if n.strip().lower().startswith(("no", "against")))
                if yes + no or KEEP_ZERO:
                    out[mid] = {"yes": yes, "no": no, "reporting": rep_pct}
    return out


def parse_NE(): return _vendor_parse("NE")
def parse_SD(): return _vendor_parse("SD")
def parse_ND(): return _vendor_parse("ND")
def parse_OR(): return _vendor_parse("OR")
def measures_NE(): return _vendor_measures("NE")
def measures_SD(): return _vendor_measures("SD")
def measures_ND(): return _vendor_measures("ND")


# ---- Alaska -----------------------------------------------------------------
# The Division of Elections' map data. It still held the August primary on 10/31, so the election name is checked.
# Ranked-choice: these are first-choice votes; any later rounds are tabulated by the Division after the night.
AK_URL = "https://www.elections.alaska.gov/enr26/results/statewide.js"


def _ak_cand(s):
    m = re.match(r"(.*?)\s*\(([A-Za-z/]+)\)\s*$", s)   # 'Kreiss-Tomkins/Johnson (DEM/NON)': the ticket's lead candidate
    name, p = (m.group(1), m.group(2).split("/")[0]) if m else (s, "")
    name = re.split(r"\s*/\s*", name)[0]
    if "," in name:
        last, first = name.split(",", 1)
        name = first.strip() + " " + last.strip()
    return name.replace('"', ""), party(p)


def _ak_data(check=True):
    d = json.loads(C.http_get(AK_URL, no_cache=True, timeout=60))
    if check and not ("General" in d.get("name", "") and "2026" in d.get("name", "")):
        raise RuntimeError("not the general yet: %s" % d.get("name"))
    return d


def parse_AK(check=True):
    d = _ak_data(check)
    out = {}
    for t in d.get("totals") or []:
        rid = race_id("AK", d["contestNames"][t["n"]])
        if not rid:
            continue
        cands = []
        for c in t.get("c") or []:
            n, p = _ak_cand(d["candidateNames"][c["n"]])
            cands.append((n, p, num(c.get("t"))))
        got = race(cands, share(t.get("r"), t.get("p")))
        if got:
            out[rid] = got
    return out


def measures_AK(check=True):
    d = _ak_data(check)
    ids = {m["title"]: m["id"] for m in _measures_for("AK")}
    out = {}
    for t in d.get("totals") or []:
        m = re.match(r"BM#\s*(\d+)", d["contestNames"][t["n"]])
        mid = ids.get("Ballot Measure " + m.group(1)) if m else None
        if not mid:
            continue
        v = {d["candidateNames"][c["n"]].strip().upper(): num(c.get("t")) for c in t.get("c") or []}
        if v.get("YES", 0) + v.get("NO", 0) or KEEP_ZERO:
            out[mid] = {"yes": v.get("YES", 0), "no": v.get("NO", 0), "reporting": share(t.get("r"), t.get("p"))}
    return out


# ---- Rhode Island -----------------------------------------------------------
# RI.gov's results site posts each election under its year; the 2026 pages aren't up yet, and the parser was tested on
# the 2024 pages' figures.
RI_BASE = "https://www.ri.gov/election/results/%s/general_election/"


def _ri_sections(year="2026"):
    s = C.http_get(RI_BASE % year, no_cache=True, timeout=60)
    if "%s General Election" % year not in s:
        raise RuntimeError("no %s general election page" % year)
    out = []
    for part in s.split("<h3>")[1:]:
        title = text_of(part[:part.find("</h3>")])
        rows = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", part[:part.find("</table>")], re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            total = re.search(r'data-title="Total votes"[^>]*>([\d,]+)', tr)
            if tds and total:
                rows.append((text_of(tds[0]), num(total.group(1))))
        out.append((title, rows))
    return out


def parse_RI(year="2026"):
    out = {}
    for title, rows in _ri_sections(year):
        rid = race_id("RI", title)
        if not rid:
            continue
        cands = []
        for n, v in rows:   # 'DEM Gabriel Amo (DEM)'
            m = re.match(r"(.*?)\s*\((\w+)\)\s*$", n)
            name, p = (m.group(1), m.group(2)) if m else (n, "")
            if p and name.lower().startswith(p.lower() + " "):
                name = name[len(p) + 1:]
            cands.append((nice(name), party(p), v))
        got = race(cands, None)
        if got:
            out[rid] = got
    return out


def measures_RI(year="2026"):
    ids = {m["title"]: m["id"] for m in _measures_for("RI")}
    out = {}
    for title, rows in _ri_sections(year):
        m = re.search(r"Question\s*(?:No\.?\s*)?(\d+)", title)
        mid = ids.get("Question " + m.group(1)) if m else None
        if mid:
            yes = sum(v for n, v in rows if n.strip().lower().startswith(("yes", "approve")))
            no = sum(v for n, v in rows if n.strip().lower().startswith(("no", "reject")))
            if yes + no or KEEP_ZERO:
                out[mid] = {"yes": yes, "no": no, "reporting": None}
    return out


# ---- Clarity states read from Sam's PC: Iowa, Colorado --------------------------------
# Elections hosted on Clarity Elections answer a home connection but not GitHub's runners (an empty HTTP 202 on 10/31),
# so scripts/results_local.py reads them on the PC and pushes data/results/local/<ST>.json, which the workflow reads.
# Without the PC those states keep their last figures, then show "No live feed".
CLARITY = {"IA": "https://electionresults.iowa.gov/IA/", "CO": "https://results.enr.clarityelections.com/CO/"}
LOCAL_DIR = os.path.join(ROOT, "data", "results", "local")
LOCAL_STALE_MIN = 45


def _clarity_get(url):
    b = C.http_get(url, no_cache=True, timeout=45, binary=True)
    return (gzip.decompress(b) if b[:2] == b"\x1f\x8b" else b).decode("utf-8", "replace")


def clarity_contests(usps):
    base = CLARITY[usps]
    today = [e for e in json.loads(_clarity_get(base + "elections.json")) if str(e.get("Date", "")).startswith("11/3/2026")]
    if not today:
        raise RuntimeError("no Nov 3 election listed yet")
    eid = today[0]["EID"]
    ver = _clarity_get("%s%s/current_ver.txt" % (base, eid)).strip()
    return json.loads(_clarity_get("%s%s/%s/json/en/summary.json" % (base, eid, ver)))


def clarity_parse(usps, contests=None):
    """-> (races, measures) in the same shape as the other feeds."""
    wanted = {re.sub(r"[^a-z0-9]", "", m["title"].lower()): m["id"] for m in _measures_for(usps)}
    races, measures = {}, {}
    for c in contests if contests is not None else clarity_contests(usps):
        title = c.get("C", "")
        rep_pct = share(c.get("PR"), c.get("TP"))
        names, parties, votes = c.get("CH") or [], c.get("P") or [], c.get("V") or []
        rid = race_id(usps, title)
        if rid:
            got = race([(nice(n), party(parties[i] if i < len(parties) else ""), num(votes[i] if i < len(votes) else 0)) for i, n in enumerate(names)], rep_pct)
            if got:
                races[rid] = got
            continue
        key = re.sub(r"[^a-z0-9]", "", title.lower())
        mid = next((v for k, v in wanted.items() if k in key), None)
        if mid:
            tot = {str(n).strip().lower(): num(votes[i] if i < len(votes) else 0) for i, n in enumerate(names)}
            yes = next((v for k, v in tot.items() if k.startswith(("yes", "for"))), 0)
            no = next((v for k, v in tot.items() if k.startswith(("no", "against"))), 0)
            if yes + no or KEEP_ZERO:
                measures[mid] = {"yes": yes, "no": no, "reporting": rep_pct}
    return races, measures


def _local(usps, key):
    """What the PC reader last wrote for a state. A stale file is a failed feed (the last published figures stay)."""
    try:
        with open(os.path.join(LOCAL_DIR, usps + ".json"), encoding="utf-8") as f:
            d = json.load(f)
    except OSError:
        raise RuntimeError("the PC reader hasn't written this state yet")
    age = (datetime.now(timezone.utc) - datetime.strptime(d["fetched"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds() / 60
    if age > LOCAL_STALE_MIN:
        raise RuntimeError("the PC reader's last read is %d minutes old" % age)
    return d.get(key) or {}


def parse_IA(): return _local("IA", "races")
def parse_CO(): return _local("CO", "races")
def measures_IA(): return _local("IA", "measures")
def measures_CO(): return _local("CO", "measures")


# ---- registries -------------------------------------------------------------
SOURCES = {"NC": parse_NC, "MN": parse_MN, "CA": parse_CA, "GA": parse_GA, "WA": parse_WA, "VA": parse_VA, "UT": parse_UT,
           "PA": parse_PA, "TX": parse_TX, "FL": parse_FL, "LA": parse_LA, "IN": parse_IN, "CT": parse_CT, "DE": parse_DE,
           "MD": parse_MD, "VT": parse_VT, "NM": parse_NM, "ID": parse_ID, "NE": parse_NE, "SD": parse_SD, "ND": parse_ND, "OR": parse_OR,
           "AK": parse_AK, "RI": parse_RI, "IA": parse_IA, "CO": parse_CO}
MEASURES = {"NC": measures_NC, "CA": measures_CA, "MN": measures_MN, "GA": measures_GA, "WA": measures_WA, "VA": measures_VA,
            "UT": measures_UT, "FL": measures_FL, "LA": measures_LA, "MD": measures_MD, "ID": measures_ID, "NE": measures_NE, "SD": measures_SD,
            "ND": measures_ND, "AK": measures_AK, "RI": measures_RI, "IA": measures_IA, "CO": measures_CO}
COUNTIES = {"MN": county_MN}
