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


def mail_snapshot(cfg, as_of, rows, url):
    def pb(d):
        return C.party_block(d["rep"], d["dem"], d["oth"], d["npa"])

    def ent(sent, recv):
        e = {"mail_voted": pb(recv), "mail_provided": pb({k: max(0, sent[k] - recv[k]) for k in sent}),
             "cast": pb(recv), "registered": 0, "turnout_pct": None}
        m = C.compute_mail(e)
        if m:
            e["mail"] = m
        return e
    counties, S, R = {}, {"dem": 0, "rep": 0, "oth": 0, "npa": 0}, {"dem": 0, "rep": 0, "oth": 0, "npa": 0}
    for name, r in rows.items():
        counties[name] = dict(ent(r["sent"], r["recv"]), fips=r["fips"])
        for k in S:
            S[k] += r["sent"][k]
            R[k] += r["recv"][k]
    statewide = ent(S, R)
    return {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"], "partisan": True,
        "source": {"primary": "MD State Board of Elections 'Mail-in Sent and Returned' by county (2026 general), as of %s; "
                              "unaffiliated voters are included in Other" % as_of, "url": url, "as_of": as_of},
        "source_compiled": as_of, "source_compiled_iso": C.utc_now_iso(),
        "methods_present": [k for k in ("mail_voted", "mail_provided") if statewide[k]["total"]],
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
        if rows:
            snap = mail_snapshot(cfg, as_of, rows, mail_url)
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
            print("CHANGED  MD mail-in as of %s: %d counties | received=%d of %d sent (R%d D%d Oth%d) margin=%s"
                  % (as_of, len(snap["counties"]), c["total"], m.get("requested", 0), c["rep"], c["dem"], c["oth"], c["margin"]))
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
