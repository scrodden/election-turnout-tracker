#!/usr/bin/env python3
"""Build data/registration.json — statewide voter REGISTRATION by party for each
party-registration state, the denominator for the Registration-vs-Turnout page.

Registration is published year-round, so each state's numbers are wired from its
official registration-statistics source (config/registration_sources.json). A
per-state parser goes in SOURCES[code] and returns {rep,dem,npa,oth,as_of}; states
without a wired parser are omitted (no fabrication). Idempotent; run each cycle.

To wire a state: implement parse_<code>() -> {"rep":int,"dem":int,"npa":int,
"oth":int,"as_of":"YYYY-MM-DD"} using its source, register in SOURCES.

Run:  python scripts/registration_update.py
"""
import os
import re
import sys
import json
import urllib.request
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402
STATES_PATH = os.path.join(ROOT, "assets", "states.json")
SRC_PATH = os.path.join(ROOT, "config", "registration_sources.json")
OUT_PATH = os.path.join(ROOT, "data", "registration.json")


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_days(iso):
    from datetime import datetime, timezone
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return 1e9


def _num(s):
    return int(str(s).replace(",", "").strip() or 0)


def _as_of(text):
    m = re.search(r"as of ([A-Za-z]+ \d+, \d{4})", text)
    if not m:
        return ""
    from datetime import datetime
    try:
        return datetime.strptime(m.group(1), "%B %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return m.group(1)


def parse_fl():
    """FL DOS 'Voter Registration - By County and Party' — an HTML table on the
    page (County | Republican | Democratic | Minor | No Party Affiliation | Total).
    Sum the county rows to statewide. Minor -> oth, No Party Affiliation -> npa."""
    url = ("https://dos.fl.gov/elections/data-statistics/voter-registration-statistics/"
           "voter-registration-reports/voter-registration-by-county-and-party/")
    html = C.http_get(url, no_cache=True)
    rows = re.findall(r"<td>([A-Z][^<]*)</td>\s*<td>([\d,]+)</td>\s*<td>([\d,]+)</td>\s*"
                      r"<td>([\d,]+)</td>\s*<td>([\d,]+)</td>\s*<td>([\d,]+)</td>", html)
    rep = dem = minor = npa = 0
    for _name, r, d, mnr, n, _tot in rows:
        rep += _num(r); dem += _num(d); minor += _num(mnr); npa += _num(n)
    if rep + dem + npa <= 0:
        raise RuntimeError("FL: no rows parsed")
    return {"rep": rep, "dem": dem, "npa": npa, "oth": minor, "as_of": _as_of(html)}


def parse_pa():
    """PA DOS 'currentvotestats.xlsx' (stable URL) — 'Reg Voter' sheet, county rows
    with columns Dem / Rep / No Aff / Other / Total. Sum to statewide."""
    url = ("https://www.pa.gov/content/dam/copapwp-pagov/en/dos/resources/voting-and-elections/"
           "voting-and-election-statistics/currentvotestats.xlsx")
    sheets = C.read_xlsx(C.http_get(url, binary=True, no_cache=True))
    rows = sheets.get("Reg Voter") or (list(sheets.values())[0] if sheets else [])
    as_of, hdr = "", None
    for r in rows[:6]:
        joined = " ".join(str(x) for x in r)
        m = re.search(r"as of (\d{1,2}/\d{1,2}/\d{4})", joined, re.I)
        if m:
            from datetime import datetime
            try:
                as_of = datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
            except ValueError:
                as_of = m.group(1)
        if any(str(x).strip() == "CountyName" for x in r):
            hdr = r
    if not hdr:
        raise RuntimeError("PA: header row not found")

    def ci(name):
        for i, x in enumerate(hdr):
            if str(x).strip().lower() == name.lower():
                return i
        return None
    i_name, i_d, i_r, i_n, i_o = ci("CountyName"), ci("Dem"), ci("Rep"), ci("No Aff"), ci("Other")
    dem = rep = npa = oth = 0
    for r in rows:
        if i_name is None or i_name >= len(r):
            continue
        nm = str(r[i_name]).strip()
        if not nm or nm == "CountyName" or nm.lower().startswith("total"):
            continue

        def num(i):
            if i is None or i >= len(r):
                return 0
            s = str(r[i]).replace(",", "").strip()
            try:
                return int(float(s)) if s else 0
            except ValueError:
                return 0
        d, rp = num(i_d), num(i_r)
        if d == 0 and rp == 0:
            continue
        dem += d; rep += rp; npa += num(i_n); oth += num(i_o)
    if dem + rep <= 0:
        raise RuntimeError("PA: no county rows parsed")
    return {"rep": rep, "dem": dem, "npa": npa, "oth": oth, "as_of": as_of}


def parse_nc():
    """NCSBE RegStat dashboard (vt.ncsbe.gov/RegStat/). ASP.NET form: GET the page
    for the anti-forgery token + cookie and the latest weekly date, then POST to get
    an inline Kendo grid whose rows include a 'Totals' object with statewide party
    counts (Democrats / Republicans / Unaffiliated + minor parties)."""
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj),
                                     urllib.request.HTTPSHandler(context=C._SSL_CTX))
    op.addheaders = [("User-Agent", C.USER_AGENT), ("Accept-Language", "en-US,en;q=0.9")]
    base = "https://vt.ncsbe.gov/RegStat/"
    g = op.open(base, timeout=60).read().decode("utf-8", "replace")
    tok = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', g)
    dates = re.findall(r'<option[^>]*value="(\d{2}/\d{2}/\d{4})"', g)
    if not tok or not dates:
        raise RuntimeError("NC: token/date not found")
    date = dates[0]
    body = urllib.parse.urlencode({
        "RegistrationStatisticsSearchFilter.SelectedYear": date.split("/")[-1],
        "RegistrationStatisticsSearchFilter.SelectedDate": date,
        "btnSearch": "Search",
        "__RequestVerificationToken": tok.group(1),
    }).encode()
    resp = op.open(urllib.request.Request(base, data=body, method="POST"), timeout=90).read().decode("utf-8", "replace")
    tm = re.search(r'\{"CountyName":"Totals"[^}]*\}', resp)
    if not tm:
        raise RuntimeError("NC: totals row not found")
    row = json.loads(tm.group(0))
    dem = int(row.get("Democrats", 0)); rep = int(row.get("Republicans", 0)); npa = int(row.get("Unaffiliated", 0))
    oth = sum(int(row.get(k, 0)) for k in ("Libertarians", "Green", "NoLabels", "Constitution", "JusticeForAll", "WeThePeople"))
    if dem + rep <= 0:
        raise RuntimeError("NC: empty totals")
    from datetime import datetime
    try:
        as_of = datetime.strptime(date, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        as_of = date
    return {"rep": rep, "dem": dem, "npa": npa, "oth": oth, "as_of": as_of}


# SOURCES[code] -> function() -> {"rep","dem","npa","oth","as_of"} (wired per state)
SOURCES = {"fl": parse_fl, "pa": parse_pa, "nc": parse_nc}


def main():
    force = "--force" in sys.argv
    # Registration changes ~monthly, so refresh at most weekly even though the
    # workflow calls this every cycle (keeps heavy sources, e.g. NC's voter file,
    # from being fetched constantly). --force overrides.
    existing = load(OUT_PATH)
    if existing and existing.get("states") and not force and age_days(existing.get("generated_at", "")) < 6.5:
        print("registration.json is fresh (<6.5 days) — skipping (refreshes ~weekly).")
        return 0
    reg = load(STATES_PATH, {}) or {}
    srcmap = (load(SRC_PATH, {}) or {}).get("sources", {})
    out = {}
    for s in reg.get("states", []):
        code = s.get("code")
        if s.get("partisan", True) is False:
            continue                      # turnout-only states have no party registration
        fn = SOURCES.get(code)
        if not fn:
            continue
        try:
            r = fn() or {}
        except Exception as e:  # noqa: BLE001
            print("  ! %s registration failed: %s" % (code, str(e)[:60]), file=sys.stderr)
            continue
        rep = int(r.get("rep", 0)); dem = int(r.get("dem", 0)); npa = int(r.get("npa", 0)); oth = int(r.get("oth", 0))
        tot = rep + dem + npa + oth
        if tot <= 0:
            continue
        out[code] = {"rep": rep, "dem": dem, "npa": npa, "oth": oth, "total": tot,
                     "as_of": r.get("as_of", ""), "source": srcmap.get(code, "")}
    doc = {"generated_at": now(), "note": "Statewide voter registration by party; the denominator for the Registration-vs-Turnout comparison. Wired per state from official registration-statistics sources.", "states": out}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    print("registration.json: %d states with party-registration data%s" %
          (len(out), (" (" + ", ".join(sorted(out)) + ")") if out else " — none wired yet"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
