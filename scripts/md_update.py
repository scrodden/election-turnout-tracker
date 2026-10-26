#!/usr/bin/env python3
"""Fetch Maryland turnout by county and registered party, by voting method.

Source: MD State Board of Elections "Official Turnout (By Party and County)" PDF.
It has one section per party (Democrat / Republican / Libertarian / Unaffiliated
/ Other Parties); each section is a county table with columns:
  Election Day, Early Voting, Vote By Mail, Provisional, Eligible Voters, Turnout%
So we get full party x method x county. Party map: Democrat->dem, Republican->rep,
Unaffiliated->npa, Libertarian + Other Parties->oth. Per-county registered =
sum of the parties' "Eligible Voters".

The 2026 general file publishes under press_room/2026_stats/ once early voting /
mail is under way; until a candidate file is reachable this writes an empty
snapshot (reads 0) and lights up automatically. Requires pypdf (the MD workflow
step installs it).

Run:  python scripts/md_update.py [--force]
      python scripts/md_update.py --validate   (parse the persisted 2024 file to prove the parser)
"""
import os
import re
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "md"
CONFIG_PATH = os.path.join(ROOT, "config", "md.json")
GEO_PATH = os.path.join(ROOT, "assets", "md-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")

METHODS = ["election_day", "early_voted", "mail_voted", "provisional"]  # PDF column order
VOTED_METHODS = ["election_day", "early_voted", "mail_voted", "provisional"]
INT_RE = re.compile(r"^[\d,]+$")


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def norm(name):
    n = name.lower().replace("saint ", "st ").replace("st. ", "st ")
    n = re.sub(r"\s+county$", "", n)
    return re.sub(r"[^a-z0-9]", "", n)


def geo_index():
    idx = {}
    for ft in load(GEO_PATH)["features"]:
        p = ft["properties"]
        idx[norm(p["name"])] = {"name": p["name"], "fips": p["fips"]}
    return idx


def parse_by_party(full, cfg):
    """Return {party_std: {county_name: [eday, early, vbm, prov, eligible]}}."""
    party_headers = cfg["source"]["party_headers"]
    skip = set(cfg["source"]["header_tokens"])
    lines = [l.strip() for l in full.split("\n") if l.strip()]
    res = {}
    cur = None
    namebuf, nums = [], []
    i = 0
    while i < len(lines):
        t = lines[i]
        if t in party_headers:
            cur = party_headers[t]; res.setdefault(cur, {}); namebuf, nums = [], []; i += 1; continue
        if cur is None:
            i += 1; continue
        if t in skip or ":" in t or "Official Turnout" in t:
            namebuf, nums = [], []; i += 1; continue
        if INT_RE.match(t):
            nums.append(int(t.replace(",", "")))
            if len(nums) == 5:
                if i + 1 < len(lines) and lines[i + 1].endswith("%"):
                    i += 1
                name = " ".join(namebuf).strip()
                if name and name.upper() != "TOTAL":
                    slot = res[cur].setdefault(name, [0, 0, 0, 0, 0])
                    for k in range(5):
                        slot[k] += nums[k]
                namebuf, nums = [], []
            i += 1; continue
        if t.endswith("%"):
            namebuf, nums = [], []; i += 1; continue
        if nums:
            nums = []
        namebuf.append(t); i += 1
    return res


def build_snapshot(cfg, parsed, idx, src_label):
    # county -> method -> {party: count}, plus registered
    counties = {}
    unmatched = set()
    for party, rows in parsed.items():
        for name, vals in rows.items():
            m = idx.get(norm(name))
            if not m:
                unmatched.add(name); continue
            c = counties.setdefault(m["name"], {"fips": m["fips"],
                                                "_m": {k: {"rep": 0, "dem": 0, "oth": 0, "npa": 0} for k in METHODS},
                                                "registered": 0})
            for ki, mkey in enumerate(METHODS):
                c["_m"][mkey][party] += vals[ki]
            c["registered"] += vals[4]  # eligible voters of this party

    counties_out = {}
    for name, c in counties.items():
        ent = {"fips": c["fips"], "registered": c["registered"]}
        for mkey in METHODS:
            s = c["_m"][mkey]
            if any(s.values()):
                ent[mkey] = C.party_block(s["rep"], s["dem"], s["oth"], s["npa"])
        voted = [ent[m] for m in VOTED_METHODS if ent.get(m)]
        ent["cast"] = C.add_blocks(*voted) if voted else C.party_block(0, 0, 0, 0)
        ent["turnout_pct"] = C.pct(ent["cast"]["total"], c["registered"])
        counties_out[name] = ent

    statewide = {}
    for mkey in METHODS:
        blocks = [counties_out[n][mkey] for n in counties_out if counties_out[n].get(mkey)]
        if blocks:
            statewide[mkey] = C.add_blocks(*blocks)
    voted = [statewide[m] for m in VOTED_METHODS if statewide.get(m)]
    statewide["cast"] = C.add_blocks(*voted) if voted else C.party_block(0, 0, 0, 0)
    statewide["registered"] = sum(c.get("registered", 0) for c in counties_out.values())
    statewide["turnout_pct"] = C.pct(statewide["cast"]["total"], statewide["registered"])

    methods_present = sorted({m for c in counties_out.values() for m in METHODS if c.get(m)})
    snap = {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"],
        "source": {"primary": "MD State Board of Elections Official Turnout (By Party and County)"},
        "source_compiled": src_label, "source_compiled_iso": (C.utc_now_iso() if counties_out else ""),
        "methods_present": methods_present, "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties_out,
    }
    return snap, unmatched


def read_pdf_text(raw):
    import io
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(raw))
    return "\n".join(p.extract_text() for p in reader.pages)


def parse_mail_xlsx(raw, idx):
    """Pre-election daily 'Mail-in Sent and Returned' by county workbook
    (press_room/2026_stats/GG26/Absentees_Sent_and_Returned_by_County.xlsx).
    Sheet1 rows: CATEGORY | COUNTY NAME | DISTRICT | .. | DEM/REP/OTH/TOTAL SENT |
    DEM/REP/OTH/TOTAL RECEIVED; the 'ALL' category is each county's total.
    MD lumps unaffiliated voters into OTH, so OTH -> oth (npa stays 0).
    -> (as_of text, {geo_name: {"fips", "sent": {..}, "recv": {..}}}, unmatched)"""
    sheets = C.read_xlsx(raw, positional=True)
    rows = next(iter(sheets.values()), [])
    as_of = ""
    for r in rows[:6]:
        for c in r:
            m = re.search(r"As of:\s*([A-Za-z]+ \d{1,2}, \d{4}(?: \d{1,2}(?::\d{2})? ?[AP]M)?)", c)
            if m:
                as_of = m.group(1)
    head = next((r for r in rows if "COUNTY NAME" in [c.strip().upper() for c in r]), None)
    if not head:
        raise RuntimeError("MD: header row not found")
    H = {c.strip().upper(): i for i, c in enumerate(head) if c.strip()}
    need = ["CATEGORY", "COUNTY NAME", "DEM SENT", "REP SENT", "OTH SENT", "TOTAL SENT",
            "DEM RECEIVED", "REP RECEIVED", "OTH RECEIVED", "TOTAL RECEIVED"]
    if any(k not in H for k in need):
        raise RuntimeError("MD: unexpected columns %s" % list(H))

    def num(r, k):
        v = r[H[k]] if H[k] < len(r) else ""
        return int(float(v)) if str(v).replace(".", "", 1).isdigit() else 0
    out, unmatched = {}, []
    for r in rows:
        if len(r) <= H["COUNTY NAME"] or r[H["CATEGORY"]].strip().upper() != "ALL":
            continue
        name = r[H["COUNTY NAME"]].strip()
        g = idx.get(norm(name))
        if not g:
            unmatched.append(name)
            continue
        sent = {"dem": num(r, "DEM SENT"), "rep": num(r, "REP SENT"), "oth": num(r, "OTH SENT"), "npa": 0}
        recv = {"dem": num(r, "DEM RECEIVED"), "rep": num(r, "REP RECEIVED"), "oth": num(r, "OTH RECEIVED"), "npa": 0}
        if sum(sent.values()) != num(r, "TOTAL SENT") or sum(recv.values()) != num(r, "TOTAL RECEIVED"):
            raise RuntimeError("MD: party columns don't add up for %s" % name)
        out[g["name"]] = {"fips": g["fips"], "sent": sent, "recv": recv}
    return as_of, out, unmatched


def parse_early_raw(text, idx):
    """In-person early voting from the SBE's 'EarlyVoting RAW data' CSV (GG26/EarlyVoting_RAW_data.csv): one row per
    county x congressional / legislative district x precinct x status x gender x age group x party x early-vote
    center, with that row's voters on Day1..Day8. Party DEM / REP; everything else (UNA, OTH, WCP, GRN) -> oth, as
    the mail-in reports count unaffiliated voters in Other.
    -> (days reported, {geo county name: {dem, rep, oth, npa}}, {cd number: {...}}, unmatched counties)"""
    import csv
    import io
    rows = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    head = [h.strip() for h in next(rows)]
    H = {h: i for i, h in enumerate(head)}
    days = [h for h in head if re.fullmatch(r"Day\d+", h)]
    if any(k not in H for k in ("COUNTY_NAME", "congressional_district_code", "PARTY_CODE")) or not days:
        raise RuntimeError("MD early voting: unexpected columns %s" % head[:12])
    counties, cds, unmatched, reported = {}, {}, set(), set()

    def z():
        return {"dem": 0, "rep": 0, "oth": 0, "npa": 0}
    for r in rows:
        if len(r) < len(head):
            continue
        n = 0
        for d in days:
            v = r[H[d]].strip()
            if v.isdigit() and int(v):
                n += int(v)
                reported.add(d)
        if not n:
            continue
        party = {"DEM": "dem", "REP": "rep"}.get(r[H["PARTY_CODE"]].strip().upper(), "oth")
        g = idx.get(norm(r[H["COUNTY_NAME"]]))
        if not g:
            unmatched.add(r[H["COUNTY_NAME"]])
        else:
            counties.setdefault(g["name"], z())[party] += n
        cd = r[H["congressional_district_code"]].strip()
        if cd.isdigit():
            cds.setdefault(str(int(cd)), z())[party] += n
    return len(reported), counties, cds, sorted(unmatched)


def parse_mail_cd_xlsx(raw):
    """The SBE's 'Mail-in Sent and Returned by Congressional District' workbook: 'ALL' rows per district
    ('Congressional District Code 01', county ALL), DEM / REP / OTH (incl. unaffiliated) sent and received.
    -> (as_of, {cd number: {"sent": {..}, "recv": {..}}})"""
    rows = next(iter(C.read_xlsx(raw, positional=True).values()), [])
    as_of = ""
    for r in rows[:6]:
        for c in r:
            m = re.search(r"As of:\s*([A-Za-z]+ \d{1,2}, \d{4}(?: +\d{1,2}(?::\d{2})? ?[AP]M)?)", c)
            if m:
                as_of = re.sub(r"\s+", " ", m.group(1))
    head = next((r for r in rows if "DISTRICT" in [c.strip().upper() for c in r]), None)
    if not head:
        raise RuntimeError("MD mail by district: header row not found")
    H = {c.strip().upper(): i for i, c in enumerate(head) if c.strip()}

    def num(r, k):
        v = r[H[k]] if H[k] < len(r) else ""
        return int(float(v)) if str(v).replace(".", "", 1).isdigit() else 0
    out = {}
    for r in rows:
        if len(r) <= H["DISTRICT"] or r[H["CATEGORY"]].strip().upper() != "ALL" or r[H["COUNTY NAME"]].strip().upper() != "ALL":
            continue
        m = re.search(r"(\d+)\s*$", r[H["DISTRICT"]])
        if not m:
            continue
        sent = {"dem": num(r, "DEM SENT"), "rep": num(r, "REP SENT"), "oth": num(r, "OTH SENT"), "npa": 0}
        recv = {"dem": num(r, "DEM RECEIVED"), "rep": num(r, "REP RECEIVED"), "oth": num(r, "OTH RECEIVED"), "npa": 0}
        if sum(sent.values()) != num(r, "TOTAL SENT"):
            raise RuntimeError("MD mail by district: party columns don't add up for district %s" % m.group(1))
        out[str(int(m.group(1)))] = {"sent": sent, "recv": recv}
    return as_of, out


def _ent(sent, recv, early=None):
    """One unit: mail sent / returned and in-person early votes, each by party -> the page's blocks."""
    def pb(d):
        return C.party_block(d["rep"], d["dem"], d["oth"], d["npa"])
    early = early or {"dem": 0, "rep": 0, "oth": 0, "npa": 0}
    e = {"mail_voted": pb(recv), "mail_provided": pb({k: max(0, sent[k] - recv[k]) for k in sent}),
         "cast": pb({k: recv[k] + early[k] for k in recv}), "registered": 0, "turnout_pct": None}
    if sum(early.values()):
        e["early_voted"] = pb(early)
    m = C.compute_mail(dict(e, cast=None))
    if m:
        e["mail"] = m
    return e


def early_through(cfg, n_days):
    from datetime import date, timedelta
    y, mo, d = (int(x) for x in cfg["source"].get("early_start", "2026-10-22").split("-"))
    return (date(y, mo, d) + timedelta(days=n_days - 1)).strftime("%B %-d" if os.name != "nt" else "%B %#d") if n_days else ""


def write_districts(cfg, early_cd, n_days):
    """data/md/districts_cd.json: the official mail-in by congressional district workbook plus in-person early votes
    by district from the raw early-voting file."""
    as_of, mail = parse_mail_cd_xlsx(C.http_get(cfg["source"]["mail_by_cd"], binary=True, no_cache=True, retries=2))
    geo = load(os.path.join(ROOT, "assets", "md-cd.geojson"))
    gidx = {str(f["properties"]["district_number"]): f["properties"] for f in geo["features"]}
    if sorted(mail) != sorted(gidx):
        raise RuntimeError("MD mail by district: districts %s vs the map's %s" % (sorted(mail), sorted(gidx)))
    zero = {"dem": 0, "rep": 0, "oth": 0, "npa": 0}
    units = {gidx[n]["name"]: dict(_ent(r["sent"], r["recv"], (early_cd or {}).get(n)), fips=gidx[n]["fips"])
             for n, r in mail.items()}
    S, Rv, E = dict(zero), dict(zero), dict(zero)
    for n, r in mail.items():
        for k in zero:
            S[k] += r["sent"][k]
            Rv[k] += r["recv"][k]
            E[k] += (early_cd or {}).get(n, zero)[k]
    thr = early_through(cfg, n_days)
    body = {"state": STATE, "election": cfg["election"], "unit_label": "District", "unit_label_plural": "Districts",
            "partisan": True,
            "source": "MD State Board of Elections: Mail-in Sent and Returned by Congressional District (as of %s) and "
                      "the early-voting raw data file (in-person early votes by district%s); unaffiliated voters are "
                      "included in Other" % (as_of, ", through " + thr if thr else ""),
            "methods_present": [k for k in ("mail_voted", "early_voted", "mail_provided") if (_ent(S, Rv, E).get(k) or {}).get("total")],
            "method_labels": cfg.get("method_labels", {}), "coverage": {"ok": len(units), "total": len(gidx), "unmatched": []},
            "as_of": as_of, "statewide": _ent(S, Rv, E), "counties": units}
    out = os.path.join(DATA_DIR, "districts_cd.json")
    prev = load(out) if os.path.exists(out) else {}
    h = C.data_hash(body)
    if h == prev.get("data_hash"):
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(dict(body, data_hash=h, generated_at=C.utc_now_iso(), source_compiled=as_of,
                       source_compiled_iso=C.utc_now_iso()), f, separators=(",", ":"))
    print("md districts: CHANGED %d districts, mail returned %d, early in person %d" % (len(units), sum(Rv.values()), sum(E.values())))


def mail_snapshot(cfg, as_of, rows, url, early=None, n_days=0):
    """The mail-in workbook's counties, plus in-person early votes by county (parse_early_raw) when there are any:
    cast = mail returned + early in person."""
    early = early or {}
    zero = {"dem": 0, "rep": 0, "oth": 0, "npa": 0}
    counties, S, R, E = {}, dict(zero), dict(zero), dict(zero)
    for name, r in rows.items():
        counties[name] = dict(_ent(r["sent"], r["recv"], early.get(name)), fips=r["fips"])
        for k in S:
            S[k] += r["sent"][k]
            R[k] += r["recv"][k]
            E[k] += early.get(name, zero)[k]
    statewide = _ent(S, R, E)
    thr = early_through(cfg, n_days)
    return {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"], "partisan": True,
        "source": {"primary": "MD State Board of Elections 'Mail-in Sent and Returned' by county (2026 general), as of %s%s; "
                              "unaffiliated voters are included in Other" % (
                                  as_of, "; in-person early voting from its raw data file, through " + thr if thr else ""),
                   "url": url, "as_of": as_of},
        "source_compiled": as_of, "source_compiled_iso": C.utc_now_iso(),
        "methods_present": [k for k in ("mail_voted", "early_voted", "mail_provided") if (statewide.get(k) or {}).get("total")],
        "method_labels": cfg.get("method_labels", {}), "statewide": statewide, "counties": counties,
    }


def main():
    force = "--force" in sys.argv
    validate = "--validate" in sys.argv
    cfg = load(CONFIG_PATH)
    idx = geo_index()

    if validate:
        raw = C.http_get(cfg["source"]["validation_url"], binary=True, no_cache=True)
        snap, unmatched = build_snapshot(cfg, parse_by_party(read_pdf_text(raw), cfg), idx, "validation")
        sw = snap["statewide"]
        print("Validation: matched counties=%d  methods=%s" % (len(snap["counties"]), snap["methods_present"]))
        for m in ["early_voted", "mail_voted", "election_day", "cast"]:
            b = sw.get(m)
            if b:
                print("  %-12s total=%s R=%s D=%s NPA=%s Oth=%s margin=%s"
                      % (m, b["total"], b["rep"], b["dem"], b["npa"], b["oth"], b["margin"]))
        if unmatched:
            print("  UNMATCHED:", sorted(unmatched))
        return 0

    parsed = {}
    src_label = ""
    base = cfg["source"]["base"]
    for fn in cfg["source"]["general_candidates"]:
        try:
            raw = C.http_get(base + fn.replace(" ", "%20"), binary=True, no_cache=True, retries=1)
            parsed = parse_by_party(read_pdf_text(raw), cfg)
            if parsed:
                src_label = cfg["election"]["name"]; break
        except Exception:  # noqa: BLE001 - file not posted yet
            continue
    if not parsed:
        # before the official turnout file exists: the daily mail-in sent/returned workbook
        mail_url = cfg["source"].get("mail_by_county")
        try:
            as_of, rows, unmatched = parse_mail_xlsx(C.http_get(mail_url, binary=True, retries=2), idx)
            if unmatched:
                raise RuntimeError("MD: unmatched counties %s" % unmatched)
        except Exception as e:  # noqa: BLE001
            print("MD mail-in workbook unavailable: %s" % str(e)[:140])
            rows = {}
        early, early_cd, n_days = {}, {}, 0
        try:   # in-person early voting (Oct 22 - 29), by county and congressional district
            n_days, early, early_cd, bad = parse_early_raw(
                C.http_get(cfg["source"]["early_raw"], no_cache=True, retries=2, timeout=120), idx)
            if bad:
                raise RuntimeError("unmatched counties %s" % bad)
        except Exception as e:  # noqa: BLE001
            print("MD early-voting raw data unavailable: %s" % str(e)[:140], file=sys.stderr)
            early, early_cd, n_days = {}, {}, 0
        try:
            write_districts(cfg, early_cd, n_days)
        except Exception as e:  # noqa: BLE001 - the district view must never sink the county update
            print("MD districts not written: %s" % str(e)[:140], file=sys.stderr)
        if rows:
            snap = mail_snapshot(cfg, as_of, rows, mail_url, early, n_days)
        else:
            import lab_standin as LAB   # nothing official yet -> UF Election Lab stand-in
            if LAB.run(STATE, cfg, GEO_PATH, LATEST_PATH, HISTORY_PATH, partisan=True, force=force):
                return 0
            print("no live file yet (candidates not reachable)")
            snap = None
        if snap:
            prev = load(LATEST_PATH) if os.path.exists(LATEST_PATH) else {}
            snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k != "source_compiled_iso"})
            changed = force or snap["data_hash"] != prev.get("data_hash")
            snap["generated_at"] = C.utc_now_iso()
            if not changed:
                print("NOCHANGE  (mail-in as of %s)" % as_of)
                return 0
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(LATEST_PATH, "w", encoding="utf-8") as f:
                json.dump(snap, f, separators=(",", ":"))
            c = snap["statewide"]["cast"]
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"generated_at": snap["generated_at"],
                                    "statewide": {"cast": [c["rep"], c["dem"], c["oth"], c["npa"], c["total"]]}},
                                   separators=(",", ":")) + "\n")
            m = snap["statewide"].get("mail") or {}
            print("CHANGED  MD mail-in as of %s: %d counties | cast=%d (mail %d of %d sent, early in person %d) "
                  "(R%d D%d Oth%d) margin=%s" % (as_of, len(snap["counties"]), c["total"], m.get("returned", 0),
                                                m.get("requested", 0), (snap["statewide"].get("early_voted") or {}).get("total", 0),
                                                c["rep"], c["dem"], c["oth"], c["margin"]))
            return 0

    snap, unmatched = build_snapshot(cfg, parsed, idx, src_label)
    snap["data_hash"] = C.data_hash(snap)
    snap["generated_at"] = C.utc_now_iso()

    prev = None
    if os.path.exists(LATEST_PATH):
        try:
            prev = load(LATEST_PATH).get("data_hash")
        except (ValueError, OSError):
            pass
    changed = force or (snap["data_hash"] != prev)
    os.makedirs(DATA_DIR, exist_ok=True)
    if changed:
        with open(LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        cast = snap["statewide"]["cast"]
        if cast["total"]:
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"generated_at": snap["generated_at"], "compiled": src_label,
                                    "data_hash": snap["data_hash"], "cast": cast["total"],
                                    "margin": cast["margin"]}, separators=(",", ":")) + "\n")
        print("CHANGED  counties=%d  cast=%s (R%s D%s NPA%s) margin=%s methods=%s"
              % (len(snap["counties"]), cast["total"], cast["rep"], cast["dem"], cast["npa"],
                 cast["margin"], snap["methods_present"]))
    else:
        print("NOCHANGE  (hash %s)" % (prev or "")[:12])
    if unmatched:
        print("  unmatched:", sorted(unmatched)[:10])
    return 0


if __name__ == "__main__":
    sys.exit(main())
