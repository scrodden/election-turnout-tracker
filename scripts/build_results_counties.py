#!/usr/bin/env python3
"""Build data/results/history/county/<office>_<year>.json -- county (or town /
parish) results for each past race on the results page, drawn when a state is
opened. Units are named exactly as in the state's map file (the same geometry
the turnout map uses, else assets/<st>-counties.geojson).

  President 2020, 2024      MIT 'County Presidential Election Returns
                            2000-2024' (doi:10.7910/DVN/VOQCHQ, CC0) -- a local
                            copy is needed (Dataverse guestbook): --countypres
  Senate, Governor 2018/22/24  MIT official precinct returns (CC0), summed by
                            county (same de-duplication as the statewide build)
  Senate, Governor 2020     US 2020 General Official Election Results in
                            Tabular Format (doi:10.7910/DVN/RV80FW, CC0)
  Off-year governors        official state files (see OFFCYCLE_* below)

A race's county figures are kept only when its two leading candidates' county
sums are within 3% of the statewide result shown on the page (official totals
where those were entered) and at least 90% of the state's map units are
matched; otherwise the race gets a 'missing' reason instead. A race decided
in a runoff is mapped with its first round (November) unless runoff county
results are available.

  python scripts/build_results_counties.py --cache DIR --countypres FILE [--only president_2024,...]
"""
import csv
import glob
import io
import json
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402
import build_turnout_history as TH  # noqa: E402
import build_results_history as RH  # noqa: E402

HIST = os.path.join(ROOT, "data", "results", "history")
OUT = os.path.join(HIST, "county")
SUFFIX = {"JR", "SR", "II", "III", "IV", "V"}
MAX_CANDS = 4


# ---------------- names ----------------

def name_key(name):
    """'NED LAMONT AND SUSAN BYSIEWICZ' / 'Ned Lamont' -> ('NED', 'LAMONT');
    'Joseph R. Biden Jr.' -> ('JOSEPH', 'BIDEN'); 'Abbott' -> ('', 'ABBOTT')."""
    n = re.split(r"\s+(?:AND|&)\s+|/", (name or "").upper())[0]
    n = re.sub(r"\"[^\"]*\"|\([^)]*\)", " ", n)   # nicknames
    if n.count(",") == 1:
        last, rest = n.split(",")
        n = rest + " " + last
    toks = [t for t in re.sub(r"[^A-Z ]", " ", n).split() if t not in SUFFIX and len(t) > 1]
    if not toks:
        return ("", "")
    return (toks[0] if len(toks) > 1 else "", toks[-1])


def _one_edit(a, b):
    if abs(len(a) - len(b)) > 1:
        return False
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    return a[i + 1:] == b[i + 1:] or a[i:] == b[i + 1:] or a[i + 1:] == b[i:]


def same_person(a, b):
    (fa, la), (fb, lb) = name_key(a), name_key(b)
    if not la or not lb:
        return False
    if la != lb and not (fa and fa == fb and min(len(la), len(lb)) >= 5 and _one_edit(la, lb)):   # O'Conner / O'Connor
        return False
    return not fa or not fb or fa == fb or fa[0] == fb[0]


# ---------------- geometry ----------------

_REG = None


def geo_options(st):
    """[(path, {norm(name): name}, n_features)] -- the turnout map's file first."""
    global _REG
    if _REG is None:
        reg = json.load(open(os.path.join(ROOT, "assets", "states.json"), encoding="utf-8"))
        _REG = reg["states"] if isinstance(reg, dict) else reg
    paths = []
    e = next((x for x in _REG if x.get("code") == st.lower()), None)
    if e and e.get("geojson"):
        paths.append(e["geojson"])
    for p in ("assets/%s-counties.geojson" % st.lower(), "assets/la-parishes.geojson" if st == "LA" else ""):
        if p and p not in paths and os.path.exists(os.path.join(ROOT, *p.split("/"))):
            paths.append(p)
    out = []
    for p in paths:
        g = json.load(open(os.path.join(ROOT, *p.split("/")), encoding="utf-8"))
        names = {TH.norm(f["properties"]["name"]): f["properties"]["name"] for f in g["features"]}
        out.append((p, names, len(g["features"])))
    return out


_XW = {}


def crosswalk(st):
    if st not in _XW:
        p = os.path.join(ROOT, "assets", "crosswalks", "%s-town-county.json" % st.lower())
        _XW[st] = C.load_crosswalk(p) if os.path.exists(p) else {}
    return _XW[st]


# abbreviated county names in some official files
ALIAS = {"MS": {"jeffdavis": "jeffersondavis"}, "LA": {"jeffdavis": "jeffersondavis"}, "MT": {"lewisclark": "lewisandclark"}}

# election authorities that aren't counties, folded into the county holding most of them (noted on the map)
FOLD = {"MO": {"kansascity": ("jackson", "Kansas City, which reports separately, is included in Jackson County.")}}


def place(units_by_field, st):
    """units_by_field: {field: {unit_name: {cand: votes}}} -> best (geo path,
    {geo unit name: {cand: votes}}, matched share of the map's units, n, notes)."""
    best = None
    for path, names, n in geo_options(st):
        for field, units in units_by_field.items():
            got = defaultdict(lambda: defaultdict(int))
            notes = set()
            xw = crosswalk(st)
            towns = bool(xw) and len(units) > 1.5 * n   # town-level units (New England, WI) -> counties via the crosswalk
            for u, cands in units.items():
                nu = TH.norm(u)
                nu = ALIAS.get(st, {}).get(nu, nu)
                if nu in FOLD.get(st, {}):
                    nu, note = FOLD[st][nu]
                    notes.add(note)
                k = None
                if towns and xw.get(C.norm_town(u)) and "county" not in u.lower():
                    k = names.get(TH.norm(xw[C.norm_town(u)]["county"]))
                k = k or names.get(nu)
                if k:
                    for c, v in cands.items():
                        got[k][c] += v
            share = len(got) / float(n) if n else 0
            if got and (best is None or share > best[2]):
                best = (path, got, share, n, sorted(notes))
    return best


# ---------------- assemble one race ----------------

def assemble(race, units_by_field, st, source, round_label=""):
    """-> (entry, None) or (None, reason)."""
    target = (race.get("first_round") or race) if race.get("first_round") and not round_label == "runoff" else race
    cands = [c for c in target["candidates"] if c["name"] not in ("Write-ins", "Other")][:MAX_CANDS]
    if len(cands) < 2:
        return None, "only one candidate"
    placed = place(units_by_field, st)
    if not placed:
        return None, "no county figures"
    path, got, share, n, fold_notes = placed
    if share < 0.9 and n - len(got) > 1:
        return None, "county figures cover only %d of %d areas" % (len(got), n)
    units, sums = {}, [0] * len(cands)
    for u, cv in got.items():
        row = [0] * (len(cands) + 1)
        for src_name, v in cv.items():
            if RH.NOT_A_CANDIDATE.match(src_name.strip()) or TH.PSEUDO.match(src_name.strip()):
                continue
            i = next((i for i, c in enumerate(cands) if same_person(c["name"], src_name)), None)
            row[len(cands) if i is None else i] += v
        for i in range(len(cands)):
            sums[i] += row[i]
        units[u] = row
    ratios = [sums[i] / float(cands[i]["votes"]) if cands[i]["votes"] else 0 for i in range(2)]
    if not all(0.97 <= x <= 1.03 for x in ratios):
        return None, "county figures don't add up to the statewide result (%s)" % ", ".join("%.1f%%" % (100 * x) for x in ratios)
    keep = [i for i in range(len(cands)) if i < 2 or sums[i] > 0]   # e.g. a statewide write-in with no county figures
    if len(keep) < len(cands):
        drop = [i for i in range(len(cands)) if i not in keep]
        units = {u: [row[i] for i in keep] + [row[-1] + sum(row[i] for i in drop)] for u, row in units.items()}
        cands = [cands[i] for i in keep]
    entry = {"geo": path, "cands": [{"name": c["name"], "party": c["party"]} for c in cands],
             "units": units, "source": source,
             "round": "first" if target is not race else (round_label or "")}
    notes = list(fold_notes)
    lo, hi = min(ratios), max(ratios)
    if lo < 0.995 or hi > 1.005:
        notes.append("County figures add up to %.1f%% of the statewide total." % (100 * (lo + hi) / 2))
    if notes:
        entry["note"] = " ".join(notes)
    return entry, None


# ---------------- sources ----------------

def precinct_units(raw):
    """{race: {field: {unit: {candidate: votes}}}} for GOVERNOR / US SENATE
    (+ special), de-duplicated like the statewide build."""
    fields = ("county_name", "jurisdiction_name")
    cell = defaultdict(lambda: [0, 0])
    for r in TH.precinct_rows(raw):
        if (r.get("stage") or "GEN").upper() not in ("GEN", ""):
            continue
        office = TH.office_key(r.get("office"))
        if office.startswith(("US SENATE", "UNITED STATES SENATOR")):   # 'US SENATE - 2 YEAR TERM' (NE 2024 special)
            office = "US SENATE"
        if office not in ("GOVERNOR", "US SENATE"):
            continue
        cand = (r.get("candidate") or "").strip()
        if not cand or TH.PSEUDO.match(cand):
            continue
        try:
            v = int(float(r.get("votes") or 0))
        except ValueError:
            continue
        special = str(r.get("special") or "").upper() in ("TRUE", "1", "T", "YES")
        rc = office + (" (SPECIAL)" if special else "")
        units = tuple((r.get(f) or (r.get(f.replace("_name", "")) or "")) for f in fields)
        party = (r.get("party_detailed") or r.get("party") or "").strip()
        cell[(rc, units, r.get("precinct") or "", cand.upper(), (r.get("mode") or "").upper())][0 if party else 1] += v
    prec = defaultdict(lambda: defaultdict(lambda: [0, 0, False]))
    for (rc, units, p, cand, mode), (pv, bv) in cell.items():
        a = prec[(rc, units, p)][cand]
        if mode == "TOTAL":
            a[0] += pv or bv
            a[2] = True
        else:
            a[1] += pv or bv
    out = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(int))))
    for (rc, units, p), cands in prec.items():
        has_total = any(a[2] for a in cands.values())
        for cand, a in cands.items():
            v = a[0] if has_total else a[1]
            for i, f in enumerate(fields):
                if units[i]:
                    out[rc][f][units[i]][cand] += v
    return out


def countypres(path, year):
    """{ST: {'county_name': {county: {candidate: votes}}}}"""
    acc = defaultdict(lambda: {"t": 0, "m": 0, "ht": False})
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        head = f.readline()
        f.seek(0)
        delim = "\t" if head.count("\t") >= head.count(",") else ","
        for r in csv.DictReader(f, delimiter=delim):
            r = {(k or "").strip().strip('"').lower(): (v or "").strip().strip('"') for k, v in r.items()}
            if r.get("year") != year:
                continue
            try:
                v = int(float(r.get("candidatevotes") or 0))
            except ValueError:
                continue
            a = acc[((r.get("state_po") or "").upper(), r.get("county_name") or "", r.get("candidate") or "")]
            if (r.get("mode") or "").upper() == "TOTAL":
                a["t"] += v
                a["ht"] = True
            else:
                a["m"] += v
    out = defaultdict(lambda: {"county_name": defaultdict(dict)})
    for (st, cty, cand), a in acc.items():
        out[st]["county_name"][cty][cand] = a["t"] if a["ht"] else a["m"]
    return out


def rv80fw_units(st, contest_pattern, fid, cache):
    """{'unit': {unit: {selection: votes}}} for one 2020 contest (reporting units one level below the state)."""
    p = os.path.join(cache, "rv80fw_%s.tab" % st) if cache else None
    if p and os.path.exists(p):
        raw = open(p, encoding="utf-8").read()
    else:
        raw = C.http_get("https://dataverse.harvard.edu/api/access/datafile/%d?format=original" % fid[st + "_tabular.tab"],
                         binary=True, retries=2, timeout=180).decode("utf-8-sig", "replace")
        if p:
            open(p, "w", encoding="utf-8").write(raw)
    delim = "\t" if raw[:500].count("\t") > raw[:500].count(",") else ","
    units = defaultdict(lambda: defaultdict(lambda: {"t": 0, "o": 0, "ht": False}))
    for r in csv.DictReader(io.StringIO(raw), delimiter=delim):
        if not re.search(contest_pattern, r.get("Contest") or "", re.I):
            continue
        ru = (r.get("ReportingUnit") or "").split(";")
        if len(ru) != 2:
            continue
        try:
            v = int(float(r.get("Count") or 0))
        except ValueError:
            continue
        a = units[ru[1]][r.get("Selection") or ""]
        if (r.get("VoteType") or "").lower() == "total":
            a["t"] += v
            a["ht"] = True
        else:
            a["o"] += v
    return {"unit": {u: {s: (a["t"] if a["ht"] else a["o"]) for s, a in cs.items()} for u, cs in units.items()}}


# ---------------- official county tables used instead of MIT data ----------------
# (where the MIT file is incomplete or the statewide figure is an official entry)
CIVERA = {"ny": "https://results.elections.ny.gov/contest/%d", "va": "https://historical.elections.virginia.gov/contest/%d",
          "ct": "https://electionhistory.ct.gov/contest/%d", "nm": "https://electionstats.sos.nm.gov/contest/%d",
          "sc": "https://electionhistory.scvotes.gov/contest/%d"}
OFFICIAL_COUNTY = {
    "gov-NY|2018": ("civera", ("ny", 1336)), "gov-NY|2022": ("civera", ("ny", 1)),
    "sen-NY|2018": ("civera", ("ny", 596)), "sen-NY|2022": ("civera", ("ny", 4)), "sen-NY|2024": ("civera", ("ny", 5582)),
    "gov-CT|2018": ("civera", ("ct", 3781)), "gov-CT|2022": ("civera", ("ct", 35080)),
    "gov-NM|2022": ("civera", ("nm", 11090)),
    "sen-IN|2022": ("in", ("2022", "US Senator")), "sen-IN|2024": ("in", ("2024", "US Senator")),
    "gov-IN|2024": ("in", ("2024", "Governor & Lt. Governor")),
    "gov-ID|2022": ("id", 17251), "sen-ID|2022": ("id", 17248),
    "sen-CA|2022": ("ca", "https://elections.cdn.sos.ca.gov/sov/2022-general/ssov/us-senate-summary-by-county-ft.xlsx"),
    "sen-CA-special|2022": ("ca", "https://elections.cdn.sos.ca.gov/sov/2022-general/ssov/us-senate-summary-by-county.xlsx"),
    "gov-TN|2022": ("tn", "https://sos-prod.tnsosgovfiles.com/s3fs-public/document/20221108GovbyCounty.pdf"),
    "gov-MO|2020": ("pdf", ("https://www.sos.mo.gov/CMSImages/ElectionResultsStatistics/ActualResults-November32020.pdf",
                            ("Mike Parson", "Nicole Galloway"),
                            ("Mike Parson", "Nicole Galloway", "Rik Combs", "Jerome Howard Bauer", "Write-in 1", "Write-in 2"))),
    "gov-ND|2020": ("nd", (313, "Governor and Lt. Governor")),
    "sen-MT|2020": ("mt", ("https://sosmt.gov/wp-content/uploads/State_Canvass_Report.pdf", "UNITED STATES SENATOR",
                           ("Steve Bullock", "Steve Daines"))),
    "gov-MT|2020": ("mt", ("https://sosmt.gov/wp-content/uploads/State_Canvass_Report.pdf", "GOVERNOR & LT. GOVERNOR",
                           ("Mike Cooney", "Lyman Bishop", "Greg Gianforte"))),
    "sen-NJ|2024": ("nj", "https://nj.gov/state/elections/assets/pdf/election-results/2024/2024-official-general-results-us-senate.pdf"),
    "sen-DE|2018": ("de", ("https://elections.delaware.gov/elections/resultsarchive/elect18/elect18_general/html/stwoff_kns.shtml",
                           "UNITED STATES SENATOR", ("New Castle", "Kent", "Sussex"))),
}
UNIT_ROWS = ("County", "Locality", "City/Town", "Parish", "Municipality", "Town")


def off_civera(spec):
    host, cid = spec
    url = "https://%s.elstats.civera.com/api/download_contest/%d_table.csv?split_party=false" % (
        {"va": "va2"}.get(host, host), cid)
    rows = list(csv.reader(io.StringIO(C.http_get(url, retries=2, timeout=90))))
    names = rows[0]
    units = {}
    for r in rows:
        if r and r[0] in UNIT_ROWS and len(r) > 2:
            units[r[1]] = {names[i]: _num(r[i]) for i in range(2, min(len(r), len(names))) if names[i] and r[i].strip()}
    return {"unit": units}, CIVERA[host] % cid


def off_de(spec):
    """Delaware 'Results by Statewide Office by County' page: 'LAST FIRST M | n | n | n | total'."""
    import html as H
    url, office, counties = spec
    t = H.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " | ", C.http_get(url, retries=2, timeout=60))))
    seg = t[t.find(office) + len(office):]
    seg = seg[:seg.find("Office Total")]
    units = defaultdict(dict)
    for m in re.finditer(r"([A-Z][A-Z .'-]+?) \| \| ([\d,]+) \| \| ([\d,]+) \| \| ([\d,]+) \| \| [\d,]+", seg):
        parts = m.group(1).split()
        name = " ".join(parts[1:] + parts[:1])   # 'CARPER THOMAS R' -> 'THOMAS R CARPER'
        for cty, v in zip(counties, m.groups()[1:]):
            units[cty][name] = _num(v)
    return {"county": dict(units)}, url


def off_in(spec):
    """Indiana ENR archive 'AllOfficeResults.csv' (official, by precinct) summed by county."""
    year, category = spec
    url = "https://enr.indianavoters.in.gov/archive/%sGeneral/download/AllOfficeResults.csv" % year
    units = defaultdict(lambda: defaultdict(int))
    for r in csv.DictReader(io.StringIO(C.http_get(url, retries=2, timeout=180).lstrip("﻿"))):
        # each county reports either by precinct or as one 'Locality' (never both), so all rows are summed
        if r.get("OfficeCategory") == category and (r.get("DataEntryLevelName") or "") in ("Precinct", "Locality"):
            units[r["ReportingCountyName"]][re.sub(r"\s*\(W/I\)$", "", r["NameonBallot"])] += int(float(r["TotalVotes"] or 0))
    return {"county": units}, "https://enr.indianavoters.in.gov/archive/%sGeneral/index.html" % year


def off_idaho(contest_id):
    """Idaho canvass database county CSV (header cells 'Name\\nParty')."""
    url = "https://canvass.sos.idaho.gov/eng/contests/download/%d/show_granularity_dt_id:3/.csv" % contest_id
    rows = list(csv.reader(io.StringIO(C.http_get(url, retries=2, timeout=90))))
    names = [h.split("\n")[0].strip() for h in rows[0]]
    units = {}
    for r in rows[1:]:
        if r and r[0] and r[0].strip().upper() not in ("TOTAL", "TOTALS"):
            units[r[0].strip()] = {names[i]: _num(r[i]) for i in range(1, min(len(r), len(names))) if names[i] and r[i].strip()}
    return {"county": units}, "https://canvass.sos.idaho.gov/contests/view/%d" % contest_id


def off_ca(url):
    """California Statement of Vote county summary (xlsx): name row, party row, then county / 'Percent' pairs."""
    import xls_lite as X
    wb = X.read_any(C.http_get(url, binary=True, retries=2, timeout=120))
    rows = (wb if isinstance(wb, list) else list(wb.items()))[0][1]
    names = [re.sub(r"\s+", " ", c or "").strip() for c in rows[0]]
    units = {}
    for r in rows[2:]:
        if r and isinstance(r[0], str) and r[0].strip() and "Percent" not in r[0] and "Total" not in r[0]:
            units[r[0].strip()] = {names[i]: int(r[i] or 0) for i in range(1, min(len(r), len(names))) if names[i]}
    return {"county": units}, url


def off_tn(url):
    """Tennessee governor-by-county PDF: only the first two columns (Lee, Martin) parse cleanly."""
    units = {}
    for t in _pdf_pages(url):
        if "Bill Lee" not in t:
            continue
        for line in t.split("\n"):
            m = re.match(r"^([\d,]+) ([\d,]+) [\d, ]*?\d([A-Z][A-Za-z .']+)$", line.strip())
            if m:
                units[m.group(3).strip()] = {"Bill Lee": _num(m.group(1)), "Jason Brantley Martin": _num(m.group(2))}
    return {"county": units}, url


def off_nj_url(url):
    return off_nj(None, url)


def off_mt(spec):
    """Montana state canvass: one page per race, county names in one block and the vote rows in the same order."""
    url, marker, cols = spec
    geo = {TH.norm(f["properties"]["name"]) for f in json.load(open(os.path.join(ROOT, "assets", "mt-counties.geojson"), encoding="utf-8"))["features"]}
    for t in _pdf_pages(url):
        if marker not in t:
            continue
        lines = [x.strip() for x in t.split("\n") if x.strip()]
        names = [x for x in lines if ALIAS["MT"].get(TH.norm(x), TH.norm(x)) in geo]
        rows = [x for x in lines if re.match(r"^\d[\d,]*( \d[\d,]*){%d}$" % (len(cols) - 1), x)]
        rows = rows[:len(names)]   # the 'Total' row follows the counties
        if len(rows) == len(names):
            return {"county": {n: {c: _num(v) for c, v in zip(cols, r.split())} for n, r in zip(names, rows)}}, url
    return {"county": {}}, url


def off_nd(spec):
    """North Dakota results site: one 'All contests in <county>' page per county (cty=01..53)."""
    import html as H
    eid, contest = spec
    units = {}
    for n in range(1, 54):
        url = "https://results.sos.nd.gov/ResultsSW.aspx?type=CTYALL&cty=%02d&map=CTY&eid=%d" % (n, eid)
        t = H.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", C.http_get(url, retries=2, timeout=60))))
        m = re.search(r"All Contests in ([A-Z][A-Za-z .]+?) All Contests in", t)
        i = t.find(contest)
        if not m or i < 0:
            continue
        block = t[i:t.find("Total Votes", i)]
        units[m.group(1)] = {c.strip(): _num(v) for c, v in re.findall(
            r"(?:(?<=%)|(?<=/>)) ([A-Za-z .'-]+?) (?:Republican|Democratic-NPL|Libertarian|Nonpartisan|Independent|Constitution) ([\d,]+) [\d.]+%", block)}
    return {"county": units}, "https://results.sos.nd.gov/Default.aspx?eid=%d" % eid


def off_pdf_table(spec):
    """Rows 'County n1 n2 ...' on the pages that name all of `marks` (one race's county table)."""
    url, marks, cols = spec
    units = {}
    rx = re.compile(r"^([A-Za-z][A-Za-z .'-]+?) ((?:[\d,]+ ){%d}[\d,]+)$" % (len(cols) - 1))
    for t in _pdf_pages(url):
        if not all(m in t for m in marks):
            continue
        for line in t.split("\n"):
            m = rx.match(line.strip())
            if m and m.group(1).strip().lower() not in ("total", "totals", "grand total"):
                units[m.group(1).strip()] = {c: _num(v) for c, v in zip(cols, m.group(2).split())}
    return {"county": units}, url


OFFICIAL_NOTES = {"gov-TN|2022": "Tennessee's county table is mapped for the two leading candidates only; county shares are of their combined votes."}


def official_entry(r, year):
    try:
        ou, osrc = official_units(r["id"], year)
        e, why = assemble(r, ou, r["state"], osrc)
    except Exception as ex:  # noqa: BLE001
        return None, "official county file unavailable (%s)" % str(ex)[:50]
    note = OFFICIAL_NOTES.get(r["id"] + "|" + year)
    if e and note:
        e["note"] = (e.get("note", "") + " " + note).strip()
    return e, why


def official_units(rid, year):
    kind, spec = OFFICIAL_COUNTY[rid + "|" + year]
    units, url = {"civera": off_civera, "de": off_de, "in": off_in, "id": off_idaho, "ca": off_ca, "tn": off_tn,
                  "nj": off_nj_url, "pdf": off_pdf_table, "mt": off_mt, "nd": off_nd}[kind](spec)
    return units, "Official results by county: " + url


# ---------------- off-year governors (official state files) ----------------

OFFCYCLE = {
    "offcycle_latest": {
        "VA": ("va", 164996), "LA": ("la", ("20231014", "10-14-2023", "")), "NJ": ("nj", 2025),
        "MS": ("ms", "https://sos.ms.gov/elections/electionresults/2023General/State%20Certified%20Results/2023%20Statewide%20Recapitulation%20Sheet.pdf"),
        "KY": ("ky", "https://elect.ky.gov/results/2020-2029/Documents/Certification%20of%20Election%20Results%20for%202023%20General%20Election%20Final.pdf"),
    },
    "offcycle_previous": {
        "VA": ("va", 147466), "LA": ("la", ("20191116", "11-16-2019", "runoff")), "NJ": ("nj", 2021),
        "MS": ("ms", "https://sos.ms.gov/elections/electionresults/2019General/Certified%20Results/2019%20General%20Election%20Official%20Statewide%20Recapitulation%20(PDF).pdf"),
        "KY": (None, "Kentucky's 2019 county results are published only as scanned images"),
    },
}


def _pdf_pages(url):
    from pypdf import PdfReader
    return [p.extract_text() or "" for p in PdfReader(io.BytesIO(C.http_get(url, binary=True, retries=2, timeout=180))).pages]


def _num(s):
    return int(s.replace(",", ""))


def off_va(contest_id):
    url = "https://va2.elstats.civera.com/api/download_contest/%d_table.csv?split_party=false" % contest_id
    rows = list(csv.reader(io.StringIO(C.http_get(url, retries=2, timeout=90))))
    names = rows[0]
    units = {}
    for r in rows:
        if r and r[0] == "Locality":
            units[r[1]] = {names[i]: _num(r[i]) for i in range(2, len(r)) if names[i] and r[i].strip()}
    return {"locality": units}, "https://historical.elections.virginia.gov/contest/%d" % contest_id


def off_la(spec):
    dt, nice, _ = spec
    import xls_lite as X
    url = "https://s3-us-west-2.amazonaws.com/mediaresults.sos.la.gov/HumanReadableElectionResults/%s/Election+Results+(%s).xlsx" % (dt, nice)
    wb = X.read_any(C.http_get(url, binary=True, retries=2, timeout=120))
    rows = dict(wb if isinstance(wb, list) else wb.items())["Multi-Parish(Parish)"]
    i = next(i for i, r in enumerate(rows) if r and r[0] == "Governor")
    head = rows[i + 1]
    names = [re.sub(r"\s*\([A-Z]+\)\s*$", "", h or "") for h in head]
    units = {}
    for r in rows[i + 2:]:
        if not r or not r[0]:
            break
        if r[0] == "Total Votes":
            continue
        units[r[0]] = {names[j]: int(r[j] or 0) for j in range(1, len(r)) if j < len(names) and names[j]}
    return {"parish": units}, url


def off_ms(url):
    units = defaultdict(dict)
    for t in _pdf_pages(url):
        m = re.search(r"State Of Mississippi-Governor\n(.*?)\n[^\n]*Lieutenant", t, re.S)
        c = re.search(r"day of November \d{4}\n(.*?)\nStatewide Election", t, re.S)
        if not m or not c:
            continue
        counties = [x.strip() for x in c.group(1).split("\n") if x.strip()]
        for line in m.group(1).split("\n"):
            mm = re.match(r"(.+?) (Democrat|Republican|Independent|Constitution|Libertarian|Green|Reform) ([\d ]+)$", line.strip())
            if not mm:
                continue
            nums = [int(x) for x in mm.group(3).split()]
            for cty, v in zip(counties, nums):
                if cty.upper() != "TOTAL":
                    units[cty][mm.group(1)] = v
    return {"county": dict(units)}, url


NJ_COUNTIES = ("ATLANTIC", "BERGEN", "BURLINGTON", "CAMDEN", "CAPE MAY", "CUMBERLAND", "ESSEX", "GLOUCESTER", "HUDSON",
               "HUNTERDON", "MERCER", "MIDDLESEX", "MONMOUTH", "MORRIS", "OCEAN", "PASSAIC", "SALEM", "SOMERSET",
               "SUSSEX", "UNION", "WARREN")


def off_nj(year, url=None):
    url = url or "https://nj.gov/state/elections/assets/pdf/election-results/%d/%d-official-general-results-governor.pdf" % (year, year)
    units = defaultdict(dict)
    for t in _pdf_pages(url):
        lines = [x.strip() for x in t.split("\n")]
        h = next((i for i, x in enumerate(lines) if x.startswith("TallySlogan")), None)
        if h is None or h + 1 >= len(lines):
            continue
        name = re.split(r"\s+\(w\)|\s+P\.?\s?O\.?\s|\s+\d", lines[h + 1])[0].strip()
        for x in lines:
            if ", NJ" in x:   # candidate address lines ('UNION CITY, NJ 07087')
                continue
            for cty in NJ_COUNTIES:
                if x.startswith(cty + " "):
                    v = re.search(r"([\d,]+)$", x)
                    if v:
                        units[cty.title()][name] = units[cty.title()].get(name, 0) + _num(v.group(1))
    return {"county": dict(units)}, url


def off_ky(url):
    """Governor table rows 'County  Beshear  Cameron  Write-in'."""
    units = {}
    on = False
    for t in _pdf_pages(url):
        for line in t.split("\n"):
            line = line.strip()
            if line.startswith("Governor"):
                on = True
                continue
            if on and re.match(r"(Secretary of State|Attorney General|Auditor|Treasurer)", line):
                on = False
            m = re.match(r"^([A-Za-z][A-Za-z .']+?) ([\d,]+) ([\d,]+) ([\d,]+)$", line) if on else None
            if m and m.group(1).lower() not in ("total", "totals"):
                units[m.group(1)] = {"Andy Beshear": _num(m.group(2)), "Daniel Cameron": _num(m.group(3)),
                                     "Write-ins": _num(m.group(4))}
    return {"county": units}, url


def build_offcycle(key):
    d = load("governor", key)
    entries, missing = {}, {}
    for r in d["races"]:
        kind, spec = OFFCYCLE[key].get(r["state"], (None, "no county source"))
        if not kind:
            missing[r["id"]] = spec
            continue
        try:
            units, url = {"va": off_va, "la": off_la, "ms": off_ms, "nj": off_nj, "ky": off_ky}[kind](spec)
        except Exception as e:  # noqa: BLE001
            missing[r["id"]] = "county file unavailable (%s)" % str(e)[:60]
            continue
        e, why = assemble(r, units, r["state"], "Official results by county: " + url,
                          round_label=spec[2] if kind == "la" else "")
        if e:
            entries[r["id"]] = e
        else:
            missing[r["id"]] = why
    write("governor", key, entries, missing)


# ---------------- per-state extract for the turnout page ----------------
# data/<st>/past_results_county.json: each past race's county results (two
# leading candidates + all votes), keyed by the turnout map's unit names, for the
# "if every county voted like <race>" estimate on states without party data.

OFFICE_LABEL = {"president": "President", "senate": "U.S. Senate", "governor": "Governor"}
OFFICE_RANK = {"president": 0, "senate": 1, "governor": 2}


def write_state_extracts():
    reg = json.load(open(os.path.join(ROOT, "assets", "states.json"), encoding="utf-8"))
    states = reg["states"] if isinstance(reg, dict) else reg
    files = sorted(glob.glob(os.path.join(OUT, "*.json")))
    by_state = defaultdict(list)
    for p in files:
        office, key = os.path.basename(p)[:-5].split("_", 1)
        cd = json.load(open(p, encoding="utf-8"))
        hist = {r["id"]: r for r in load(office, key)["races"]}
        for rid, e in cd["races"].items():
            r = hist.get(rid)
            if not r:
                continue
            target = r["first_round"] if e.get("round") == "first" and r.get("first_round") else r
            sw = {c["name"]: c["votes"] for c in target["candidates"]}
            tot = target.get("total_votes") or sum(sw.values())
            c1, c2 = e["cands"][0], e["cands"][1]
            label = "%d %s%s%s" % (r["year"], OFFICE_LABEL[office], " (special)" if r.get("special") else "",
                                   " — first round" if e.get("round") == "first" else "")
            by_state[r["state"]].append({
                "key": "%s-%s%s" % (office, r["year"], "-special" if r.get("special") else ""),
                "label": label, "office": office, "year": r["year"], "geo": e["geo"],
                "cands": [{"name": c1["name"], "party": c1["party"]}, {"name": c2["name"], "party": c2["party"]}],
                "statewide": [round(100.0 * sw.get(c1["name"], 0) / tot, 2), round(100.0 * sw.get(c2["name"], 0) / tot, 2)] if tot else None,
                "units": {u: [row[0], row[1], sum(row)] for u, row in e["units"].items()},
                "source": e["source"]})
    n = 0
    for s in states:
        st, geo = s["code"].upper(), s.get("geojson")
        races = [x for x in by_state.get(st, []) if x["geo"] == geo]
        p = os.path.join(ROOT, "data", s["code"], "past_results_county.json")
        if not races:
            if os.path.exists(p):
                os.remove(p)
            continue
        races.sort(key=lambda x: (-x["year"], OFFICE_RANK[x["office"]], x["key"]))
        for x in races:
            del x["geo"]
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"state": s["code"], "races": races}, f, separators=(",", ":"))
        n += 1
    print("per-state extracts: %d states" % n)


# ---------------- main ----------------

def load(office, year):
    p = os.path.join(HIST, "%s_%s.json" % (office, year))
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


FRIENDLY = {"no county figures": "county results aren't available in the sources used"}


def write(office, year, entries, missing):
    os.makedirs(OUT, exist_ok=True)
    missing = {k: FRIENDLY.get(v, v) for k, v in missing.items()}
    doc = {"office": office, "year": year, "races": entries, "missing": missing}
    with open(os.path.join(OUT, "%s_%s.json" % (office, year)), "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, separators=(",", ":"))
    print("%s_%s: %d races mapped; missing: %s" % (office, year, len(entries),
                                                  ", ".join("%s (%s)" % (k, v) for k, v in sorted(missing.items())) or "none"))


def main():
    args = sys.argv[1:]
    cache = args[args.index("--cache") + 1] if "--cache" in args else None
    cpres = args[args.index("--countypres") + 1] if "--countypres" in args else None
    only = set(args[args.index("--only") + 1].split(",")) if "--only" in args else None
    only_states = set(args[args.index("--states") + 1].upper().split(",")) if "--states" in args else None

    def want(k):
        return only is None or k in only

    if "--extracts-only" in args:
        write_state_extracts()
        return 0
    if "--patch-official" in args:   # redo only the races with an official county table, keep the rest of each file
        for key in sorted(OFFICIAL_COUNTY):
            rid, y = key.split("|")
            office = "governor" if rid.startswith("gov-") else "senate"
            p = os.path.join(OUT, "%s_%s.json" % (office, y))
            if not os.path.exists(p) or not want("%s_%s" % (office, y)):
                continue
            doc = json.load(open(p, encoding="utf-8"))
            r = next((x for x in load(office, y)["races"] if x["id"] == rid), None)
            if not r:
                continue
            e, why = official_entry(r, y)
            if e:
                doc["races"][rid] = e
                doc["missing"].pop(rid, None)
            else:
                doc["races"].pop(rid, None)
                doc["missing"][rid] = why
            write(office, y, doc["races"], doc["missing"])
        write_state_extracts()
        return 0

    # president (county presidential file)
    for y in ("2020", "2024"):
        if not want("president_" + y):
            continue
        doc = load("president", y)
        if not cpres:
            print("president_%s: skipped (no --countypres file)" % y)
            continue
        cp = countypres(cpres, y)
        entries, missing = {}, {}
        for r in doc["races"]:
            e, why = assemble(r, cp.get(r["state"], {}), r["state"],
                              "MIT Election Data + Science Lab, County Presidential Election Returns 2000-2024 (CC0)")
            if e:
                entries[r["id"]] = e
            else:
                missing[r["id"]] = why
        write("president", y, entries, missing)

    # senate + governor from precinct files
    for y in ("2018", "2022", "2024"):
        if not (want("senate_" + y) or want("governor_" + y)):
            continue
        docs = {o: load(o, y) for o in ("senate", "governor")}
        res = {o: ({}, {}) for o in docs}
        states = sorted({r["state"] for d in docs.values() if d for r in d["races"]})
        if only_states:   # re-run some states and keep everything else in the existing files
            states = [s for s in states if s in only_states]
            for o in docs:
                p = os.path.join(OUT, "%s_%s.json" % (o, y))
                if os.path.exists(p):
                    old = json.load(open(p, encoding="utf-8"))
                    keep = lambda k: k.split("-")[1] not in only_states   # noqa: E731
                    res[o] = ({k: v for k, v in old["races"].items() if keep(k)},
                              {k: v for k, v in old["missing"].items() if keep(k)})
        for st in states:
            try:
                raw = TH.fetch_zip(TH.MEDSL[y].format(st=st.lower()), cache)
                pu = precinct_units(raw)
                del raw
            except Exception as e:  # noqa: BLE001
                pu = {}
                print("  %s %s: no precinct file (%s)" % (y, st, str(e)[:60]))
            for o, d in docs.items():
                if not d or not want("%s_%s" % (o, y)):
                    continue
                for r in d["races"]:
                    if r["state"] != st:
                        continue
                    rc = ("GOVERNOR" if o == "governor" else "US SENATE") + (" (SPECIAL)" if r.get("special") else "")
                    if r["id"] + "|" + y in OFFICIAL_COUNTY:
                        e, why = official_entry(r, y)
                    else:
                        e, why = assemble(r, pu.get(rc, {}), st,
                                          "MIT Election Data + Science Lab official precinct returns, summed by county (CC0)")
                    if e:
                        res[o][0][r["id"]] = e
                    else:
                        res[o][1][r["id"]] = why
        for o in docs:
            if docs[o] and want("%s_%s" % (o, y)):
                write(o, y, *res[o])

    # 2020 senate + governor (official tabular file)
    if want("senate_2020") or want("governor_2020"):
        meta = json.loads(C.http_get("https://dataverse.harvard.edu/api/datasets/:persistentId/?persistentId=" + RH.RV80FW,
                                     retries=2, timeout=60))
        fid = {f["dataFile"]["filename"]: f["dataFile"]["id"] for f in meta["data"]["latestVersion"]["files"]}
        src = "US 2020 General Official Election Results in Tabular Format (Harvard Dataverse doi:10.7910/DVN/RV80FW, CC0)"
        for o, pat in (("senate", None), ("governor", r"^(?!.*\bLieutenant\b)(?!.*\bLt\.?\s).*\bGovernor\b")):
            if not want("%s_2020" % o):
                continue
            d = load(o, "2020")
            entries, missing = {}, {}
            for r in d["races"]:
                st = r["state"]
                if st + "_tabular.tab" not in fid:
                    missing[r["id"]] = "no county file"
                    continue
                p = pat or (r"^US Senate [A-Z]{2}\s*\(" if r.get("special") else r"^US Senate [A-Z]{2}\s*$")
                try:
                    units = rv80fw_units(st, p, fid, cache)
                    if not units["unit"] and not pat:   # a state's only Senate race may carry a term note (AZ)
                        units = rv80fw_units(st, r"^US Senate [A-Z]{2}\b", fid, cache)
                except Exception as e:  # noqa: BLE001
                    missing[r["id"]] = "county file unavailable (%s)" % str(e)[:40]
                    continue
                e, why = assemble(r, units, st, src)
                if e:
                    entries[r["id"]] = e
                else:
                    missing[r["id"]] = why
            write(o, "2020", entries, missing)

    for key in ("offcycle_latest", "offcycle_previous"):
        if want("governor_" + key):
            build_offcycle(key)
    write_state_extracts()
    return 0


if __name__ == "__main__":
    sys.exit(main())
