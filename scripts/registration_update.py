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
MANUAL_PATH = os.path.join(ROOT, "config", "registration_manual.json")
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


def parse_co():
    """CO SoS monthly 'Party & Status' registration workbook. The county table's
    ACTIVE block (columns up to 'Active Total') has a 'Total' row with statewide
    counts by CO party code: DEM, REP, UAF (unaffiliated) + minor parties. Files
    are per-month (…/2026/<Month>Statistics2026.xlsx); walk back from the current
    month to the newest posted file."""
    from datetime import datetime, timezone
    months = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]
    base = "https://www.coloradosos.gov/pubs/elections/VoterRegNumbers/2026/%sStatistics2026.xlsx"
    now = datetime.now(timezone.utc)
    start = now.month if now.year == 2026 else 12
    raw = as_of = None
    for mi in range(start, 0, -1):
        try:
            raw = C.http_get(base % months[mi - 1], binary=True, no_cache=True)
            as_of = "%s 2026" % months[mi - 1]
            break
        except Exception:  # noqa: BLE001
            continue
    if not raw:
        raise RuntimeError("CO: no monthly file")
    rows = (C.read_xlsx(raw).get("Party & Status")) or []
    hdr = hidx = None
    for i, r in enumerate(rows[:8]):
        cells = [str(x).strip() for x in r]
        if "DEM" in cells and "REP" in cells:
            hdr, hidx = cells, i
            break
    if not hdr:
        raise RuntimeError("CO: header not found")
    act_end = hdr.index("Active Total") if "Active Total" in hdr else len(hdr)
    ahdr = hdr[:act_end]

    def col(name):
        return ahdr.index(name) if name in ahdr else None
    total = None
    for r in rows[hidx + 1:]:
        if str(r[0]).strip().lower() == "total":
            total = r
            break
    if not total:
        raise RuntimeError("CO: total row not found")
    rep, dem, npa = _num(_cell(total, col("REP"))), _num(_cell(total, col("DEM"))), _num(_cell(total, col("UAF")))
    oth = sum(_num(_cell(total, col(m))) for m in ("ACN", "APV", "CTR", "FWD", "GRN", "LBR", "NOL", "UNI"))
    if rep + dem <= 0:
        raise RuntimeError("CO: empty totals")
    return {"rep": rep, "dem": dem, "npa": npa, "oth": oth, "as_of": as_of}


def _cell(row, i):
    return row[i] if (i is not None and i < len(row)) else 0


def _pdf_text(raw):
    import io
    import pypdf
    return "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(raw)).pages)


def _months_back(pattern, fmt):
    """Yield (url, label) walking back from the current month through Jan 2026.
    pattern is a format string taking the value produced by fmt(month_index)."""
    from datetime import datetime, timezone
    names = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]
    now = datetime.now(timezone.utc)
    start = now.month if now.year >= 2026 else 12
    for mi in range(start, 0, -1):
        yield pattern % fmt(mi), names[mi - 1] + " 2026"


def parse_md():
    """MD SBE monthly Voter Registration Activity Report (pdf/vrar/2026/MSR-2026_MM.pdf).
    Each jurisdiction row's 'TOTAL ACTIVE REGISTRATION' block is the 6 party
    integers (DEM REP GRN WCP UNA OTH) right before that row's comma-formatted
    active total. Sum the 24 jurisdictions. UNA->npa, GRN+WCP+OTH->oth."""
    raw = as_of = None
    for url, label in _months_back("https://elections.maryland.gov/pdf/vrar/2026/MSR-2026_%s.pdf",
                                   lambda m: "%02d" % m):
        try:
            r = C.http_get(url, binary=True)
            if r[:4] == b"%PDF":
                raw = r; as_of = label; break
        except Exception:  # noqa: BLE001
            continue
    if not raw:
        raise RuntimeError("MD: no monthly report")
    # Use the statewide TOTAL row. Its 'TOTAL ACTIVE REGISTRATION' block is the 6
    # party counts (DEM REP GRN WCP UNA OTH) that sum to the active total; find it
    # by that invariant (comma formatting in the PDF is inconsistent), picking the
    # largest matching total (the active-registration block, ~4.3M).
    best = None
    for ln in _pdf_text(raw).splitlines():
        if not ln.strip().upper().startswith("TOTAL"):
            continue
        nums = [int(x.replace(",", "")) for x in re.findall(r"[\d,]+", ln)]
        for i in range(6, len(nums)):
            if nums[i] and sum(nums[i - 6:i]) == nums[i]:
                if best is None or nums[i] > best[6]:
                    best = nums[i - 6:i] + [nums[i]]
    if not best or best[6] < 1000000:
        raise RuntimeError("MD: statewide TOTAL row not found")
    dem, rep, grn, wcp, una, oth = best[:6]
    return {"rep": rep, "dem": dem, "npa": una, "oth": grn + wcp + oth, "as_of": as_of}


def parse_ak():
    """AK Division of Elections monthly 'Voters by Party and Precinct' HTML report
    (…/statistics/2026/<MON>/VOTERS BY PARTY AND PRECINCT.htm). The statewide row
    is a grand total followed by 16 party/group columns in the fixed order
    D L R C G I J M Q S V W Y Z N U (they sum to the grand total). D->dem, R->rep,
    N (Nonpartisan) + U (Undeclared) -> npa, the 12 minor groups -> oth."""
    abbr = {1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
            7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC"}
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    start = now.month if now.year >= 2026 else 12
    base = "https://www.elections.alaska.gov/statistics/2026/%s/VOTERS%%20BY%%20PARTY%%20AND%%20PRECINCT.htm"
    html = as_of = None
    for mi in range(start, 0, -1):
        try:
            h = C.http_get(base % abbr[mi], accept="text/html,*/*;q=0.8")
            if "TOTAL" in h and re.search(r"\bD\b\s*</?", h) is not None or "PARTY" in h:
                html = h; as_of = abbr[mi] + " 2026"; break
        except Exception:  # noqa: BLE001
            continue
    if not html:
        raise RuntimeError("AK: no monthly report")
    txt = re.sub(r"<[^>]+>", " ", html)
    nums = [int(x.replace(",", "")) for x in re.findall(r"\d[\d,]*", txt)]
    best = None
    for i in range(len(nums) - 16):
        w = nums[i + 1:i + 17]
        if nums[i] > 400000 and sum(w) == nums[i]:      # grand total then 16 party cols
            if best is None or nums[i] > best[0]:
                best = [nums[i]] + w
    if not best:
        raise RuntimeError("AK: statewide totals row not found")
    tot, w = best[0], best[1:]
    dem, rep = w[0], w[2]
    npa = w[14] + w[15]           # Nonpartisan + Undeclared
    oth = tot - rep - dem - npa
    return {"rep": rep, "dem": dem, "npa": npa, "oth": oth, "as_of": as_of}


def parse_ne():
    """NE SoS monthly statewide VR report (…/2026VR/Statewide-<Month>-2026.pdf).
    Has a 'Grand Total' row: [Republican, Democratic, Libertarian, <minor…>,
    Nonpartisan, Grand Total]. Nonpartisan (col before the total) -> npa; the
    middle minor-party columns -> oth."""
    names = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    start = now.month if now.year >= 2026 else 12
    base = "https://sos.nebraska.gov/sites/default/files/doc/elections/vrstats/2026VR/Statewide-%s-2026.pdf"
    raw = as_of = None
    for mi in range(start, 0, -1):
        try:
            r = C.http_get(base % names[mi - 1], binary=True)
            if r[:4] == b"%PDF":
                raw = r; as_of = names[mi - 1] + " 2026"; break
        except Exception:  # noqa: BLE001
            continue
    if not raw:
        raise RuntimeError("NE: no monthly report")
    for ln in _pdf_text(raw).splitlines():
        if not re.search(r"grand total", ln, re.I):
            continue
        nums = [int(x.replace(",", "")) for x in re.findall(r"[\d,]+", ln)]
        if len(nums) >= 4 and sum(nums[:-1]) == nums[-1] and nums[-1] > 500000:
            rep, dem = nums[0], nums[1]
            npa = nums[-2]
            oth = sum(nums[2:-2])
            return {"rep": rep, "dem": dem, "npa": npa, "oth": oth, "as_of": as_of}
    raise RuntimeError("NE: grand total row not found")


def parse_ky():
    """KY SBE monthly Voter Registration Statistics Report (voterstatscounty-<Month> 2026.pdf).
    Has a 'Statewide totals' row: [precincts, Dem, Rep, Other, Ind, Libert, Green,
    Const, Reform, SocWk, KYPrty, Male, Female, Registered]. Ind->npa;
    Other+minor parties->oth."""
    raw = as_of = None
    names = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    start = now.month if now.year >= 2026 else 12
    base = "https://elect.ky.gov/Resources/Documents/"
    for mi in range(start, 0, -1):
        for fn in ("voterstatscounty-%s%%202026.pdf" % names[mi - 1],
                   "voterstatscounty-%%20%s%%202026.pdf" % names[mi - 1]):
            try:
                r = C.http_get(base + fn, binary=True)
                if r[:4] == b"%PDF":
                    raw = r; as_of = names[mi - 1] + " 2026"; break
            except Exception:  # noqa: BLE001
                continue
        if raw:
            break
    if not raw:
        raise RuntimeError("KY: no monthly report")
    m = re.search(r"Statewide totals\s+([\d\s,]+)", _pdf_text(raw))
    if not m:
        raise RuntimeError("KY: no statewide totals row")
    nums = [int(x.replace(",", "")) for x in re.findall(r"[\d,]+", m.group(1))]
    if len(nums) < 14:
        raise RuntimeError("KY: unexpected totals row (%d cols)" % len(nums))
    dem, rep, other, ind = nums[1], nums[2], nums[3], nums[4]
    minor = sum(nums[5:11])          # Libert, Green, Const, Reform, SocWk, KY Prty
    return {"rep": rep, "dem": dem, "npa": ind, "oth": other + minor, "as_of": as_of}


def _latest_pdf(pattern, fmt):
    """Walk back from the current month to the newest posted monthly PDF.
    Returns (raw, as_of_label, url) or raises."""
    for url, label in _months_back(pattern, fmt):
        try:
            r = C.http_get(url, binary=True)
            if r[:4] == b"%PDF":
                return r, label, url
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("no monthly report found for %s" % pattern)


def parse_ia():
    """IA SoS monthly county 'Voter Registration Totals' PDF
    (elections/pdf/VRStatsArchive/2026/Co<Mon>26.pdf). The statewide 'Totals' row is
    [Dem, Rep, No Party, Other, Total] ACTIVE, the same five INACTIVE, then the grand
    total. Uses active registration, as CO and MD do. No Party -> npa."""
    abbr = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    raw, as_of, url = _latest_pdf(
        "https://sos.iowa.gov/elections/pdf/VRStatsArchive/2026/Co%s26.pdf", lambda m: abbr[m - 1])
    m = re.search(r"^Totals\s+([\d,]+(?:\s+[\d,]+){10})\s*$", _pdf_text(raw), re.M)
    if not m:
        raise RuntimeError("IA: Totals row not found")
    n = [int(x.replace(",", "")) for x in m.group(1).split()]
    dem, rep, npa, oth, active = n[0], n[1], n[2], n[3], n[4]
    if dem + rep + npa + oth != active:
        raise RuntimeError("IA: active party columns don't sum to the active total")
    return {"rep": rep, "dem": dem, "npa": npa, "oth": oth, "as_of": as_of, "source": url}


def parse_or():
    """OR SoS monthly 'Voter Registration by County' PDF
    (elections/Documents/Registration/2026-<month>.pdf). The party table's 'Total' row
    is [Democrat, Republican, Non Affiliated, Constitution, Independent, Libertarian,
    No Labels, Pacific Green, Progressive, We the People, Working Families, Other,
    Total]. Non Affiliated -> npa; every minor party (including the Independent Party
    of Oregon, a real party) -> oth."""
    names = ["january", "february", "march", "april", "may", "june", "july",
             "august", "september", "october", "november", "december"]
    raw, as_of, url = _latest_pdf(
        "https://sos.oregon.gov/elections/Documents/Registration/2026-%s.pdf", lambda m: names[m - 1])
    m = re.search(r"^Total\s+([\d,]+(?:\s+[\d,]+){12})\s*$", _pdf_text(raw), re.M)
    if not m:
        raise RuntimeError("OR: Total row not found")
    n = [int(x.replace(",", "")) for x in m.group(1).split()]
    if sum(n[:12]) != n[12]:
        raise RuntimeError("OR: party columns don't sum to the total")
    dem, rep, npa = n[0], n[1], n[2]
    return {"rep": rep, "dem": dem, "npa": npa, "oth": sum(n[3:12]), "as_of": as_of, "source": url}


def parse_wy():
    """WY SoS monthly 'Voter Registration Statistics' PDF
    (Elections/Docs/VRStats/2026/26<Mon>VR_Stats.pdf). Per-county rows plus a
    statewide 'TOTAL' row: [Constitution, Democratic, Libertarian, Republican,
    Unaffiliated, Other, TOTAL]. Unaffiliated -> npa; Constitution/Libertarian/
    Other -> oth."""
    abbr = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    raw, as_of, url = _latest_pdf(
        "https://sos.wyo.gov/Elections/Docs/VRStats/2026/26%sVR_Stats.pdf", lambda m: abbr[m - 1])
    m = re.search(r"^TOTAL\s+([\d,]+(?:\s+[\d,]+){6})\s*$", _pdf_text(raw), re.M)
    if not m:
        raise RuntimeError("WY: TOTAL row not found")
    n = [int(x.replace(",", "")) for x in m.group(1).split()]
    if len(n) != 7 or sum(n[:6]) != n[6]:
        raise RuntimeError("WY: party columns don't sum to the total")
    const, dem, lib, rep, npa, other = n[:6]
    return {"rep": rep, "dem": dem, "npa": npa, "oth": const + lib + other, "as_of": as_of, "source": url}


def parse_nj():
    """NJ Division of Elections monthly Statewide Voter Registration Summary PDF
    (assets/pdf/svrs-reports/2026/2026-<MM>-voter-registration-by-county.pdf).
    Per-county rows plus a lowercase 'total' row: [UNA, DEM, REP, CNV, CON, GRE,
    LIB, NAT, RFP, SSP, total]. UNA (unaffiliated) -> npa; the 7 minor parties
    (Conservative, Constitution, Green, Libertarian, Natural Law, Reform,
    Socialist) -> oth."""
    raw, as_of, url = _latest_pdf(
        "https://www.nj.gov/state/elections/assets/pdf/svrs-reports/2026/2026-%s-voter-registration-by-county.pdf",
        lambda m: "%02d" % m)
    m = re.search(r"^total\s+([\d]+(?:\s+[\d]+){10})\s*$", _pdf_text(raw), re.M)
    if not m:
        raise RuntimeError("NJ: total row not found")
    n = [int(x) for x in m.group(1).split()]
    if len(n) != 11 or sum(n[:10]) != n[10]:
        raise RuntimeError("NJ: party columns don't sum to the total")
    una, dem, rep = n[0], n[1], n[2]
    oth = sum(n[3:10])
    return {"rep": rep, "dem": dem, "npa": una, "oth": oth, "as_of": as_of, "source": url}


def parse_ut():
    """UT Lt. Governor's Office 'Active Voters by County and Party' PDF, linked
    (dated in its filename, so resolved from the listing page rather than
    guessed) from vote.utah.gov/current-voter-registration-statistics/. Rows are
    (County, Party, Voters) triples -- a county with 0 for a party just omits
    that row, so parties are summed by name rather than by fixed position.
    Unaffiliated -> npa; every minor party (incl. Independent American, a real
    UT party) -> oth."""
    listing = C.http_get("https://vote.utah.gov/current-voter-registration-statistics/", no_cache=True)
    m = re.search(r'href="(https://vote\.utah\.gov/wp-content/uploads/\d{4}/\d{2}/'
                  r'[\d.]+-Active-Voters-by-County-and-Party\.pdf)"', listing)
    if not m:
        raise RuntimeError("UT: current report link not found")
    url = m.group(1)
    txt = _pdf_text(C.http_get(url, binary=True, timeout=60)).replace("Independent\nAmerican", "Independent American")
    rows = re.findall(r"^.*?\s+(Constitution|Democratic|Forward|Green|Independent American|"
                      r"Libertarian|Republican|Unaffiliated)\s+([\d,]+)\s*$", txt, re.M)
    if not rows:
        raise RuntimeError("UT: no county/party rows parsed")
    tot = {}
    for party, n in rows:
        tot[party] = tot.get(party, 0) + int(n.replace(",", ""))
    dem, rep, npa = tot.get("Democratic", 0), tot.get("Republican", 0), tot.get("Unaffiliated", 0)
    oth = sum(v for k, v in tot.items() if k not in ("Democratic", "Republican", "Unaffiliated"))
    if dem + rep <= 0:
        raise RuntimeError("UT: empty totals")
    dm = re.search(r"(\d{2}/\d{2}/\d{4})", txt)
    from datetime import datetime
    as_of = datetime.strptime(dm.group(1), "%m/%d/%Y").strftime("%Y-%m-%d") if dm else ""
    return {"rep": rep, "dem": dem, "npa": npa, "oth": oth, "as_of": as_of, "source": url}


def parse_sd():
    """SD SoS 'Voter Registration Tracking' page -- an HTML table with one row
    per snapshot date (newest first): [date, No Labels, Constitution,
    Republican, Democrat, NPA, Independent, NPA/IND, Libertarian, Other, Total,
    Inactive]. Columns are matched by header name (order has drifted over the
    years) rather than fixed position. NPA + Independent + NPA/IND -> npa; the
    remaining minor-party columns -> oth."""
    from datetime import datetime
    url = ("https://sdsos.gov/elections-voting/upcoming-elections/voter-registration-totals/"
           "voter-registration-comparison-table.aspx")
    h = C.http_get(url, no_cache=True)
    thead = re.search(r"<thead>(.*?)</thead>", h, re.S)
    tbody = re.search(r"<tbody>\s*<tr>(.*?)</tr>", h, re.S)
    if not thead or not tbody:
        raise RuntimeError("SD: table not found")
    headers = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<th[^>]*>(.*?)</th>", thead.group(1), re.S)]
    cells = re.findall(r"<td[^>]*>(.*?)</td>", tbody.group(1), re.S)
    if len(cells) < 2:
        raise RuntimeError("SD: no data row found")

    def clean(c):
        return re.sub(r"<[^>]+>", "", c).replace("&nbsp;", "").strip()

    def num(c):
        s = clean(c).replace(",", "")
        return int(s) if s.lstrip("-").isdigit() else 0
    vals = {name: num(c) for name, c in zip(headers[1:], cells[1:])}
    rep, dem = vals.get("Republican", 0), vals.get("Democrat", 0)
    npa = vals.get("NPA", 0) + vals.get("Independent", 0) + vals.get("NPA/IND", 0)
    oth = vals.get("No Labels", 0) + vals.get("Constitution", 0) + vals.get("Libertarian", 0) + vals.get("Other", 0)
    total = vals.get("Total", 0)
    if rep + dem <= 0 or (rep + dem + npa + oth) != total:
        raise RuntimeError("SD: party columns don't sum to the total")
    dm = re.search(r"([A-Za-z]+ \d{1,2}, \d{4})", clean(cells[0]))
    as_of = datetime.strptime(dm.group(1), "%B %d, %Y").strftime("%Y-%m-%d") if dm else ""
    return {"rep": rep, "dem": dem, "npa": npa, "oth": oth, "as_of": as_of, "source": url}


def parse_ca():
    """CA SoS periodic 'Report of Registration' -- the newest report's slug is
    resolved from the report-registration index (reports are posted roughly
    every 45-60 days, not monthly, and the exact slug isn't predictable), then
    that report's 'By County' workbook is fetched for its 'State Total' row.
    No Party Preference -> npa; American Independent/Green/Libertarian/Peace and
    Freedom/Unknown/Other -> oth."""
    from datetime import datetime
    idx = C.http_get("https://www.sos.ca.gov/elections/report-registration", no_cache=True)
    slugs = re.findall(r'href="https://www\.sos\.ca\.gov/elections/report-registration/([a-z0-9-]+)"', idx)
    if not slugs:
        raise RuntimeError("CA: no report links found")
    slug = slugs[-1]
    sheets = C.read_xlsx(C.http_get(
        "https://elections.cdn.sos.ca.gov/ror/%s/county.xlsx" % slug, binary=True, timeout=60))
    rows = sheets.get("By County") or (list(sheets.values())[0] if sheets else [])
    total_row = next((r for r in rows if r and str(r[0]).strip() == "State Total"), None)
    if not total_row:
        raise RuntimeError("CA: State Total row not found")

    def n(i):
        try:
            return int(float(str(total_row[i]).replace(",", "") or 0))
        except (ValueError, IndexError):
            return 0
    total_reg = n(2)
    dem, rep = n(3), n(4)
    oth = n(5) + n(6) + n(7) + n(8) + n(9) + n(10)   # AI, Green, Libertarian, P&F, Unknown, Other
    npa = n(11)                                       # No Party Preference
    if dem + rep <= 0 or (dem + rep + oth + npa) != total_reg:
        raise RuntimeError("CA: party columns don't sum to the total")
    as_of = ""
    try:
        page = C.http_get("https://www.sos.ca.gov/elections/report-registration/%s" % slug)
        dm = re.search(r"<h1>Report of Registration - ([^<]+)</h1>", page)
        if dm:
            as_of = datetime.strptime(dm.group(1).strip(), "%B %d, %Y").strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001 - as_of is a nice-to-have, not worth failing the parse
        pass
    return {"rep": rep, "dem": dem, "npa": npa, "oth": oth, "as_of": as_of,
            "source": "https://www.sos.ca.gov/elections/report-registration/%s" % slug}


def parse_wv():
    """WV SoS 'Voter Registration Totals' page -- a grid of per-month download
    links (Drupal media entities, so URLs aren't a predictable pattern); resolve
    the newest one from the highest-numbered year block, then that month's PDF
    has a per-county table and a statewide 'WEST VIRGINIA' total row: [Democrat,
    Republican, Mountain, Libertarian, Constitution, No Party, Other, Total].
    No Party -> npa; Mountain/Libertarian/Constitution/Other -> oth."""
    from datetime import datetime
    listing = C.http_get(
        "https://sos.wv.gov/elections/election-data/west-virginia-voter-registration-totals", no_cache=True)
    blocks = re.findall(r'"vrt-year-label">(\d{4})</div><div class="vrt-month-list">(.*?)</div></div>',
                        listing, re.S)
    if not blocks:
        raise RuntimeError("WV: no year blocks found")
    _, block = max(blocks, key=lambda b: int(b[0]))
    links = re.findall(r'href="(/media/\d+/download\?inline)"', block)
    if not links:
        raise RuntimeError("WV: no month links found")
    url = "https://sos.wv.gov" + links[-1]         # last link in the year = most recently posted month
    txt = _pdf_text(C.http_get(url, binary=True, timeout=60))
    m = re.search(r"WEST VIRGINIA\s+([\d,]+(?:\s+[\d,]+){7})", txt)
    if not m:
        raise RuntimeError("WV: statewide total row not found")
    n = [int(x.replace(",", "")) for x in m.group(1).split()]
    if len(n) != 8 or sum(n[:7]) != n[7]:
        raise RuntimeError("WV: party columns don't sum to the total")
    dem, rep, mtn, lib, con, npa, other = n[:7]
    dm = re.search(r"as of ([A-Za-z]+ \d{1,2}, \d{4})", txt, re.I)
    as_of = datetime.strptime(dm.group(1), "%B %d, %Y").strftime("%Y-%m-%d") if dm else ""
    return {"rep": rep, "dem": dem, "npa": npa, "oth": mtn + lib + con + other, "as_of": as_of, "source": url}


def parse_ok():
    """OK State Election Board month-end 'Registration Statistics by County' PDF,
    linked from the 2026 statistics archive page (most-recent-first). Has a
    'Grand Total' row: [Republican, Democrat, Libertarian, Independent, Total].
    Independent (OK's no-party-affiliation label) -> npa; Libertarian -> oth."""
    from datetime import datetime
    archive = ("https://oklahoma.gov/elections/voter-registration/voter-registration-statistics/"
              "voter-registration-statistics-archive/2026-month-end-voter-registration-statistics.html")
    h = C.http_get(archive, no_cache=True)
    m = re.search(r'href="(/content/dam/ok/en/elections/voter-registration-statistics/'
                 r'2026-vr-statistics/[\d-]+_vrstats-county\.pdf)"', h)
    if not m:
        raise RuntimeError("OK: no month-end report link found")
    url = "https://oklahoma.gov" + m.group(1)
    txt = _pdf_text(C.http_get(url, binary=True, timeout=60))
    m2 = re.search(r"Grand Total\s+([\d,]+(?:\s+[\d,]+){4})", txt)
    if not m2:
        raise RuntimeError("OK: Grand Total row not found")
    n = [int(x.replace(",", "")) for x in m2.group(1).split()]
    if len(n) != 5 or sum(n[:4]) != n[4]:
        raise RuntimeError("OK: party columns don't sum to the total")
    rep, dem, lib, npa = n[:4]
    dm = re.search(r"Month Ending:\s*(\d{1,2}/\d{1,2}/\d{4})", txt)
    as_of = datetime.strptime(dm.group(1), "%m/%d/%Y").strftime("%Y-%m-%d") if dm else ""
    return {"rep": rep, "dem": dem, "npa": npa, "oth": lib, "as_of": as_of, "source": url}


def parse_ks():
    """KS SoS monthly 'Voter Registration Numbers by County' xlsx
    (elections/vr-statistics/<YYYY>/<MM>-<YYYY>-...xlsx, newest linked from the
    statistics page). 'By County' sheet 'Grand Total' row: [Democratic,
    Libertarian, No Labels Kansas, Republican, United Kansas, Unaffiliated,
    Total]. Unaffiliated -> npa; the minor parties -> oth."""
    page = "https://sos.ks.gov/elections/voter-registration-statistics.html"
    h = C.http_get(page, no_cache=True)
    files = re.findall(r'href="([^"]*vr-statistics/(\d{4})/(\d{2})-\d{4}-Voter-Registration-Numbers-by-County\.xlsx)"', h)
    if not files:
        raise RuntimeError("KS: no monthly workbook linked")
    href, y, mo = max(files, key=lambda f: (f[1], f[2]))
    url = href if href.startswith("http") else "https://sos.ks.gov/elections/" + href.lstrip("/").replace("elections/", "", 1)
    rows = C.read_xlsx(C.http_get(url, binary=True, timeout=60)).get("By County", [])
    head = rows[0] if rows else []
    tot = next((r for r in rows if r and r[0].strip().lower() == "grand total"), None)
    if not tot:
        raise RuntimeError("KS: Grand Total row not found")
    vals = {h.strip().lower(): _num(v) for h, v in zip(head[1:], tot[1:])}
    rep, dem, npa = vals.get("republican", 0), vals.get("democratic", 0), vals.get("unaffiliated", 0)
    total = vals.get("total", 0)
    oth = total - rep - dem - npa
    if min(rep, dem, npa, oth) < 0 or not total:
        raise RuntimeError("KS: unexpected columns %s" % head)
    return {"rep": rep, "dem": dem, "npa": npa, "oth": oth, "as_of": "%s-%s-01" % (y, mo), "source": url}


def parse_dc():
    """DC Board of Elections 'Monthly Report of Voter Registration Statistics'
    PDF (Data-Statistics-Report-<M>_<YYYY>.pdf, newest linked from the voter
    registration statistics page). Citywide 'TOTALS' row: [DEM, REP, STG, N-P,
    OTH, TOTALS]. N-P -> npa; Statehood Green + other -> oth."""
    from datetime import datetime
    page = "https://www.dcboe.org/data,-maps,-forms/voter-registration-statistics"
    h = C.http_get(page, no_cache=True)
    files = re.findall(r'href="([^"]*Data-Statistics-Report-(\d{1,2})_(\d{4})\.pdf[^"]*)"', h)
    if not files:
        raise RuntimeError("DC: no monthly report linked")
    href, mo, y = max(files, key=lambda f: (int(f[2]), int(f[1])))
    url = href if href.startswith("http") else "https://www.dcboe.org" + href
    txt = _pdf_text(C.http_get(url, binary=True, timeout=60))
    m = re.search(r"^TOTALS[ \t]+([\d,]+(?:[ \t]+[\d,]+){5})", txt, re.M)   # first = citywide summary
    if not m:
        raise RuntimeError("DC: TOTALS row not found")
    n = [_num(x) for x in m.group(1).split()]
    if sum(n[:5]) != n[5]:
        raise RuntimeError("DC: party columns don't sum to the total")
    dem, rep, stg, npa, other = n[:5]
    dm = re.search(r"AS OF ([A-Z]+ \d{1,2}, \d{4})", txt)
    as_of = datetime.strptime(dm.group(1).title(), "%B %d, %Y").strftime("%Y-%m-%d") if dm else ""
    return {"rep": rep, "dem": dem, "npa": npa, "oth": stg + other, "as_of": as_of, "source": url,
            "name": "District of Columbia"}


def parse_ct():
    """CT Secretary of the State 'Registration and Party Enrollment Statistics'
    PDF (published about once a year, before the November election; newest
    linked from the Statistics and Data page). Statewide 'Totals' row gives
    active/inactive/total for Republican, Democratic, Minor Parties,
    Unaffiliated and all voters; we use ACTIVE voters (as for CO and IA)."""
    from datetime import datetime
    page = "https://portal.ct.gov/sots/election-services/statistics-and-data/statistics-and-data"
    h = C.http_get(page, no_cache=True)
    links = [x.replace("&amp;", "&") for x in re.findall(r'href="([^"]*registration[^"]*enrollment[^"]*\.pdf[^"]*)"', h, re.I)]
    if not links:
        raise RuntimeError("CT: no registration & enrollment PDF linked")
    url = links[0] if links[0].startswith("http") else "https://portal.ct.gov" + links[0]   # page lists newest first
    txt = _pdf_text(C.http_get(url, binary=True, timeout=60))
    m = re.search(r"^Totals\s+([\d,]+(?:\s+[\d,]+){14})\s*$", txt, re.M)
    if not m:
        raise RuntimeError("CT: statewide Totals row not found")
    n = [_num(x) for x in m.group(1).split()]
    rep, dem, minor, una, allv = n[0], n[3], n[6], n[9], n[12]      # active columns
    if rep + dem + minor + una != allv:
        raise RuntimeError("CT: active party columns don't sum to active total")
    dm = re.search(r"as of ([A-Za-z]+ \d{1,2}, \d{4})", txt)
    as_of = datetime.strptime(dm.group(1), "%B %d, %Y").strftime("%Y-%m-%d") if dm else ""
    return {"rep": rep, "dem": dem, "npa": una, "oth": minor, "as_of": as_of, "source": url.split("?")[0]}


def parse_nm():
    """NM SoS monthly 'Voter Registration Statistics -- Statewide by County' PDF.
    The 2026 page embeds one file widget per month (data-folder-id /
    data-widget-id); a public GetWidgetFiles call lists each month's files and
    PublicFiles/<account>/<fileId>/<name> serves them. Pick the newest
    'Statewide <MM-DD-YY>' file. Rows are count/percent pairs for DEMOCRATIC,
    REPUBLICAN, NO PARTY/DECLINED TO SELECT, OTHER, then TOTAL; the statewide
    row is the largest group whose four counts sum to its total."""
    from datetime import datetime
    api = "https://klvg4oyd4j.execute-api.us-west-2.amazonaws.com/prod/"
    year = datetime.utcnow().year
    page = "https://www.sos.nm.gov/voting-and-elections/data-and-maps/voter-registration-statistics/%d-voter-registration-data/" % year
    h = C.http_get(page, no_cache=True)
    acct = re.search(r'data-account-guid="([0-9a-f]{32})"', h)
    widgets = re.findall(r'data-folder-id="([0-9a-f-]{36})" data-widget-id="([0-9a-f-]{36})"', h)
    if not acct or not widgets:
        raise RuntimeError("NM: no file widgets on the %d page" % year)
    best = None
    for folder, widget in widgets:
        j = json.loads(C.http_get("%sGetWidgetFiles?widgetId=%s&folderId=%s&rootFolderId=%s&accountGUID=%s&authTokenGUID="
                                  % (api, widget, folder, folder, acct.group(1))))
        for f in (j.get("data") or {}).get("files", []):
            m = re.match(r"Statewide[ _](\d{2})-(\d{2})-(\d{2,4})\.pdf$", f.get("name", ""))
            if m:
                y = m.group(3) if len(m.group(3)) == 4 else "20" + m.group(3)
                key = "%s-%s-%s" % (y, m.group(1), m.group(2))
                if best is None or key > best[0]:
                    best = (key, f["fileId"], f["name"])
    if not best:
        raise RuntimeError("NM: no Statewide file found")
    url = "%sPublicFiles/%s/%s/%s" % (api, acct.group(1), best[1], urllib.parse.quote(best[2]))
    txt = _pdf_text(C.http_get(url, binary=True, timeout=60))
    toks = re.findall(r"[\d,]+\.\d+%|[\d,]+%?|[\d,]+", txt[txt.find("COUNTY"):])
    rows = []
    for i in range(len(toks) - 8):
        g = toks[i:i + 9]
        if all(g[k].endswith("%") for k in (1, 3, 5, 7)) and not any(g[k].endswith("%") for k in (0, 2, 4, 6, 8)):
            n = [_num(g[k]) for k in (0, 2, 4, 6, 8)]
            if n[4] and sum(n[:4]) == n[4]:
                rows.append(n)
    if not rows:
        raise RuntimeError("NM: statewide TOTAL row not found")
    dem, rep, npa, oth, _tot = max(rows, key=lambda r: r[4])
    return {"rep": rep, "dem": dem, "npa": npa, "oth": oth, "as_of": best[0], "source": url}


def parse_me():
    """ME SoS 'Statewide Registered and Enrolled Data File' -- the ACTIVE-voters
    workbook linked from the Voter Data page (e.g. 'E&R 3.7.26 ACTIVE ... .xlsx';
    the date is in the file name). One row per ward/precinct with party columns
    D, G (Green), L (Libertarian), R, U (unenrolled) and TOTAL; a trailing row
    carries the grand total, used as a check. U -> npa; G + L -> oth."""
    page = "https://www.maine.gov/sos/elections-voting/voter-data"
    h = C.http_get(page, no_cache=True)
    m = re.search(r'href="([^"]*E%26R%20(\d{1,2})\.(\d{1,2})\.(\d{2})%20ACTIVE[^"]*\.xlsx)"', h)
    if not m:
        raise RuntimeError("ME: ACTIVE registered & enrolled workbook not linked")
    url = m.group(1) if m.group(1).startswith("http") else "https://www.maine.gov" + m.group(1)
    as_of = "20%s-%02d-%02d" % (m.group(4), int(m.group(2)), int(m.group(3)))
    rows = next(iter(C.read_xlsx(C.http_get(url, binary=True, timeout=90)).values()), [])
    head = [c.strip().upper() for c in rows[0]]
    idx = {k: head.index(k) for k in ("D", "G", "L", "R", "U", "TOTAL")}
    t = {k: 0 for k in idx}
    grand = 0
    for r in rows[1:]:
        if len(r) > idx["TOTAL"] and r[idx["TOTAL"]].strip():
            for k, i in idx.items():
                t[k] += _num(r[i])
        else:   # footer row: the lone number is the grand total
            nums = [_num(c) for c in r if c.strip().isdigit()]
            grand = nums[0] if len(nums) == 1 else grand
    if t["D"] + t["G"] + t["L"] + t["R"] + t["U"] != t["TOTAL"] or (grand and grand != t["TOTAL"]):
        raise RuntimeError("ME: party columns don't add up to the total")
    return {"rep": t["R"], "dem": t["D"], "npa": t["U"], "oth": t["G"] + t["L"], "as_of": as_of, "source": url}


def parse_la():
    """LA SoS monthly 'Statewide Report of Registered Voters' PDF
    (electionstatistics.sos.la.gov/Data/Registration_Statistics/statewide/
    2026_MM01_sta_comb.pdf). First row 'STATE' then 20 numbers: registered
    total/white/black/other, then the same four for Democrats, Republicans,
    No Party and Other Parties. Uses each block's TOTAL."""
    raw, _label, url = _latest_pdf(
        "https://electionstatistics.sos.la.gov/Data/Registration_Statistics/statewide/2026_%s01_sta_comb.pdf",
        lambda m: "%02d" % m)
    txt = _pdf_text(raw)
    m = re.search(r"^STATE\s*\n((?:[\d,]+\s*\n){19}[\d,]+)", txt, re.M)
    if not m:
        raise RuntimeError("LA: STATE row not found")
    n = [_num(x) for x in m.group(1).split()]
    total, dem, rep, npa, oth = n[0], n[4], n[8], n[12], n[16]
    if dem + rep + npa + oth != total:
        raise RuntimeError("LA: party totals don't sum to registered total")
    mo = re.search(r"2026_(\d{2})01_sta_comb", url).group(1)
    return {"rep": rep, "dem": dem, "npa": npa, "oth": oth, "as_of": "2026-%s-01" % mo, "source": url}


def parse_de():
    """DE Department of Elections monthly 'Voter Registration Report by
    Political Party' (voter/registrationtotals/pdfs/vrt_PP<YYYYMM01>.html): one
    row per party with Kent / New Castle / Sussex / Grand Total, plus a Grand
    Total row. NO PARTY CHOICE + NO PARTY - AVR + NONPARTISAN -> npa; every
    other minor party (incl. the Independent Party of Delaware) -> oth."""
    for url, _label in _months_back("https://elections.delaware.gov/voter/registrationtotals/pdfs/vrt_PP2026%s01.html",
                                    lambda m: "%02d" % m):
        try:
            h = C.http_get(url)
        except Exception:  # noqa: BLE001
            continue
        rows = {}
        for tr in re.findall(r"<tr.*?</tr>", h, re.S | re.I):
            cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)]
            if len(cells) == 5 and re.fullmatch(r"[\d,]+", cells[4] or "x"):
                rows[cells[0].upper()] = _num(cells[4])
        total = rows.pop("GRAND TOTAL", 0)
        if not rows or not total:
            continue
        rep, dem = rows.pop("REPUBLICAN", 0), rows.pop("DEMOCRATIC", 0)
        npa = sum(rows.pop(k, 0) for k in list(rows) if k.startswith("NO PARTY") or k == "NONPARTISAN")
        oth = sum(rows.values())
        if rep + dem + npa + oth != total:
            raise RuntimeError("DE: party rows don't sum to the grand total")
        d = re.search(r"vrt_PP(\d{4})(\d{2})(\d{2})", url)
        return {"rep": rep, "dem": dem, "npa": npa, "oth": oth,
                "as_of": "%s-%s-%s" % d.groups(), "source": url}
    raise RuntimeError("DE: no monthly party report found")


def parse_ri():
    """RI Department of State 'Registered Voters' data hub
    (datahub.sos.ri.gov/RegisteredVoter.aspx) is a Power BI publish-to-web
    report; query it the way the page does (see oh_update.PowerBI). Entity
    CumulativeVoterRegistrationData: monthly snapshots ('Date '), PARTY, STATUS,
    'Voter Registration ' counts. Newest month, STATUS = Active (the report's
    own page filter). Unaffiliated -> npa."""
    from datetime import datetime, timezone
    from oh_update import PowerBI
    key = "ca3ba28c-538b-4cb2-a312-ec2421293ed6"
    pbi = PowerBI("https://wabi-us-gov-virginia-api.analysis.usgovcloudapi.net/public/reports/", key)
    ent = "CumulativeVoterRegistrationData"
    last = max(r[0] for r in pbi.query(ent, ["Date "], [], {}) if r[0])
    day = datetime.fromtimestamp(last / 1000, timezone.utc)
    rows = pbi.query(ent, ["PARTY"], ["Voter Registration "],
                     {"STATUS": ["'Active'"], "Date ": ["datetime'%s'" % day.strftime("%Y-%m-%dT%H:%M:%S")]})
    by = {(p or "").strip().lower(): int(n or 0) for p, n in rows}
    rep, dem, npa = by.pop("republican", 0), by.pop("democrat", 0), by.pop("unaffiliated", 0)
    if not (rep and dem and npa):
        raise RuntimeError("RI: unexpected party values %s" % list(by))
    return {"rep": rep, "dem": dem, "npa": npa, "oth": sum(by.values()), "as_of": day.strftime("%Y-%m-%d"),
            "source": "https://datahub.sos.ri.gov/RegisteredVoter.aspx"}


def parse_ny():
    """NYS Board of Elections 'Voter Enrollment by County, Party Affiliation and
    Status' (published each February and November). elections.ny.gov blocks
    automated requests, but the NYC Board of Elections republishes the same
    statewide workbook (vote.nyc 'Voter Enrollment Totals': county_<mon><yy>.xlsx).
    'Statewide Total' Active row: DEM, REP, CON, WOR, OTH, BLANK, TOTAL.
    BLANK (no party) -> npa; CON + WOR + OTH -> oth."""
    page = "https://vote.nyc/page/voter-enrollment-totals"
    h = C.http_get(page, no_cache=True)
    mons = {"feb": 2, "nov": 11}
    best = None
    for href, mon, yy in re.findall(r'href="([^"]*county_(feb|nov)(\d{2})[^"/]*\.xlsx)"', h, re.I):
        key = (int(yy), mons[mon.lower()])
        if best is None or key > best[0]:
            best = (key, href)
    if not best:
        raise RuntimeError("NY: no county enrollment workbook linked")
    url = best[1] if best[1].startswith("http") else "https://www.vote.nyc" + best[1]
    rows = next(iter(C.read_xlsx(C.http_get(url, binary=True, timeout=60)).values()), [])
    title = " ".join(" ".join(r) for r in rows[:3])
    act = next((r for r in rows if r and r[0].strip().lower() == "statewide total" and "active" in [c.strip().lower() for c in r]), None)
    if not act:
        raise RuntimeError("NY: Statewide Total Active row not found")
    n = [_num(c) for c in act if re.fullmatch(r"[\d,]+", c.strip())][-7:]    # cells can be sparse
    dem, rep, con, wor, other, blank, total = n
    if dem + rep + con + wor + other + blank != total:
        raise RuntimeError("NY: party columns don't sum to the total")
    from datetime import datetime
    dm = re.search(r"as of ([A-Za-z]+ \d{1,2}, \d{4})", title)
    as_of = datetime.strptime(dm.group(1), "%B %d, %Y").strftime("%Y-%m-%d") if dm else ""
    return {"rep": rep, "dem": dem, "npa": blank, "oth": con + wor + other, "as_of": as_of, "source": url}


# SOURCES[code] -> function() -> {"rep","dem","npa","oth","as_of"[,"source"]} (wired per state)
SOURCES = {"fl": parse_fl, "pa": parse_pa, "nc": parse_nc, "co": parse_co,
           "md": parse_md, "ky": parse_ky, "ne": parse_ne, "ak": parse_ak,
           "ia": parse_ia, "or": parse_or, "wy": parse_wy, "nj": parse_nj,
           "ut": parse_ut, "sd": parse_sd, "ca": parse_ca, "wv": parse_wv,
           "ok": parse_ok, "ks": parse_ks, "dc": parse_dc, "ct": parse_ct,
           "nm": parse_nm, "me": parse_me, "la": parse_la, "de": parse_de,
           "ri": parse_ri, "ny": parse_ny}
# not in assets/states.json (the 50 turnout states) but has party registration
EXTRA_CODES = ["dc"]


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
    # A state whose source is temporarily unreachable (site down, WAF-blocked from
    # this runner's IP, format change) keeps its last-known numbers instead of
    # dropping off the table; its original as_of date shows how old they are.
    prev_states = (existing or {}).get("states", {}) or {}
    out = {}
    for s in reg.get("states", []) + [{"code": c} for c in EXTRA_CODES]:
        code = s.get("code")
        # NOTE: states.json's `partisan` flag describes TURNOUT ballot-tracking
        # (whether early/mail ballots cast are broken out by party), not voter
        # REGISTRATION -- a state can be turnout-only (e.g. CA) yet still
        # publish official registration-by-party statistics. Only gate on
        # whether a parser is actually wired below.
        fn = SOURCES.get(code)
        if not fn:
            continue
        try:
            r = fn() or {}
        except Exception as e:  # noqa: BLE001
            print("  ! %s registration failed: %s" % (code, str(e)[:60]), file=sys.stderr)
            r = {}
        rep = int(r.get("rep", 0)); dem = int(r.get("dem", 0)); npa = int(r.get("npa", 0)); oth = int(r.get("oth", 0))
        tot = rep + dem + npa + oth
        if tot <= 0:
            prev = prev_states.get(code)
            if prev and not prev.get("manual"):
                out[code] = dict(prev, stale=True)
                print("  ! %s: keeping last-known values (as_of %s)" % (code, prev.get("as_of", "")), file=sys.stderr)
            continue
        out[code] = {"rep": rep, "dem": dem, "npa": npa, "oth": oth, "total": tot,
                     "as_of": r.get("as_of", ""), "source": r.get("source") or srcmap.get(code, "")}
        if r.get("name"):
            out[code]["name"] = r["name"]

    # Manual snapshots for states whose official source can't be auto-fetched
    # (e.g. Cloudflare/JS dashboards like Idaho's VoteIdaho). These are read by
    # hand from the site and refreshed periodically; an auto parser in SOURCES
    # always takes precedence over a manual entry for the same state.
    manual = (load(MANUAL_PATH, {}) or {}).get("states", {})
    for code, m in manual.items():
        if code in out:
            continue
        rep = int(m.get("rep", 0)); dem = int(m.get("dem", 0)); npa = int(m.get("npa", 0)); oth = int(m.get("oth", 0))
        tot = rep + dem + npa + oth
        if tot <= 0:
            continue
        out[code] = {"rep": rep, "dem": dem, "npa": npa, "oth": oth, "total": tot,
                     "as_of": m.get("as_of", ""), "source": m.get("source", srcmap.get(code, "")),
                     "manual": True, "note": m.get("note", "")}
        if m.get("name"):
            out[code]["name"] = m["name"]
    doc = {"generated_at": now(), "note": "Statewide voter registration by party; the denominator for the Registration-vs-Turnout comparison. Wired per state from official registration-statistics sources.", "states": out}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    print("registration.json: %d states with party-registration data%s" %
          (len(out), (" (" + ", ".join(sorted(out)) + ")") if out else " — none wired yet"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
