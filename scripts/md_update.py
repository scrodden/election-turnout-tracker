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
        print("no live file yet (candidates not reachable)")

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
