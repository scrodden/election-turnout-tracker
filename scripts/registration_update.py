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


# SOURCES[code] -> function() -> {"rep","dem","npa","oth","as_of"} (wired per state)
SOURCES = {"fl": parse_fl, "pa": parse_pa, "nc": parse_nc, "co": parse_co,
           "md": parse_md, "ky": parse_ky, "ne": parse_ne}


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
    doc = {"generated_at": now(), "note": "Statewide voter registration by party; the denominator for the Registration-vs-Turnout comparison. Wired per state from official registration-statistics sources.", "states": out}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    print("registration.json: %d states with party-registration data%s" %
          (len(out), (" (" + ", ".join(sorted(out)) + ")") if out else " — none wired yet"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
