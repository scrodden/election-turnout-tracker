#!/usr/bin/env python3
"""Texas early voters by last party primary, built from official public rosters
(our replacement for theoelections' party split, whose data files closed to
automated access on 2026-10-22). Every county, two ways:

  * the state's rosters (all counties but those in COUNTIES): the Secretary of
    State's turnout app serves, per election and early-voting day, every
    county's roster of that day's voters -- VUID, voting method, precinct (its
    "voter details" CSV buttons). Primary history = the 2026 March primaries
    and May runoffs (final, fetched once); this election = the general's day
    files, fetched as each day's report appears. 2024's primary rosters are not
    available statewide: the SoS's older site (earlyvoting.texas-election.com)
    still lists those elections but no longer offers their voter files.
  * a county's own rosters (COUNTIES -- Harris): its official 2024 and 2026
    primary and runoff rosters (county voter IDs, so they can't be joined to the
    state's VUIDs) and its cumulative early-voting and mail rosters, which also
    run ahead of the state's.
The last primary a voter took part in gives their party (D / R); none = no
primary in the span. Congressional districts: each precinct's district from the
Texas Legislative Council's 2026 precinct file (Precincts26P_Districts, plan
PLANC2333 -- assets/tx-precinct-cd.json); Harris's from its rosters.

Voter-level files and the join (a SQLite file) stay in LOCAL_DIR, outside the
repo; only totals are written: data/tx/roster_party.json (per county: voters by
party, by method and by congressional district, plus the statewide day-by-day
series), data/tx/districts.json and data/tx/party_share.json.

Runs on Sam's PC from the Georgia reader's scheduled task (local_ga_reader), or
by hand:  python scripts/tx_rosters.py [--rebuild] [--force]
"""
import binascii
import csv
import io
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

LOCAL_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects", "Politics", "tx-rosters")
OUT_PATH = os.path.join(ROOT, "data", "tx", "roster_party.json")
OUT_V = 2   # roster_party.json layout (2: every county, statewide series)
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

COUNTIES = {
    "Harris": {
        "base": "https://appfiles.harrisvotes.com/harrisvotes/prd/Reports/",
        "page": "https://app.harrisvotes.com/ElectionResults/ElectionRosters",
        # oldest first: a later primary overrides an earlier one
        "primaries": [("2024-03", "0324 Official Voter Rosters.zip"), ("2024-05", "0524_VoterHistory_Both Parties.xlsx"),
                      ("2026-03", "0326_OfficialRoster.zip"), ("2026-05", "0526_OfficialRoster.zip")],
        "general": {"inp": "Cumulative_EV_ToPost_1126.zip", "mail": "Cumulative_BBM_ToPost_1126.zip"},
    },
}
SPAN = {"state": "2026", "Harris": "2024-2026"}   # primaries (with their runoffs) behind each route's party


def _col(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _rows(x, sheet):
    """Yield each row of a sheet as {column index: value}; a shared-string cell's value is its index (int),
    any other cell's value its text (str)."""
    with x.open(sheet) as f:
        for _, el in ET.iterparse(f):
            if el.tag != NS + "row":
                continue
            cells = {}
            for c in el.findall(NS + "c"):
                v, isn = c.find(NS + "v"), c.find(NS + "is")
                if isn is not None:
                    cells[_col(c.get("r"))] = "".join(t.text or "" for t in isn.iter(NS + "t"))
                elif v is not None:
                    cells[_col(c.get("r"))] = int(v.text) if c.get("t") == "s" else v.text
            el.clear()
            yield cells


def xlsx_columns(xbytes, wanted):
    """Stream a big .xlsx sheet and return {header: [values...]} for the `wanted` headers only (matched
    case-insensitively). Shared strings are resolved afterwards for just the cells kept, so a
    multi-million-row roster never loads every name and address."""
    x = zipfile.ZipFile(io.BytesIO(xbytes))
    sheet = sorted(n for n in x.namelist() if n.startswith("xl/worksheets/sheet"))[0]
    want = {w.lower() for w in wanted}
    rows = _rows(x, sheet)
    header = next(rows, {})
    hs = _shared_strings(x, {v for v in header.values() if isinstance(v, int)})
    names = {j: str(hs.get(v, "") if isinstance(v, int) else v).strip() for j, v in header.items()}
    pick = {j: n for j, n in names.items() if n.lower() in want}
    out = {n: [] for n in pick.values()}
    for cells in rows:
        for j, n in pick.items():
            out[n].append(cells.get(j, ""))
    needed = {v for vals in out.values() for v in vals if isinstance(v, int)}
    if needed:
        strings = _shared_strings(x, needed)
        for n in out:
            out[n] = [strings.get(v, "") if isinstance(v, int) else v for v in out[n]]
    return out


def _shared_strings(x, needed):
    out, i = {}, 0
    if "xl/sharedStrings.xml" not in x.namelist():
        return out
    with x.open("xl/sharedStrings.xml") as f:
        for _, el in ET.iterparse(f):
            if el.tag == NS + "si":
                if i in needed:
                    out[i] = "".join(t.text or "" for t in el.iter(NS + "t"))
                i += 1
                el.clear()
    return out


def fetch(url, path=None, need=True):
    """GET (cached on disk when `path` is given and the server's Last-Modified hasn't changed; with need=False
    an unchanged file isn't read back -- its data comes back None)."""
    req = urllib.request.Request(url, headers={"User-Agent": C.USER_AGENT})
    if path and os.path.exists(path) and os.path.exists(path + ".lm"):
        req.add_header("If-Modified-Since", open(path + ".lm").read().strip())
    try:
        with urllib.request.urlopen(req, timeout=900, context=C._SSL_CTX) as r:
            data, lm = r.read(), r.headers.get("Last-Modified", "")
    except urllib.error.HTTPError as e:
        if e.code == 304 and path:
            return (open(path, "rb").read() if need else None), open(path + ".lm").read().strip(), False
        raise
    if path:
        with open(path, "wb") as f:
            f.write(data)
        with open(path + ".lm", "w") as f:
            f.write(lm)
    return data, lm, True


def roster_tables(data, name):
    """A roster file (xlsx, or zip of xlsx/csv parts) -> list of column dicts."""
    if name.lower().endswith(".xlsx"):
        return [data]
    z = zipfile.ZipFile(io.BytesIO(data))
    return [z.read(n) for n in sorted(z.namelist()) if n.lower().endswith(".xlsx") and "location" not in n.lower()
            and ("voter" in n.lower() or "roster" in n.lower())]


def build_primary_lookup(county, spec, force=False):
    """{county ID: "D"/"R"} by last primary, and {precinct: congressional district} from the 2026 rosters.
    Cached in LOCAL_DIR/<county>_primaries.json; rebuilt when asked or when a roster file changes."""
    os.makedirs(LOCAL_DIR, exist_ok=True)
    out_path = os.path.join(LOCAL_DIR, "%s_primaries.json" % county.lower())
    cache = {} if force else (json.load(open(out_path, encoding="utf-8")) if os.path.exists(out_path) else {})
    stamps, last, prec_cd = {}, {}, {}
    changed = not cache or sorted(cache.get("stamps") or {}) != sorted(c for c, _ in spec["primaries"])
    files = []
    for code, fname in spec["primaries"]:
        local = os.path.join(LOCAL_DIR, "%s_%s" % (county.lower(), fname.replace(" ", "_")))
        data, lm, new = fetch(spec["base"] + urllib.parse.quote(fname), local, need=False)
        stamps[code] = lm
        changed = changed or new or (cache.get("stamps", {}).get(code) != lm)
        files.append((code, fname, data, local))
    if not changed:
        return cache
    files = [(code, fname, data if data is not None else open(local, "rb").read()) for code, fname, data, local in files]
    counts = {}
    for code, fname, data in files:
        n = 0
        for part in roster_tables(data, fname):
            cols = xlsx_columns(part, ["IDNumber", "IDNUMBER", "Party", "PARTY_CODE", "Precinct", "PRECINCT", "Dist02"])
            ids = cols.get("IDNumber") or cols.get("IDNUMBER") or []
            party = cols.get("Party") or cols.get("PARTY_CODE") or []
            prec, cd = cols.get("Precinct") or cols.get("PRECINCT") or [], cols.get("Dist02") or []
            if not ids or len(party) != len(ids):
                raise ValueError("%s %s: no ID/party columns" % (county, fname))
            for i, vid in enumerate(ids):
                p = str(party[i]).strip().upper()[:1]
                if vid and p in ("D", "R"):
                    last[str(vid).strip().lstrip("0")] = p
                    n += 1
                if code.startswith("2026") and i < len(cd) and i < len(prec):
                    m = re.search(r"CONGRESSIONAL DIST\s*0*(\d+)", str(cd[i]))
                    if m:
                        k = str(prec[i]).strip().zfill(4)
                        c = prec_cd.setdefault(k, {})
                        c[m.group(1)] = c.get(m.group(1), 0) + 1
        counts[code] = n
        print("tx rosters: %s %s -> %d primary voters" % (county, code, n))
    cache = {"built": C.utc_now_iso(), "stamps": stamps, "counts": counts, "last": last,
             "precinct_cd": {k: max(v, key=v.get) for k, v in prec_cd.items()},
             "precinct_cd_split": sorted(k for k, v in prec_cd.items() if len(v) > 1)}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"))
    return cache


def general_voters(county, spec):
    """{county ID: (method, precinct)} from the 2026 general's cumulative rosters (in person wins if a
    voter is on both, e.g. a mail ballot surrendered at the polls) and the files' Last-Modified stamps."""
    voters, stamps = {}, {}
    for method in ("mail", "inp"):
        fname = spec["general"][method]
        local = os.path.join(LOCAL_DIR, "%s_%s" % (county.lower(), fname))
        data, lm, _ = fetch(spec["base"] + urllib.parse.quote(fname), local)
        stamps[method] = lm
        z = zipfile.ZipFile(io.BytesIO(data))
        for n in sorted(z.namelist()):
            if not n.lower().endswith(".csv"):
                continue
            rd = csv.DictReader(io.StringIO(z.read(n).decode("utf-8-sig", "replace")))
            for r in rd:
                vid = str(r.get("IDNumber") or "").strip().lstrip("0")
                if vid:
                    voters[vid] = (method, str(r.get("Precinct") or "").strip().zfill(4), r.get("ActivityDate", ""))
    return voters, stamps


def _z():
    return {"voters": 0, "dem": 0, "rep": 0, "none": 0}


class Tally:
    """One county's voters by party: in all, by method (mail / inp), by congressional district (each with its
    own mail / inp) and by day (ISO date -> [voters, dem, rep, none])."""
    PIDX = {"dem": 1, "rep": 2, "none": 3}

    def __init__(self):
        self.tot, self.by_m, self.by_cd, self.unmapped, self.days = _z(), {"mail": _z(), "inp": _z()}, {}, 0, {}

    def add(self, party, method, cd, day, n=1):
        if cd:
            d = self.by_cd.setdefault(str(cd), dict(_z(), mail=_z(), inp=_z()))
        else:
            self.unmapped += n
        for b in (self.tot, self.by_m[method]) + ((d, d[method]) if cd else ()):
            b["voters"] += n
            b[party] += n
        if day:
            a = self.days.setdefault(day, [0, 0, 0, 0])
            a[0] += n
            a[self.PIDX[party]] += n

    def record(self, **extra):
        return dict(self.tot, mail=self.by_m["mail"], inp=self.by_m["inp"], cd=self.by_cd, cd_unmapped=self.unmapped,
                    days=dict(sorted(self.days.items())), **extra)


def tally(voters, lookup):
    t = Tally()
    last, pcd = lookup["last"], lookup["precinct_cd"]
    for vid, (method, prec, when) in voters.items():
        try:
            day = datetime.strptime(when, "%m/%d/%Y").strftime("%Y-%m-%d") if when else ""
        except ValueError:
            day = ""
        t.add({"D": "dem", "R": "rep"}.get(last.get(vid), "none"), method, pcd.get(prec), day)
    through = max(t.days) if t.days else ""
    return t, ("%s/%s/%s" % (through[5:7], through[8:10], through[:4]) if through else "")


# ---- every other county: the Secretary of State's statewide rosters ----
# The SoS turnout app (SOS_PAGE) reads getFile?type=EVR_STATEWIDE&electionId=&electionDate=MM/DD/YYYY: JSON whose
# "upload" is a base64 CSV of that day's voters in every county -- COUNTY, VOTER_NAME, ID_VOTER (the statewide
# VUID), VOTING_METHOD (IN-PERSON / MAIL-IN), PRECINCT (mail ballots under the day the county logged them). For the
# finished 2026 primaries each day's file runs cumulative; type=EVR_STATEWIDE_ELECTIONDAY is a zip with election
# day's (STATEWIDE_VOTER_INFO.csv). Measured 10/22: the general's 10/14-10/21 files held 2,109,600 voters against
# the report's 2,227,031 ballots (mail logged before 10/14 isn't in any day's file; Dallas's rosters lag its count).
SOS_API = "https://goelect.txelections.civixapps.com/api-ivis-system/api/v1/getFile?"
SOS_PAGE = "https://goelect.txelections.civixapps.com/ivis-evr-ui/evr"
SOS_DIR = os.path.join(LOCAL_DIR, "sos")
SOS_DB = os.path.join(SOS_DIR, "tx_rosters.sqlite")
# oldest first, a later one overriding: the March 3 primaries, then the May 26 runoffs
SOS_PRIMARIES = [(53813, "R", "2026 Republican primary"), (53814, "D", "2026 Democratic primary"),
                 (58315, "R", "2026 Republican runoff"), (58314, "D", "2026 Democratic runoff")]
SOS_REFETCH_DAYS = 3   # the general's latest days are fetched again whenever the state's report is updated
PCT_CD = os.path.join(ROOT, "assets", "tx-precinct-cd.json")
TLC_PAGE = "https://data.capitol.texas.gov/dataset/precincts"
TLC_XLS = ("https://data.capitol.texas.gov/dataset/d04c72b9-16c4-4ab2-8c6d-c666d41e04b7/resource/"
           "58899c7b-a3ba-4cea-b70c-524fbd3920af/download/precincts26p_districts.xls")


def sos_json(**params):
    j = json.loads(C.http_get(SOS_API + urllib.parse.urlencode(params), no_cache=True, retries=3, timeout=120))
    return json.loads(binascii.a2b_base64(j["upload"]).decode("utf-8").strip()) if j.get("upload") else None


def sos_download(path, **params):
    """Stream one roster file to `path`, decoding the base64 as it arrives (the general's day files run to tens
    of MB, so the JSON is never held whole). Three tries: the app 502s under load."""
    tmp = path + ".part"
    for attempt in range(3):
        try:
            req = urllib.request.Request(SOS_API + urllib.parse.urlencode(params), headers={"User-Agent": C.USER_AGENT})
            with urllib.request.urlopen(req, timeout=900, context=C._SSL_CTX) as r, open(tmp, "wb") as out:
                head = b""
                while b'"upload":"' not in head:
                    chunk = r.read(65536)
                    if not chunk:
                        raise ValueError("no file in the response (%r)" % head[:120])
                    head += chunk
                rest, carry, closed = head.split(b'"upload":"', 1)[1], b"", False
                while rest:
                    end = rest.find(b'"')
                    if end >= 0:
                        rest, closed = rest[:end], True
                    buf = carry + rest
                    k = len(buf) // 4 * 4
                    out.write(binascii.a2b_base64(buf[:k]))
                    carry = buf[k:]
                    rest = b"" if closed else r.read(1 << 20)
                if not closed or carry:
                    raise ValueError("truncated")
            os.replace(tmp, path)
            return
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                raise
            print("tx rosters: SoS file %s: %s -- retrying" % (params, str(e)[:100]))
            time.sleep(15 * (attempt + 1))


def sos_rows(path):
    """-> (COUNTY, VUID int, in person?, precinct) per voter in a SoS roster CSV, or the voter CSV in its zip."""
    if path.endswith(".zip"):
        z = zipfile.ZipFile(path)
        f = io.TextIOWrapper(z.open([n for n in z.namelist() if "VOTER" in n.upper()][0]), encoding="utf-8",
                             errors="replace", newline="")
    else:
        f = open(path, encoding="utf-8", errors="replace", newline="")
    with f:
        rd = csv.reader(f)
        h = [x.strip().upper() for x in next(rd)]
        ci = h.index("COUNTY") if "COUNTY" in h else h.index("COUNTY_NAME")
        vi, mi, pi = h.index("ID_VOTER"), h.index("VOTING_METHOD"), h.index("PRECINCT")
        for r in rd:
            v = r[vi].strip() if len(r) > pi else ""
            if v.isdigit():
                yield r[ci].strip().upper(), int(v), r[mi].strip().upper() == "IN-PERSON", r[pi].strip()


def sos_db():
    os.makedirs(SOS_DIR, exist_ok=True)
    db = sqlite3.connect(SOS_DB)
    db.execute("PRAGMA synchronous=OFF")
    db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS prim (vuid INTEGER PRIMARY KEY, party TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS gen (vuid INTEGER, county TEXT, inp INTEGER, precinct TEXT, day TEXT, "
               "src TEXT DEFAULT 'state')")
    if "src" not in [r[1] for r in db.execute("PRAGMA table_info(gen)")]:   # made before the counties' own lists joined
        db.execute("ALTER TABLE gen ADD COLUMN src TEXT DEFAULT 'state'")
    db.execute("CREATE INDEX IF NOT EXISTS gen_day ON gen (day)")
    return db


def _meta(db, k, v=None):
    if v is None:
        r = db.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return r[0] if r else None
    db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, v))


def sos_primaries(db, elections, rebuild=False):
    """Fill `prim` (VUID -> party of the last 2026 primary or runoff voted in) once. The day files are deleted
    after loading (voter-level, and the join is all that's needed); -> {election: voters}."""
    built = _meta(db, "prim_built")
    if built and not rebuild:
        return json.loads(built)
    db.execute("DELETE FROM prim")
    counts = {}
    for eid, party, label in SOS_PRIMARIES:
        e = elections[eid]
        files = []
        for d in e["early_voting_dates"]:
            p = os.path.join(SOS_DIR, "%d_EV_%s.csv" % (eid, d["date"][:5].replace("/", "")))
            if not os.path.exists(p):
                sos_download(p, type="EVR_STATEWIDE", electionId=eid, electionDate=d["date"])
            files.append(p)
        p = os.path.join(SOS_DIR, "%d_ED.zip" % eid)
        if not os.path.exists(p):
            sos_download(p, type="EVR_STATEWIDE_ELECTIONDAY", electionId=eid, electionDate=e["election_date"])
        files.append(p)
        seen = set()
        for f in files:
            vs = [v for _, v, _, _ in sos_rows(f)]
            seen.update(vs)
            db.executemany("INSERT OR REPLACE INTO prim VALUES (?, ?)", ((v, party) for v in vs))
        counts[label] = len(seen)
        print("tx rosters: state %s -> %d voters" % (label, len(seen)))
    if min(counts.values()) < 1000:
        raise ValueError("primary rosters look incomplete: %s" % counts)
    _meta(db, "prim_built", json.dumps(dict(counts, built=C.utc_now_iso())))
    db.commit()
    for eid, _, _ in SOS_PRIMARIES:
        for f in os.listdir(SOS_DIR):
            if f.startswith("%d_" % eid):
                os.remove(os.path.join(SOS_DIR, f))
    return json.loads(_meta(db, "prim_built"))


def sos_general(db, elec, force=False):
    """Bring `gen` up to date with the general's day files -> (changed, latest day 'MM/DD/YYYY', report date).
    A day's file is fetched when it first appears and again, while it's one of the latest SOS_REFETCH_DAYS days,
    whenever the state's report has been updated since (counties add late rows)."""
    today = (datetime.now(timezone.utc) - timedelta(hours=5)).replace(tzinfo=None)   # Texas (Central) date
    dates = [d["date"] for d in elec.get("early_voting_dates", []) if datetime.strptime(d["date"], "%m/%d/%Y") <= today]
    if not dates:
        return False, "", ""
    rep = sos_json(type="EVR_EARLYVOTING", electionId=elec["id"], electionDate=max(dates, key=lambda d: d[6:] + d[:5]))
    upd, lt = rep.get("date_updated") or "", rep.get("early_voting_date") or ""
    latest = "%s/%s/%s" % (lt[5:7], lt[8:10], lt[:4]) if lt else max(dates)
    have = sorted((d for d in dates if d[6:] + d[:5] <= latest[6:] + latest[:5]), key=lambda d: d[6:] + d[:5])
    changed = False
    for i, d in enumerate(have):
        stamp = _meta(db, "gen:" + d)
        if stamp and not force and (i < len(have) - SOS_REFETCH_DAYS or stamp == upd):
            continue
        p = os.path.join(SOS_DIR, "general_%s.csv" % d.replace("/", ""))
        sos_download(p, type="EVR_STATEWIDE", electionId=elec["id"], electionDate=d)
        day = "%s-%s-%s" % (d[6:], d[:2], d[3:5])
        db.execute("DELETE FROM gen WHERE day = ? AND src = 'state'", (day,))
        db.executemany("INSERT INTO gen (vuid, county, inp, precinct, day) VALUES (?, ?, ?, ?, ?)",
                       ((v, c, int(ip), pr, day) for c, v, ip, pr in sos_rows(p)))
        _meta(db, "gen:" + d, upd)
        db.commit()
        os.remove(p)
        changed = True
        print("tx rosters: state general %s loaded" % d)
    return changed, latest, upd


# ---- counties' own lists that carry the VUID: joined with the state's (a voter on both counts once) ----
# Bexar sends the state almost none of its mail ballots (8 in the Oct 22 report), so its own daily lists fill them in:
# elections.bexar.gov's Document Center folder "11-03 General Election" (245), listed by the folder page's own JSON
# query. PDFs: "Received Mail Ballots - YYYY-MM-DD" (VUID, name, received date, precinct) and "Early Vote Ballots
# -YYYY-MM-DD" (name, VUID, site, precinct); a precinct reads "P 3007", or "S 4007.01" for part of a split precinct
# (split by other districts, not congressional ones -- the state's files and the TLC's use the base number).
VUID_COUNTIES = {
    "BEXAR": {"label": "Bexar County Elections' daily mail and early-vote lists",
              "page": "https://elections.bexar.gov/DocumentCenter/Index/245", "base": "https://elections.bexar.gov",
              "list": "https://elections.bexar.gov/Admin/DocumentCenter/Home/Document_AjaxBinding?renderMode=0&loadSource=7",
              "folder": 245},
}
ROW_RE = re.compile(r"(?<!\d)(\d{6,10})(?!\d).*\s[PS]\s+(\d+)(?:\.\d+)?\s*$")   # a few VUIDs print without leading zeros


def county_docs(spec):
    """-> [(kind 'mail' / 'ev', ISO day, url, stamp)] for the county folder's list PDFs."""
    body = json.dumps({"folderId": spec["folder"], "getDocuments": 1, "imageRepo": False, "renderMode": 0, "loadSource": 7,
                       "searchString": "", "pageNumber": 1, "rowsPerPage": 500, "sortColumn": "Name", "sortOrder": 0})
    req = urllib.request.Request(spec["list"], data=body.encode(), headers={"User-Agent": C.USER_AGENT,
                                                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60, context=C._SSL_CTX) as r:
        docs = json.loads(r.read()).get("Documents") or []
    out = []
    for d in docs:
        m = re.search(r"(Received Mail|Early Vote) Ballots\s*-\s*(\d{4}-\d{2}-\d{2})", d.get("DisplayName") or "")
        if m and d.get("URL"):
            out.append(("mail" if m.group(1).startswith("Received") else "ev", m.group(2), spec["base"] + d["URL"],
                        "%s %s %s" % (d.get("ID"), d.get("LastModifiedDateString"), d.get("FileSize"))))
    return out


def pdf_voters(data):
    """-> [(VUID, base precinct)] from a county list PDF (pypdf, installed on Sam's PC)."""
    from pypdf import PdfReader
    out = []
    for page in PdfReader(io.BytesIO(data)).pages:
        for ln in (page.extract_text() or "").splitlines():
            m = ROW_RE.search(ln.strip())
            if m:
                out.append((int(m.group(1)), m.group(2)))
    return out


def county_lists(db, force=False):
    """Load the VUID_COUNTIES' new or changed list PDFs into `gen` (src '<COUNTY>:<kind>') -> changed?"""
    changed = False
    for county, spec in VUID_COUNTIES.items():
        try:
            docs = county_docs(spec)
        except Exception as e:  # noqa: BLE001
            print("tx rosters: %s's list folder unavailable (%s)" % (county.title(), str(e)[:100]), file=sys.stderr)
            continue
        for kind, day, url, stamp in docs:
            key = "%s:%s:%s" % (county, kind, day)
            if not force and _meta(db, key) == stamp:
                continue
            try:
                data = fetch(url)[0]
                rows = pdf_voters(data)
            except Exception as e:  # noqa: BLE001
                print("tx rosters: %s %s %s unreadable (%s)" % (county.title(), kind, day, str(e)[:100]), file=sys.stderr)
                continue
            if not rows and len(data) > 100000:
                print("tx rosters: %s %s %s: no voter rows read from %d bytes -- skipped"
                      % (county.title(), kind, day, len(data)), file=sys.stderr)
                continue
            src = "%s:%s" % (county, kind)
            db.execute("DELETE FROM gen WHERE src = ? AND day = ?", (src, day))
            db.executemany("INSERT INTO gen (vuid, county, inp, precinct, day, src) VALUES (?, ?, ?, ?, ?, ?)",
                           ((v, county, int(kind == "ev"), pr, day, src) for v, pr in rows))
            _meta(db, key, stamp)
            db.commit()
            changed = True
            print("tx rosters: %s's own %s list %s: %d voters" % (county.title(), kind, day, len(rows)))
    return changed


def _ckey(name):
    return re.sub(r"[^A-Z0-9]", "", str(name).upper())


def _pkey(p):
    """Precinct codes as rosters and the TLC file write them differ in zero-padding and trailing dashes."""
    p = re.sub(r"[\s-]+$", "", str(p).strip().upper())
    return p.lstrip("0") or "0"


def precinct_cds():
    """{COUNTY key: {precinct key: district}} from assets/tx-precinct-cd.json, built from the TLC's file once."""
    if os.path.exists(PCT_CD):
        return json.load(open(PCT_CD, encoding="utf-8"))["counties"]
    import xls_reader
    rows = list(xls_reader.read_xls(fetch(TLC_XLS)[0]).values())[0]
    h = [str(x) for x in rows[0]]
    ci, pi, di = h.index("FENAME"), h.index("PREC"), h.index("PlanC2333")
    out = {}
    for r in rows[1:]:
        if r[di] is not None:
            out.setdefault(_ckey(r[ci]), {})[_pkey(r[pi])] = int(r[di])
    assert len(out) == 254 and sum(len(v) for v in out.values()) > 9000, "TLC precinct file: %d counties" % len(out)
    with open(PCT_CD, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"source": "Texas Legislative Council, Capitol Data Portal: Precincts26P_Districts.xls (2026 primary "
                             "precincts and their districts; congressional plan PLANC2333)", "url": TLC_PAGE,
                   "counties": out}, f, separators=(",", ":"))
    return out


def sos_tally(db, skip):
    """Every county's Tally from the state's rosters (counties in `skip` left out) -> {COUNTY: Tally}."""
    pcd = precinct_cds()
    one = {c: next(iter(set(m.values()))) for c, m in pcd.items() if len(set(m.values())) == 1}
    q = ("SELECT county, inp, precinct, day, COALESCE(p.party, 'N'), COUNT(*) FROM "
         "(SELECT vuid, county, inp, precinct, day, ROW_NUMBER() OVER (PARTITION BY vuid ORDER BY inp DESC, day) AS rn "
         "FROM gen) g LEFT JOIN prim p USING (vuid) WHERE rn = 1 GROUP BY 1, 2, 3, 4, 5")
    out = {}
    for county, inp, prec, day, party, n in db.execute(q):
        if county in skip:
            continue
        k = _ckey(county)
        cd = one.get(k) or (pcd.get(k) or {}).get(_pkey(prec))
        out.setdefault(county, Tally()).add({"D": "dem", "R": "rep"}.get(party, "none"), "inp" if inp else "mail", cd, day, n)
    return out


DIST_PATH = os.path.join(ROOT, "data", "tx", "districts.json")
SHARE_PATH = os.path.join(ROOT, "data", "tx", "party_share.json")
CD_GEO = os.path.join(ROOT, "assets", "tx-cd.geojson")
SPAN_TEXT = "the 2026 primaries and May runoffs (Harris County: 2024 and 2026, from its own rosters)"


def _blk(t):
    return C.party_block(t["rep"], t["dem"], 0, t["none"])


def _mdy(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %-d" if os.name != "nt" else "%b %#d") if iso else "?"


def write_districts(counties, through):
    """data/tx/districts.json for the Texas page's congressional-district view: early voters on the rosters in
    each district, by party and method."""
    geo = json.load(open(CD_GEO, encoding="utf-8"))
    gidx = {f["properties"]["district_number"]: f["properties"] for f in geo["features"]}
    sums = {}
    for name, c in counties.items():
        for cd, t in (c.get("cd") or {}).items():
            a = sums.setdefault(int(cd), {"all": _z(), "mail": _z(), "inp": _z()})
            for k, src in (("all", t), ("mail", t.get("mail") or {}), ("inp", t.get("inp") or {})):
                for f in ("voters", "dem", "rep", "none"):
                    a[k][f] += src.get(f, 0)
    units = {}
    for num, a in sorted(sums.items()):
        if num not in gidx:
            continue
        ent = {"fips": gidx[num]["fips"], "cast": _blk(a["all"]), "registered": 0, "turnout_pct": None}
        if a["mail"]["voters"]:
            ent["mail_voted"] = _blk(a["mail"])
        if a["inp"]["voters"]:
            ent["early_voted"] = _blk(a["inp"])
        units[gidx[num]["name"]] = ent

    def tot(key):
        t = _z()
        for a in sums.values():
            for f in t:
                t[f] += a[key][f]
        return _blk(t)
    unplaced = sum(c.get("cd_unmapped", 0) for c in counties.values())
    voters = sum(c.get("voters", 0) for c in counties.values())
    iso = "%s-%s-%s" % (through[6:], through[:2], through[3:5]) if len(through) == 10 else ""
    body = {"state": "tx", "unit_label": "Congressional District", "unit_label_plural": "Congressional Districts",
            "partisan": True, "party_methods": ["cast", "mail_voted", "early_voted"],
            "party_basis": "the last party primary each voter voted in (%s)" % SPAN_TEXT,
            "source": "Official rosters: the Texas Secretary of State's daily early-voting and mail voter files for every "
                      "county and its 2026 primary and runoff voter files, Harris County's own rosters, matched voter by "
                      "voter; districts from the Texas Legislative Council's 2026 precinct file (plan PLANC2333)",
            "methods_present": ["mail_voted", "early_voted"],
            "method_labels": {"cast": "Ballots cast", "mail_voted": "Mail ballots", "early_voted": "Voted early in person"},
            "party_labels": {"rep": "Republican primary voters", "dem": "Democratic primary voters", "npa": "No recent primary"},
            "party_hints": {"cast": "Early voters on the official rosters by the last party primary they voted in (%s). "
                                    "No recent primary = none in that span, including new voters." % SPAN_TEXT},
            "coverage": {"ok": len(units), "total": len(gidx), "unmatched": []}, "partial": len(units) < len(gidx),
            "as_of": through, "statewide": {"cast": tot("all"), "mail_voted": tot("mail"), "early_voted": tot("inp")},
            "counties": units,
            "map_note": ("Early voters on the official rosters through %s (%s), by congressional district (the 2025 map) "
                         "and the last party primary each voted in: %s. No recent primary = none in that span, including "
                         "new voters. The rosters are the Secretary of State's daily voter files, which run a day behind "
                         "the county map's totals, and Harris County's own; each voter's district is their precinct's, from "
                         "the Texas Legislative Council's 2026 precinct file (%s voters whose precinct couldn't be placed "
                         "are left out)." % (_mdy(iso), format(voters, ","), SPAN_TEXT, format(unplaced, ",")))}
    old = (json.load(open(DIST_PATH, encoding="utf-8")) if os.path.exists(DIST_PATH) else {}) or {}
    if {k: v for k, v in old.items() if k not in ("generated_at", "source_compiled", "source_compiled_iso")} == body:
        return
    with open(DIST_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(dict(body, generated_at=C.utc_now_iso(), source_compiled=through,
                       source_compiled_iso=C.utc_now_iso()), f, separators=(",", ":"))


def state_series(counties, sos):
    """Statewide early voters by party, cumulative through each of the state's roster days (Harris's own rosters
    folded in by their dates; their days after the state's latest wait for the state to catch up)."""
    dates = sos.get("state_days") or sorted(sos.get("days") or {})   # county lists' earlier days fold into the first
    if not dates:
        return {}
    days = {}
    for src in [sos["days"]] + [r.get("days") or {} for r in counties.values() if r.get("route") == "county"]:
        for d, a in src.items():
            b = days.setdefault(d, [0, 0, 0, 0])
            for i in range(4):
                b[i] += a[i]
    out = {"dates": [], "voters": [], "dem": [], "rep": [], "none": []}
    cum, keys, i = [0, 0, 0, 0], sorted(days), 0
    for d in dates:
        while i < len(keys) and keys[i] <= d:
            cum = [x + y for x, y in zip(cum, days[keys[i]])]
            i += 1
        out["dates"].append(d)
        for k, v in zip(("voters", "dem", "rep", "none"), cum):
            out[k].append(v)
    return out


def write_party_share(series, n_counties):
    """data/tx/party_share.json for the page's party-over-time chart (this election only: the past generals'
    figures, from theoelections, used a longer primary history, so they aren't comparable)."""
    if not series.get("dates"):
        return
    body = {"state": "tx", "source": "Official rosters: early voters matched to the primary rosters",
            "url": SOS_PAGE, "as_of": series["dates"][-1], "counties": n_counties, "partial_from": series["dates"][-1],
            "compare_default": "",
            "series": {"2026": dict(series, election_day="2026-11-03", span=SPAN_TEXT)},
            "note": "Share of early voters through each day by the last party primary each voted in: %s; none = no "
                    "primary in that span, including new voters. Every county's voters come from the Texas Secretary "
                    "of State's daily roster files (mail ballots logged before Oct 14 aren't in them) and Harris "
                    "County's own rosters, matched voter by voter to the official primary rosters. Earlier generals "
                    "aren't shown: their party figures (theoelections.com) counted primaries back to 2020, so they "
                    "aren't comparable. The latest day (*) can still fill in as counties add late rows." % SPAN_TEXT}
    old = (json.load(open(SHARE_PATH, encoding="utf-8")) if os.path.exists(SHARE_PATH) else {}) or {}
    if {k: v for k, v in old.items() if k != "generated_at"} == body:
        return
    with open(SHARE_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(dict(body, generated_at=C.utc_now_iso()), f, separators=(",", ":"))


def county_names():
    geo = json.load(open(os.path.join(ROOT, "assets", "tx-counties.geojson"), encoding="utf-8"))
    return {_ckey(f["properties"]["name"]): f["properties"]["name"] for f in geo["features"]}


def main():
    force, rebuild = "--force" in sys.argv, "--rebuild" in sys.argv
    out = (json.load(open(OUT_PATH, encoding="utf-8")) if os.path.exists(OUT_PATH) else {}) or {}
    fresh = out.get("v") != OUT_V
    counties = {} if fresh else out.get("counties", {})
    sos = {} if fresh else out.get("sos") or {}
    changed = force or fresh
    # counties' own rosters
    own = set()
    for county, spec in COUNTIES.items():
        prim = [c for c, _ in spec["primaries"]]
        old = counties.get(county, {})
        try:
            lookup = build_primary_lookup(county, spec, force=rebuild)
            voters, stamps = general_voters(county, spec)
        except Exception as e:  # noqa: BLE001
            kept = old.get("route") == "county" and old.get("primaries") == prim
            print("tx rosters: %s's own rosters unavailable (%s) -- %s" % (county, str(e)[:120],
                  "keeping its last figures" if kept else "using the state's"), file=sys.stderr)
            if kept:
                own.add(county)
            continue
        own.add(county)
        if not force and old.get("stamps") == stamps and old.get("primaries") == prim:
            print("tx rosters: %s unchanged (%s)" % (county, stamps))
            continue
        changed = True
        t, through = tally(voters, lookup)
        counties[county] = t.record(through=through, stamps=stamps, route="county", span=SPAN.get(county, ""),
                                    page=spec["page"], primaries=prim, primary_counts=lookup.get("counts", {}))
        print("tx rosters: %s %d voters through %s: D %d R %d none %d (mail %d, in person %d); %d districts, %d unmapped"
              % (county, t.tot["voters"], through, t.tot["dem"], t.tot["rep"], t.tot["none"], t.by_m["mail"]["voters"],
                 t.by_m["inp"]["voters"], len(t.by_cd), t.unmapped))
    # the state's rosters: every other county
    try:
        import tx_update as T
        db = sos_db()
        try:
            els = {e["id"]: e for e in (sos_json(type="EVR_ELECTION") or {}).get("elections", [])}
            prim_counts = sos_primaries(db, els, rebuild=rebuild)
            elec = els.get((T.find_election(T.load(T.CONFIG_PATH, {})) or {}).get("id"))
            if not elec:
                raise ValueError("the general isn't in the SoS app")
            new, latest, upd = sos_general(db, elec, force=force)
            new = county_lists(db, force=force) or new
            skip = {_ckey(c) for c in own}
            # the join reruns only when the files (or which counties use their own rosters) changed
            if new or force or fresh or not sos.get("through") or set(sos.get("skip") or []) != skip:
                names, days, unknown = county_names(), {}, []
                for c in [n for n, r in counties.items() if r.get("route") != "county" or n not in own]:
                    del counties[c]
                for sc, t in sos_tally(db, skip).items():
                    name = names.get(_ckey(sc))
                    if not name:
                        unknown.append(sc)
                        continue
                    rec = t.record(route="state", span=SPAN["state"], page=SOS_PAGE, primaries=["2026-03", "2026-05"])
                    if sc in VUID_COUNTIES:
                        rec.update(supplement=VUID_COUNTIES[sc]["label"], supplement_page=VUID_COUNTIES[sc]["page"])
                    for d, a in rec.pop("days").items():
                        b = days.setdefault(d, [0, 0, 0, 0])
                        for i in range(4):
                            b[i] += a[i]
                    counties[name] = rec
                # "through" = the last full day: the report for a voting day can come with a sliver of the next
                # day's file (mail logged early that morning), which isn't that day's voting yet
                sday = dict(db.execute("SELECT day, COUNT(*) FROM gen WHERE src = 'state' GROUP BY day"))
                big = max(sday.values()) if sday else 0
                full = max((d for d, n in sday.items() if n >= 0.1 * big), default="")
                full = "%s/%s/%s" % (full[5:7], full[8:10], full[:4]) if full else latest
                for rec in counties.values():
                    if rec.get("route") == "state":
                        rec["through"] = full
                if unknown:
                    print("tx rosters: state county names not on the map: %s" % unknown[:8], file=sys.stderr)
                sos = {"through": full, "latest_file": latest, "date_updated": upd, "primaries": prim_counts, "skip": sorted(skip),
                       "days": dict(sorted(days.items())), "state_days": sorted(sday)}
                changed = True
                st = sum(r["voters"] for r in counties.values() if r.get("route") == "state")
                print("tx rosters: state rosters through %s (report %s): %d counties, %d voters"
                      % (full, upd, sum(1 for r in counties.values() if r.get("route") == "state"), st))
            else:
                print("tx rosters: state rosters unchanged (through %s, report %s)" % (sos.get("through"), upd))
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 - the state's side failing must not lose Harris's update
        print("tx rosters: state rosters unavailable (%s) -- keeping the last figures" % str(e)[:150], file=sys.stderr)
    if not changed:   # nothing new: leave the files (and their timestamps) alone, so no commit
        return 0
    series = state_series(counties, sos)
    through = sos.get("through") or max((c.get("through", "") for c in counties.values()), default="")
    write_districts(counties, through)
    write_party_share(series, len(counties))
    body = {"v": OUT_V, "generated_at": C.utc_now_iso(),
            "method": "last party primary voted in (%s), from official rosters matched voter by voter to the 2026 "
                      "general's early-voting and mail rosters" % SPAN_TEXT,
            "sos": sos, "state_series": series, "counties": dict(sorted(counties.items()))}
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(body, f, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
