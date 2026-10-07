#!/usr/bin/env python3
"""Ohio county boards of elections' own early-vote data, which runs well ahead
of the Secretary of State's once-a-day dashboard (e.g. on 10/6/26 Cuyahoga's
early in-person report showed 1,911 ballots while the state showed 2
statewide).

Each county publishes in its own way, so each source type has a parser,
registered in PARSERS and listed per county in config/oh.json county_reports
({"parser": ..., "page": ..., parser options}):

  cuyahoga      daily summary PDFs (totals only, no party)
  election_vault  Franklin / Butler "Election Vault" public-records portal:
                absentee-by-mail and in-person voter lists (one row per voter,
                with party), refreshed nightly; fetched by the portal's own
                download postback
  avlist_xls    ES&S / DIMS "Absent Voter Details" spreadsheet exports
                (Morrow, Crawford, Henry), one row per absentee voter with
                category/org, returned date and party
  trumbull_csv  Trumbull's absentee voter download (CSV, one row per voter)
  sheet_returned  Hancock's returned-ballots list (Google Sheets CSV export)

Voter-level files are reduced to counts in memory: no names, addresses or IDs
are kept, logged or written anywhere.

A parser returns any of these, each either a number (no party) or a party
dict {"rep","dem","oth","npa"}:
  requested     absentee-by-mail requests / applications to date
  sent          mail ballots sent to date
  mail_returned valid mail ballots returned to date
  eip           early in-person ballots cast to date
plus  as_of     the data's own time stamp ("YYYY-MM-DD HH:MM", Ohio time)
      label     how the source is described on the site
      stamp     a change marker (file stamp / URL) so unchanged files are
                not re-downloaded on the next check
"""
import csv
import html as H
import http.cookiejar
import io
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import common as C

PS = ("rep", "dem", "oth", "npa")
PARTY = {"r": "rep", "rep": "rep", "republican": "rep",
         "d": "dem", "dem": "dem", "democrat": "dem", "democratic": "dem",
         "u": "npa", "np": "npa", "nopty": "npa", "unaffiliated": "npa", "--": "npa", "non": "npa"}
IN_PERSON = {"OFF", "EV", "EVOFF", "OFFICE", "INP", "IP"}


def _zero():
    return {p: 0 for p in PS}


def _party(v):
    return PARTY.get(str(v or "").strip().lower(), "oth")


def _et(dt_utc):
    """UTC -> Ohio clock time (EDT until the first Sunday of November)."""
    y = dt_utc.year
    nov1 = datetime(y, 11, 1)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7, hours=6)
    return dt_utc - timedelta(hours=4 if dt_utc < dst_end else 5)


def _footer_stamp(texts):
    """'10/6/2026   3:20:41PM' (report footers) -> 'YYYY-MM-DD HH:MM'."""
    for t in texts:
        m = re.search(r"(\d{1,2}/\d{1,2}/20\d{2})\s+(\d{1,2}:\d{2}:\d{2})\s*([AP]M)", t or "")
        if m:
            return datetime.strptime("%s %s%s" % m.groups(), "%m/%d/%Y %I:%M:%S%p").strftime("%Y-%m-%d %H:%M")
    return ""


def _links(url, h):
    for href, txt in re.findall(r'<a[^>]+href\s*=\s*"([^"#]+)"[^>]*>(.*?)</a>', h, re.S | re.I):
        yield urllib.parse.urljoin(url, H.unescape(href).strip()), re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", H.unescape(txt))).strip()


def _quote(url):
    return urllib.parse.quote(url, safe=":/?&=%#+,;~")


# --------------------------------------------------------------- Cuyahoga PDFs
def _pdf_text(url):
    import pypdf
    raw = C.http_get(url, binary=True, no_cache=True, retries=2)
    return "\n".join(p.extract_text(extraction_mode="layout") for p in pypdf.PdfReader(io.BytesIO(raw)).pages)


def _num(text, label):
    m = re.search(re.escape(label) + r"\s*:?\s*([\d,]+)", text)
    return C.parse_number(m.group(1)) if m else None


def _stamp(text):
    """The report header's date and time (layout puts them on separate lines)."""
    d = re.search(r"(\d{1,2}/\d{1,2}/20\d{2})", text)
    t = re.search(r"(\d{1,2}:\d{2}:\d{2})\s*([AP]M)", text)
    if not (d and t):
        return ""
    return datetime.strptime("%s %s%s" % (d.group(1), t.group(1), t.group(2)), "%m/%d/%Y %I:%M:%S%p").strftime("%Y-%m-%d %H:%M")


def cuyahoga(cfg, prev):
    """Cuyahoga BOE 'current election' page: Vote-by-Mail Daily Update and
    Early In-Person Daily Update PDFs (stable paths; the ?sfvrsn suffix only
    versions them). Totals only -- no party."""
    vbm, eip = _pdf_text(cfg["vbm_pdf"]), _pdf_text(cfg["eip_pdf"])
    out = {"requested": _num(vbm, "Applications Processed to Date"),
           "mail_returned": _num(vbm, "Valid Ballots Returned to Date"),
           "eip": _num(eip, "EIP Ballots Issued and Cast to Date")}
    stamps = [s for s in (_stamp(vbm), _stamp(eip)) if s]
    out["as_of"] = max(stamps) if stamps else ""
    out["as_of_by_report"] = {"vote-by-mail": _stamp(vbm), "early in person": _stamp(eip)}
    if all(v is None for k, v in out.items() if k in ("requested", "mail_returned", "eip")):
        raise RuntimeError("Cuyahoga: no figures found in the daily reports")
    return out


# --------------------------------------------------- Election Vault (Franklin, Butler)
def _vault_open(page):
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=C._SSL_CTX))
    op.addheaders = [("User-Agent", C.UA if hasattr(C, "UA") else "Mozilla/5.0")]
    return op, op.open(page, timeout=90).read().decode("utf-8", "replace")


def _vault_files(h):
    """{file name: (download control id, 'last updated' text)} from the grid."""
    out = {}
    for m in re.finditer(r"<span>([^<]+)</span></a><script[^>]*>.*?'uniqueID':'([^']+)'", h, re.S | re.I):
        name = H.unescape(m.group(1))
        tail = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h[m.end():m.end() + 3000]))
        when = re.search(r"([A-Z][a-z]+day, [A-Z][a-z]+ \d{1,2}, 20\d{2} \d{1,2}:\d{2} [AP]M)", tail)
        out[name] = (m.group(2), when.group(1) if when else "")
    return out


def _vault_download(op, page, h, control):
    fields = {}
    for tag in re.findall(r"<input[^>]+type=\"hidden\"[^>]*>", h, re.I):
        n, v = re.search(r'name="([^"]+)"', tag), re.search(r'value="([^"]*)"', tag)
        if n:
            fields[n.group(1)] = H.unescape(v.group(1)) if v else ""
    fields.update({"__EVENTTARGET": control, "__EVENTARGUMENT": ""})
    req = urllib.request.Request(page, data=urllib.parse.urlencode(fields).encode(), headers={"Referer": page})
    return op.open(req, timeout=300).read()


def _dict_rows(raw, name):
    text = raw.decode("latin-1")
    return csv.DictReader(io.StringIO(text), delimiter="\t" if name.lower().endswith(".txt") else ",")


def election_vault(cfg, prev):
    """cfg: page, mail_file, in_person_file, cols {party, mailed, returned,
    status}, valid_status (status values that count a returned ballot as
    valid; default '' and 'VAL'), in_person_voted (column that must be set)."""
    page = cfg["page"]
    op, h = _vault_open(page)
    files = _vault_files(h)
    want = [cfg["mail_file"], cfg.get("in_person_file")]
    missing = [f for f in want if f and f not in files]
    if missing:
        raise RuntimeError("Election Vault: %s not listed" % ", ".join(missing))
    stamp = {f: files[f][1] for f in want if f}
    if prev and prev.get("stamp") == stamp:
        return prev
    col = cfg["cols"]
    valid = set(cfg.get("valid_status", ["", "VAL"]))
    req, sent, ret = _zero(), _zero(), _zero()
    for r in _dict_rows(_vault_download(op, page, h, files[cfg["mail_file"]][0]), cfg["mail_file"]):
        p = _party(r.get(col["party"]))
        req[p] += 1
        if (r.get(col["mailed"]) or "").strip():
            sent[p] += 1
        if (r.get(col["returned"]) or "").strip() and (r.get(col["status"]) or "").strip().upper() in valid:
            ret[p] += 1
    out = {"requested": req, "sent": sent, "mail_returned": ret}
    if cfg.get("in_person_file"):
        eip, must = _zero(), cfg.get("in_person_voted")
        for r in _dict_rows(_vault_download(op, page, h, files[cfg["in_person_file"]][0]), cfg["in_person_file"]):
            if not must or (r.get(must) or "").strip():
                eip[_party(r.get(col["party"]))] += 1
        out["eip"] = eip
    stamps = []
    for when in stamp.values():
        try:
            stamps.append(datetime.strptime(when, "%A, %B %d, %Y %I:%M %p").strftime("%Y-%m-%d %H:%M"))
        except ValueError:
            pass
    out["as_of"] = min(stamps) if stamps else ""
    out["stamp"] = stamp
    return out


# ------------------------------------------ ES&S / DIMS "Absent Voter Details" .xls
def _avlist_counts(rows):
    """Count an Absent Voter Details sheet: header row with 'Party' and
    'Returned'; column 0 = category, column 1 = org (MAIL/EMA/OFF/EV...).
    In-person (org or category in IN_PERSON) with a returned date -> eip;
    everything else is a mail request, returned when dated."""
    hdr = next((i for i, r in enumerate(rows)
                if any(isinstance(v, str) and v.strip() == "Party" for v in r)
                and any(isinstance(v, str) and v.strip() == "Returned" for v in r)), None)
    if hdr is None:
        raise RuntimeError("no 'Party'/'Returned' header row")
    cols = {v.strip(): i for i, v in enumerate(rows[hdr]) if isinstance(v, str)}
    pc, rc = cols["Party"], cols["Returned"]
    req, ret, eip = _zero(), _zero(), _zero()
    n = 0
    for r in rows[hdr + 1:]:
        if len(r) <= pc or not isinstance(r[pc], str) or not re.fullmatch(r"[A-Z\-]{1,6}", r[pc].strip()):
            continue
        cat = (r[0] or "").strip().upper() if isinstance(r[0], str) else ""
        org = (r[1] or "").strip().upper() if len(r) > 1 and isinstance(r[1], str) else ""
        if not (cat or org):
            continue
        p = _party(r[pc])
        returned = len(r) > rc and isinstance(r[rc], float) and r[rc] > 40000
        n += 1
        if org in IN_PERSON or cat in IN_PERSON:
            if returned:
                eip[p] += 1
            continue
        req[p] += 1
        if returned:
            ret[p] += 1
    if not n:
        raise RuntimeError("no voter rows recognised")
    texts = [v for r in rows[-80:] + rows[:12] for v in r if isinstance(v, str)]
    return {"requested": req, "mail_returned": ret, "eip": eip, "as_of": _footer_stamp(texts)}


def _find_link(page, pattern, pick="last"):
    h = C.http_get(page, retries=2, timeout=60, no_cache=True)
    hits = [(u, t) for u, t in _links(page, h) if re.search(pattern, u + " " + t, re.I)]
    if not hits:
        # some CMSs put the link in unquoted / single-quoted attributes
        hits = [(urllib.parse.urljoin(page, H.unescape(u)), "") for u in
                re.findall(r"""href\s*=\s*['"]?([^'" >]+)""", h, re.I) if re.search(pattern, H.unescape(u), re.I)]
    if not hits:
        raise RuntimeError("no link matching %r on %s" % (pattern, page))
    return hits, h


def avlist_xls(cfg, prev):
    """cfg: page, link (regex for the spreadsheet link on that page). The newest
    link wins (highest DocumentCenter id, else the last on the page)."""
    import xls_lite
    hits, _ = _find_link(cfg["page"], cfg["link"])

    def key(ut):
        m = re.search(r"/View/(\d+)/", ut[0])
        return int(m.group(1)) if m else 0
    url = max(hits, key=key)[0] if any(key(x) for x in hits) else hits[-1][0]
    url = _quote(url)
    day_files = _daily_returned(cfg) if cfg.get("returned_pdfs") else {}
    stamp = [url] + sorted(u for _, u in day_files.values())
    if prev and prev.get("stamp") == stamp:
        return prev
    sheets = xls_lite.read_xls(C.http_get(url, binary=True, retries=2, timeout=120, no_cache=True))
    rows = max(sheets, key=lambda s: len(s[1]))[1]
    out = _avlist_counts(rows)
    if day_files:
        out["eip"] = _in_person_from(day_files)
    out["stamp"] = stamp
    return out


def _daily_returned(cfg):
    """Henry posts its cumulative list without in-person voters, plus one
    'Returned' PDF per day (every category). -> {day: (doc id, url)}, the
    newest file per day if a day is re-posted."""
    hits, _ = _find_link(cfg["page"], cfg["returned_pdfs"])
    by_day = {}
    for u, t in hits:
        m = re.search(r"/View/(\d+)/(\d{7,8})", u)
        if m and int(m.group(1)) > by_day.get(m.group(2), (0, ""))[0]:
            by_day[m.group(2)] = (int(m.group(1)), u)
    return by_day


def _in_person_from(day_files):
    """In-person (org OFF) rows by party, summed over the daily PDFs."""
    import pypdf
    eip = _zero()
    for _, u in day_files.values():
        raw = C.http_get(u, binary=True, retries=2, timeout=120)
        for page in pypdf.PdfReader(io.BytesIO(raw)).pages:
            for line in page.extract_text(extraction_mode="layout").splitlines():
                toks = re.split(r"\s{2,}", line.strip())
                if len(toks) < 4 or toks[1].upper() not in IN_PERSON:
                    continue
                party = next((t for t in toks[2:] if t.upper() in ("DEM", "REP", "NOPTY", "LIB", "GRN", "NP")), None)
                if party:
                    eip[_party(party)] += 1
    return eip


# ------------------------------------------------------------ Trumbull CSV
def trumbull_csv(cfg, prev):
    """Report-style CSV: every row repeats the 9 field labels, then the values
    (care-of, name, address, city, country, PCT, party, category, issued,
    returned, voter id, state id). The party column is located by its codes;
    category and returned follow it."""
    url = cfg["csv"]
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, context=C._SSL_CTX, timeout=60) as r:
        lm = r.headers.get("Last-Modified", "")
    if prev and lm and prev.get("stamp") == lm:
        return prev
    rows = list(csv.reader(io.StringIO(C.http_get(url, binary=True, retries=2, timeout=120).decode("latin-1"))))
    rows = [r for r in rows if len(r) > 12]
    if not rows:
        raise RuntimeError("Trumbull: empty file")
    codes = {"--", "D", "R", "L", "G", "N", "U"}
    width = min(len(r) for r in rows)
    pc = next((i for i in range(9, width) if sum(1 for r in rows if r[i].strip() in codes) > 0.9 * len(rows)), None)
    if pc is None:
        raise RuntimeError("Trumbull: party column not found")
    cc, rc = pc + 1, pc + 3
    req_, ret, eip = _zero(), _zero(), _zero()
    for r in rows:
        p = _party(r[pc])
        returned = bool(re.match(r"\d{1,2}/\d{1,2}/20\d{2}", r[rc].strip()))
        if r[cc].strip().upper() in IN_PERSON:
            if returned:
                eip[p] += 1
            continue
        req_[p] += 1
        if returned:
            ret[p] += 1
    out = {"requested": req_, "mail_returned": ret, "stamp": lm}
    if sum(eip.values()):
        out["eip"] = eip
    if lm:
        out["as_of"] = _et(datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S GMT")).strftime("%Y-%m-%d %H:%M")
    return out


# ----------------------------------------------------- Hancock returned list
def sheet_returned(cfg, prev):
    """Hancock 'Absentees by mail returned' list (Google Sheets CSV export):
    one row per returned ballot with PARTY: and TYPE:."""
    rows = list(csv.DictReader(io.StringIO(C.http_get(cfg["csv"], retries=2, timeout=60, no_cache=True))))
    ret, eip = _zero(), _zero()
    for r in rows:
        r = {k.strip().rstrip(":").upper(): (v or "").strip() for k, v in r.items() if k}
        if not r.get("RETURN DATE"):
            continue
        p = _party(r.get("PARTY"))
        (eip if r.get("TYPE", "").upper() in IN_PERSON else ret)[p] += 1
    out = {"mail_returned": ret}
    if sum(eip.values()):
        out["eip"] = eip
    return out


PARSERS = {"cuyahoga": cuyahoga, "election_vault": election_vault, "avlist_xls": avlist_xls,
           "trumbull_csv": trumbull_csv, "sheet_returned": sheet_returned}


def fetch_all(reports_cfg, prev=None):
    """-> {county: figures}; a county whose source fails keeps its previous
    figures (or is left out), so the state's data stands."""
    prev = prev or {}
    out = {}
    for county, cfg in (reports_cfg or {}).items():
        if county.startswith("_"):
            continue
        fn = PARSERS.get(cfg.get("parser", county.lower()))
        if not fn:
            continue
        try:
            got = fn(cfg, prev.get(county))
            out[county] = dict(got, url=cfg.get("page", ""), label=cfg.get("label", "%s County BOE reports" % county))
        except Exception as e:  # noqa: BLE001
            print("OH: %s county data unavailable: %s" % (county, str(e)[:160]), file=sys.stderr)
            if county in prev:
                out[county] = prev[county]
    return out
