#!/usr/bin/env python3
"""Texas early voters by last party primary, built from counties' own public
rosters (our replacement for theoelections' party split, whose data files
closed to automated access on 2026-10-22).

For each county wired in COUNTIES:
  * primary history: the county's official voter rosters for the party
    primaries (and runoffs), each voter's county ID and party. The last primary
    a voter took part in gives their party (D / R); none = no primary in the
    span. Built once into a local lookup (LOCAL_DIR, outside the repo: it is
    voter-level, so it never goes to GitHub).
  * this election: the county's cumulative early-voting and ballot-by-mail
    rosters (county ID, precinct), downloaded when they change.
  * congressional district: each precinct's district, from the 2026 primary
    rosters' district columns (the 2025 plan used for 2026).
Only totals are written: data/tx/roster_party.json, per county: voters by
party, by method (mail / in person) and by congressional district.

Harris: harrisvotes.com "Election Rosters" (app.harrisvotes.com/ElectionResults/
ElectionRosters): official rosters 0322, 0522, 0324, 0524, 0326, 0526 and the
2026 general's Cumulative_EV_ToPost_1126.zip / Cumulative_BBM_ToPost_1126.zip.

Runs on Sam's PC from the Georgia reader's scheduled task (local_ga_reader), or
by hand:  python scripts/tx_rosters.py [--rebuild] [--force]
"""
import csv
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

LOCAL_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects", "Politics", "tx-rosters")
OUT_PATH = os.path.join(ROOT, "data", "tx", "roster_party.json")
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

COUNTIES = {
    "Harris": {
        "base": "https://appfiles.harrisvotes.com/harrisvotes/prd/Reports/",
        "page": "https://app.harrisvotes.com/ElectionResults/ElectionRosters",
        # oldest first: a later primary overrides an earlier one
        "primaries": [("2022-03", "0322_VoterHistory_BothParties.xlsx"), ("2022-05", "0522_VoterHistory_BothParties.xlsx"),
                      ("2024-03", "0324 Official Voter Rosters.zip"), ("2024-05", "0524_VoterHistory_Both Parties.xlsx"),
                      ("2026-03", "0326_OfficialRoster.zip"), ("2026-05", "0526_OfficialRoster.zip")],
        "general": {"inp": "Cumulative_EV_ToPost_1126.zip", "mail": "Cumulative_BBM_ToPost_1126.zip"},
    },
}


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
    changed = not cache
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


def tally(voters, lookup):
    def z():
        return {"voters": 0, "dem": 0, "rep": 0, "none": 0}
    tot, by_m, by_cd, unmapped = z(), {"mail": z(), "inp": z()}, {}, 0
    last, pcd = lookup["last"], lookup["precinct_cd"]
    dates = []
    for vid, (method, prec, when) in voters.items():
        p = {"D": "dem", "R": "rep"}.get(last.get(vid), "none")
        cd = pcd.get(prec)
        if cd:
            d = by_cd.setdefault(cd, dict(z(), mail=z(), inp=z()))
        for b in (tot, by_m[method]) + ((d, d[method]) if cd else ()):
            b["voters"] += 1
            b[p] += 1
        if not cd:
            unmapped += 1
        if when:
            dates.append(when)
    return tot, by_m, by_cd, unmapped, max(dates, key=lambda d: datetime.strptime(d, "%m/%d/%Y")) if dates else ""


DIST_PATH = os.path.join(ROOT, "data", "tx", "districts.json")
CD_GEO = os.path.join(ROOT, "assets", "tx-cd.geojson")


def _blk(t):
    return C.party_block(t["rep"], t["dem"], 0, t["none"])


def write_districts(counties):
    """data/tx/districts.json for the Texas page's congressional-district view: the wired counties' early
    voters in each district (a district reaching into counties not wired yet shows only its part here)."""
    geo = json.load(open(CD_GEO, encoding="utf-8"))
    gidx = {f["properties"]["district_number"]: f["properties"] for f in geo["features"]}
    sums = {}
    for name, c in counties.items():
        for cd, t in (c.get("cd") or {}).items():
            a = sums.setdefault(int(cd), {"all": {"voters": 0, "dem": 0, "rep": 0, "none": 0},
                                          "mail": {"voters": 0, "dem": 0, "rep": 0, "none": 0},
                                          "inp": {"voters": 0, "dem": 0, "rep": 0, "none": 0}})
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
        t = {"voters": 0, "dem": 0, "rep": 0, "none": 0}
        for a in sums.values():
            for f in t:
                t[f] += a[key][f]
        return _blk(t)
    names = sorted(counties)
    through = max((c.get("through", "") for c in counties.values()), default="")
    body = {"state": "tx", "unit_label": "Congressional District", "unit_label_plural": "Congressional Districts",
            "partisan": True, "party_methods": ["cast", "mail_voted", "early_voted"],
            "party_basis": "the last party primary each voter voted in (2022-2026)",
            "source": "Official county rosters (%s): the 2026 general's early-voting and mail rosters matched to the "
                      "2022-2026 primary rosters; districts from the 2026 primary rosters" % ", ".join(names),
            "methods_present": ["mail_voted", "early_voted"],
            "method_labels": {"cast": "Ballots cast", "mail_voted": "Mail ballots", "early_voted": "Voted early in person"},
            "party_labels": {"rep": "Republican primary voters", "dem": "Democratic primary voters", "npa": "No primary 2022-2026"},
            "party_hints": {"cast": "Early voters by the last party primary they voted in (2022-2026), matched from the county's official rosters."},
            "coverage": {"ok": len(units), "total": len(gidx), "unmatched": []}, "partial": True, "as_of": through,
            "statewide": {"cast": tot("all"), "mail_voted": tot("mail"), "early_voted": tot("inp")},
            "counties": units,
            "map_note": ("So far this covers %s County only: each district shows its %s County early voters, so a "
                         "district reaching into other counties shows only that part, and districts elsewhere are gray. "
                         "Party is the last party primary each voter voted in (2022-2026), matched from the county's "
                         "official primary rosters; none = no primary in that span, including new voters. Through %s."
                         % (" and ".join(names), " and ".join(names), through))}
    with open(DIST_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(dict(body, generated_at=C.utc_now_iso(), source_compiled=through,
                       source_compiled_iso=C.utc_now_iso()), f, separators=(",", ":"))


def main():
    force = "--force" in sys.argv
    out = (json.load(open(OUT_PATH, encoding="utf-8")) if os.path.exists(OUT_PATH) else {}) or {}
    counties = out.get("counties", {})
    changed = force or not out
    for county, spec in COUNTIES.items():
        lookup = build_primary_lookup(county, spec, force="--rebuild" in sys.argv)
        voters, stamps = general_voters(county, spec)
        if not force and counties.get(county, {}).get("stamps") == stamps:
            print("tx rosters: %s unchanged (%s)" % (county, stamps))
            continue
        changed = True
        tot, by_m, by_cd, unmapped, through = tally(voters, lookup)
        counties[county] = dict(tot, mail=by_m["mail"], inp=by_m["inp"], cd=by_cd, cd_unmapped=unmapped,
                                through=through, stamps=stamps, page=spec["page"],
                                primaries=[c for c, _ in spec["primaries"]], primary_counts=lookup.get("counts", {}))
        print("tx rosters: %s %d voters through %s: D %d R %d none %d (mail %d, in person %d); %d districts, %d unmapped"
              % (county, tot["voters"], through, tot["dem"], tot["rep"], tot["none"], by_m["mail"]["voters"],
                 by_m["inp"]["voters"], len(by_cd), unmapped))
    if not changed:   # nothing new: leave both files (and their timestamps) alone, so no commit
        return 0
    write_districts(counties)
    body = {"generated_at": C.utc_now_iso(), "method": "last party primary (official county rosters, 2022-2026 primaries "
                                                       "and runoffs) matched to the 2026 general's early-voting and mail rosters",
            "counties": counties}
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(body, f, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
