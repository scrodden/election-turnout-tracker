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

Not reachable from scripts (checked 2026-10-10): GA (sos.ga.gov 403), MN
(sos.mn.gov pages and electionresults.sos.mn.gov behind bot checks / CAPTCHA),
WI (elections.wi.gov Cloudflare challenge; its file names aren't discoverable
without the pages), MS (sos.ms.gov 403, files too), OH (ohiosos.gov
Cloudflare; the registration dashboard's report key isn't published outside
the blocked portal). North Dakota has no voter registration.

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


SOURCES = {"mt": parse_mt, "in": parse_in, "vt": parse_vt, "hi": parse_hi, "il": parse_il}


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
            r = fn()
            if not r.get("total"):
                raise RuntimeError("no total")
            out[code] = r
        except Exception as e:  # noqa: BLE001
            print("  ! %s registered total failed: %s" % (code, str(e)[:120]), file=sys.stderr)
            old = (prev.get("states") or {}).get(code)
            if old:
                out[code] = dict(old, stale=True)
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
