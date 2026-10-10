#!/usr/bin/env python3
"""Build data/registered_totals.json -- statewide registered-voter TOTALS for
states without party registration (so they aren't in data/registration.json),
used by build_status.py as the turnout denominator on the national page.

Each state is read from its own official source (no fabrication); a state whose
source fails keeps its last-known value (marked stale) instead of dropping out.

  mt  SoS 'Registered Voters By County' Tableau dashboard (tableau-ext.mt.gov,
      same server as the MT turnout connector): Active registered voters, live
  in  Election Division 'Registration and Turnout Data' (newest election's
      workbook on in.gov): Statewide 'Registered Voters' as of that election
  vt  SoS 'Voter Registration Totals' page: 'Active and Eligible Voters' table,
      newest month (updated the first of each month)
  hi  Office of Elections results: newest election's statewide summary.txt,
      'Registered Voters' column (as of that election)
  il  State Board of Elections 'Voter Turnout' page: newest election's 'All
      Jurisdictions' 'Total Voters' (registered, as of that election)

  ga  SoS 'Georgia Active Voters Report' -- the SoS's Tableau Public dashboard
      (CSV export of ActiveVotersbyCounty; sos.ga.gov itself blocks scripts):
      active voters, daily
  wi  WEC monthly 'Voter Registration Statistics' workbook VoterCountsByCounty
      (the pages are behind a Cloudflare check but the files are served; each
      month's file gets the next numeric suffix, so the next one is probed)
  mn  SoS 'Voter Registration by County since 2000' workbook (stable media
      URL; newest column), unless the manual snapshot is newer

Official figures read by hand from pages scripts can't reach (Cloudflare /
403) live in config/registered_totals_manual.json (OH: certified 2026 primary
press release; MS: monthly Active Voter Count report; MN: the counts page's
monthly county table). A manual entry is used when no parser covers the state
or when it is newer than the parsed figure.

  nd  North Dakota has no voter registration, so its turnout denominator is
      the Secretary of State's ELIGIBLE-voter estimate (from U.S. Census
      data), as the SoS itself uses: the sum of county 'Voters' on its results
      web service (results.sos.nd.gov -> resultsws ResultsAjax.svc/
      GetVoterTurnoutData) for the election currently loaded there; labelled
      from the SoS 'Election Statistics 1980-Present' table when it matches.

Refreshes about daily (registration moves slowly). Run:
  python scripts/registered_totals_update.py [--force]
"""
import html as H
import http.cookiejar
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

OUT_PATH = os.path.join(ROOT, "data", "registered_totals.json")
MANUAL_PATH = os.path.join(ROOT, "config", "registered_totals_manual.json")
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", H.unescape(h))).strip()


def _rows(table_html):
    return [[_text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S | re.I)]
            for r in re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S | re.I)]


def parse_mt():
    import mt_update as M
    page = "https://sosmt.gov/elections/regvotercounty/"
    cols = M.tableau_worksheet_rows("https://tableau-ext.mt.gov", "/t/SOS/views/RegisteredVotersByCounty/RegVoterCounty",
                                    "/vizql/t/SOS/w/RegisteredVotersByCounty/v/RegVoterCounty", "County Data")
    n = None
    for county, status, cnt in zip(cols["County"], cols["Voter Status"], cols["CNT(vrVoterReg)"]):
        if county == "%all%" and status == "Active":
            n = int(cnt)
    if not n:
        raise RuntimeError("MT: no statewide Active row")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {"total": n, "as_of": today, "basis": "active registered voters (live dashboard)", "source": page}


def parse_in():
    import xls_lite
    page = "https://www.in.gov/sos/elections/voter-information/register-to-vote/voter-registration-and-turnout-statistics"
    h = C.http_get(page, retries=2, timeout=60)
    for href, txt in re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', h, re.S | re.I):
        label = _text(txt)
        m = re.match(r"(20\d{2}) (General|Primary|Municipal)\b.*Registration", label, re.I)
        if not m or not re.search(r"\.xlsx?(\?|$)", href, re.I):
            continue   # newest first on the page; only workbooks are parsed
        url = urllib.parse.urljoin(page, H.unescape(href))
        rows = xls_lite.read_any(C.http_get(url, binary=True, retries=2, timeout=90))[0][1]
        hdr = next((r for r in rows if r and "Registered Voters" in [str(x).strip() for x in r if x]), None)
        if not hdr:
            continue
        ci = [str(x).strip() if x else "" for x in hdr].index("Registered Voters")
        tot = next((r for r in rows if r and str(r[0]).strip().lower() == "statewide"), None)
        if tot and isinstance(tot[ci], float):
            return {"total": int(tot[ci]), "as_of": "%s %s election" % (m.group(1), m.group(2).lower()),
                    "basis": "registered voters as of that election", "source": url}
    raise RuntimeError("IN: no registration workbook with a Statewide row")


def parse_vt():
    page = "https://sos.vermont.gov/node/1131"
    h = C.http_get(page, retries=2, timeout=60)
    for m in re.finditer(r"<table[^>]*>(.*?)</table>", h, re.S | re.I):
        before = _text(h[max(0, m.start() - 900):m.start()])
        if "could vote without additional action" not in before:
            continue   # the 'Active and Eligible Voters' table
        rows = _rows(m.group(1))
        months = [c.upper() for c in rows[0][1:]]
        for r in rows[1:]:
            if not r or not re.fullmatch(r"20\d{2}", r[0]):
                continue
            vals = [(i, C.parse_number(v)) for i, v in enumerate(r[1:]) if re.search(r"\d", v)]
            if vals:
                i, n = vals[-1]
                name = MONTHS[["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"].index(months[i])]
                return {"total": int(n), "as_of": "%s 1, %s" % (name, r[0]),
                        "basis": "active and eligible voters", "source": page}
    raise RuntimeError("VT: 'Active and Eligible Voters' table not found")


def parse_hi():
    page = "https://elections.hawaii.gov/election-results/"
    h = C.http_get(page, retries=2, timeout=60)
    links = [H.unescape(u) for u in re.findall(r'href="([^"]*/wp-content/results/[^"]*summary\.txt)"', h, re.I)]
    if not links:
        raise RuntimeError("HI: no statewide summary.txt link")

    def rank(u):
        m = re.search(r"/(20\d{2})\s*(General|Primary)", urllib.parse.unquote(u), re.I)
        return (int(m.group(1)), 1 if m.group(2).lower() == "general" else 0) if m else (0, 0)
    url = max(links, key=rank)
    s = C.http_get(urllib.parse.quote(url, safe=":/"), retries=2, timeout=60)
    lines = [l.split("\t") for l in s.splitlines() if l.strip()]
    hdr = next(l for l in lines if l and l[0].startswith("#Contest ID"))
    ci = [c.strip() for c in hdr].index("Registered Voters")
    vals = [C.parse_number(l[ci]) for l in lines if not l[0].startswith("#") and len(l) > ci and re.search(r"\d", l[ci])]
    if not vals:
        raise RuntimeError("HI: no Registered Voters values")
    y, kind = rank(url)
    return {"total": int(max(vals)), "as_of": "%d %s election" % (y, "general" if kind else "primary"),
            "basis": "registered voters as of that election", "source": url}


def parse_il():
    page = "https://www.elections.il.gov/ElectionOperations/VoterTurnout.aspx"
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=C._SSL_CTX))
    op.addheaders = [("User-Agent", C.USER_AGENT)]
    h = op.open(page, timeout=60).read().decode("utf-8", "replace")
    sel = re.search(r'<select[^>]+name="([^"]*ddlElections)"[^>]*>(.*?)</select>', h, re.S | re.I)
    opts = [(v, _text(t)) for v, t in re.findall(r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>', sel.group(2), re.S | re.I)
            if v and v != "0"]
    eid, label = opts[0]   # newest election first
    form = {}
    for tag in re.findall(r"<input[^>]+>", h, re.I):
        n, v, t = (re.search(r'%s="([^"]*)"' % a, tag) for a in ("name", "value", "type"))
        if n and (not t or t.group(1).lower() == "hidden"):
            form[n.group(1)] = H.unescape(v.group(1)) if v else ""
    form.update({sel.group(1): eid, "__EVENTTARGET": sel.group(1), "__EVENTARGUMENT": ""})
    r = op.open(urllib.request.Request(page, data=urllib.parse.urlencode(form).encode(), headers={"Referer": page}),
                timeout=90).read().decode("utf-8", "replace")
    for tb in re.findall(r"<table[^>]*>(.*?)</table>", r, re.S | re.I):
        rows = _rows(tb)
        if rows and "Total Voters" in rows[0]:
            ci = rows[0].index("Total Voters")
            tot = next((x for x in rows if x and x[0].lower() == "all jurisdictions"), None)
            if tot:
                return {"total": C.parse_number(tot[ci]), "as_of": label.title(),
                        "basis": "registered voters ('Total Voters') as of that election", "source": page}
    raise RuntimeError("IL: 'All Jurisdictions' row not found")


def parse_ga():
    import csv
    import io
    url = ("https://public.tableau.com/views/ElectionDashboard_16395162064680/ActiveVotersbyCounty.csv"
           "?:showVizHome=no")
    rows = list(csv.DictReader(io.StringIO(C.http_get(url, retries=2, timeout=60).lstrip("\ufeff"))))
    counties = [r for r in rows if (r.get("County Name") or "").strip() and
                not re.search(r"total", r.get("County Name", ""), re.I)]
    if len(counties) < 150:
        raise RuntimeError("GA: only %d county rows" % len(counties))
    total = sum(C.parse_number(r.get("Total 2") or r.get("Sum of Total") or "0") for r in counties)
    dates = {(r.get("Month, Day, Year of Calculation1") or "").strip() for r in counties} - {""}
    return {"total": int(total), "as_of": sorted(dates)[-1] if dates else "", "basis": "active voters",
            "source": "https://sos.ga.gov/georgia-active-voters-report"}


def parse_wi(prev=None):
    import xls_lite
    start = int((prev or {}).get("file_n") or 39)
    best = None
    for n in range(start, start + 4):
        url = "https://elections.wi.gov/sites/default/files/documents/VoterCountsByCounty_%d.xlsx" % n
        req = urllib.request.Request(url, headers={"User-Agent": C.USER_AGENT})
        try:
            with urllib.request.urlopen(req, context=C._SSL_CTX, timeout=60) as r:
                best = (n, url, r.read(), r.headers.get("Last-Modified", ""))
        except Exception:  # noqa: BLE001 - next month's file not posted yet
            if n > start:
                break
    if not best:
        raise RuntimeError("WI: no VoterCountsByCounty_%d.xlsx" % start)
    n, url, raw, lm = best
    rows = xls_lite.read_any(raw)[0][1]
    hdr = [str(x).strip() for x in rows[0]]
    ci = hdr.index("VoterCount")
    total = sum(r[ci] for r in rows[1:] if len(r) > ci and isinstance(r[ci], float))
    when = datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S GMT") if lm else None   # posted the day after the 1st
    as_of = "%s 1, %d" % (MONTHS[when.month - 1], when.year) if when else ""
    return {"total": int(total), "as_of": as_of, "as_of_date": when.strftime("%Y-%m-01") if when else "",
            "basis": "active registered voters", "file_n": n,
            "source": "https://elections.wi.gov/statistics-data/voter-registration-statistics", "file": url}


def parse_mn():
    import xls_lite
    from datetime import date, timedelta
    url = "https://www.sos.mn.gov/media/3294/minnesota-voter-registration-by-county-since-2000.xlsx"
    rows = xls_lite.read_any(C.http_get(url, binary=True, retries=2, timeout=90))[0][1]
    hi = next(i for i, r in enumerate(rows) if r and str(r[0]).strip() == "County")
    hdr = rows[hi]
    ci = max(i for i, v in enumerate(hdr) if v not in (None, ""))
    when = hdr[ci]
    d = (date(1899, 12, 30) + timedelta(days=int(when))) if isinstance(when, float) else \
        datetime.strptime(str(when).strip(), "%m/%d/%Y").date()
    total = sum(r[ci] for r in rows[hi + 1:] if r and isinstance(r[0], str) and r[0].strip()
                and not re.search(r"total", r[0], re.I) and len(r) > ci and isinstance(r[ci], float))
    return {"total": int(total), "as_of": "%s %d, %d" % (MONTHS[d.month - 1], d.day, d.year),
            "as_of_date": d.isoformat(), "basis": "registered voters (active and challenged)",
            "source": "https://www.sos.mn.gov/election-administration-campaigns/data-maps/voter-registration-counts/"}


def parse_nd():
    import io
    import pypdf
    rows = json.loads(C.http_get("https://resultsws.sos.nd.gov/ResultsAjax.svc/GetVoterTurnoutData?", retries=2,
                                 timeout=60, referer="https://results.sos.nd.gov/"))
    total = sum(int(r.get("Voters") or 0) for r in rows)
    if len(rows) != 53 or not total:
        raise RuntimeError("ND: expected 53 counties, got %d" % len(rows))
    as_of = "the election currently on the SoS results site"
    try:   # name the election from the SoS statistics table when its estimate matches
        raw = C.http_get("https://www.sos.nd.gov/sites/www/files/documents/elections/election-results-pdfs/"
                         "statistics-turnout.pdf", binary=True, retries=1, timeout=60)
        kinds = {"G": "general", "P": "primary", "PP": "presidential primary", "S": "special"}
        for page in pypdf.PdfReader(io.BytesIO(raw)).pages:
            for line in page.extract_text(extraction_mode="layout").splitlines():
                m = re.match(r"\s*((?:19|20)\d{2})\s+(PP|P|G|S)\s+(.*)$", line)
                if m:
                    cells = re.split(r"\s{2,}", m.group(3).strip())
                    if len(cells) >= 7 and C.parse_number(cells[6]) == total:
                        as_of = "SoS estimate used for the %s %s election" % (m.group(1), kinds[m.group(2)])
    except Exception:  # noqa: BLE001 - the label is optional
        pass
    return {"total": total, "as_of": as_of, "kind": "eligible",
            "basis": "eligible voters (SoS estimate from U.S. Census data; North Dakota has no voter registration)",
            "source": "https://results.sos.nd.gov/voterturnoutdetails.aspx"}


SOURCES = {"mt": parse_mt, "in": parse_in, "vt": parse_vt, "hi": parse_hi, "il": parse_il,
           "ga": parse_ga, "wi": parse_wi, "mn": parse_mn, "nd": parse_nd}


def main():
    force = "--force" in sys.argv
    prev = load(OUT_PATH, {}) or {}
    try:
        age = (datetime.now(timezone.utc) - datetime.strptime(prev.get("generated_at", ""), "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=timezone.utc)).total_seconds() / 86400.0
    except ValueError:
        age = 1e9
    if prev.get("states") and not force and age < 0.9:
        print("registered_totals.json is fresh (<1 day) - skipping.")
        return 0
    out = {}
    for code, fn in SOURCES.items():
        try:
            r = fn(((prev.get("states") or {}).get(code))) if code == "wi" else fn()
            if not r.get("total"):
                raise RuntimeError("no total")
            out[code] = r
        except Exception as e:  # noqa: BLE001
            print("  ! %s registered total failed: %s" % (code, str(e)[:120]), file=sys.stderr)
            old = (prev.get("states") or {}).get(code)
            if old:
                out[code] = dict(old, stale=True)
    # official figures read by hand where scripts are blocked; used when no parser
    # covers the state or when the snapshot is newer than the parsed figure
    for code, m in ((load(MANUAL_PATH, {}) or {}).get("states") or {}).items():
        if not m.get("total"):
            continue
        cur = out.get(code)
        if not cur or (m.get("as_of_date", "") > (cur.get("as_of_date") or "")):
            out[code] = dict({k: v for k, v in m.items() if not k.startswith("_")}, manual=True)
    doc = {"generated_at": now(),
           "note": "Statewide registered-voter totals for states without party registration (official sources; see "
                   "scripts/registered_totals_update.py). Used as the turnout denominator on the national page.",
           "states": out}
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    print("registered_totals.json: %s" % ", ".join("%s %s (%s)" % (k, format(v["total"], ","), v.get("as_of", ""))
                                                   for k, v in sorted(out.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
