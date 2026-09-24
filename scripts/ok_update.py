#!/usr/bin/env python3
"""Oklahoma absentee ballots by county AND registered party.

Source: Oklahoma State Election Board "Absentee Voting Statistics" page, which
links two kinds of county-by-party PDFs during an election's final stretch:
  ballot-totals-by-affiliation-*.pdf  (report ap2520, mail absentee)
      per county: 'NN COUNTY Party sent undeliverable received' + party lines
  absentee-inperson-totals-*.pdf      (report ap2541, in-person absentee/early)
      per county page: 'County: NAME' then 'Party count' lines
Each file prints the election date it covers; only files for the configured
election date are used, so primary/runoff reports are ignored. Totals are
entered by the 77 county election boards and posted a few times a day.

Republican->rep, Democrat->dem, Independent->npa, Libertarian->oth.
mail_voted = received; mail_provided = sent - undeliverable - received
(outstanding) -> ballot chase; early_voted = in-person; cast = both.

Run:  python scripts/ok_update.py [--force]
      python scripts/ok_update.py --test-mail URL --test-inperson URL --test-date M/D/YYYY
      (parse given files for another election without writing)
"""
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "ok"
CONFIG_PATH = os.path.join(ROOT, "config", "ok.json")
GEO_PATH = os.path.join(ROOT, "assets", "ok-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
BASE = "https://oklahoma.gov"

PARTY = {"republican": "rep", "democrat": "dem", "independent": "npa", "libertarian": "oth"}
PARTY_RE = "Republican|Democrat|Independent|Libertarian"


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _n(s):
    return int(s.replace(",", ""))


def _same_date(a, b):
    try:
        return [int(x) for x in a.split("/")] == [int(x) for x in b.split("/")]
    except ValueError:
        return False


def _text(url):
    from pypdf import PdfReader
    raw = C.http_get(url, binary=True, retries=2)
    return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(raw)).pages)


def _file_key(url):
    """Sort key from the posting date in the file name (MMDDYYYY or YYYYMMDD, optional -pm / -N)."""
    name = url.rsplit("/", 1)[-1].lower()
    m = re.search(r"(\d{8})", name)
    if not m:
        return ("", 0)
    d = m.group(1)
    ymd = d if d.startswith("20") else d[4:] + d[:4]
    return (ymd, 1 if ("pm" in name or re.search(r"-\d\.pdf$", name)) else 0)


def newest_links(src):
    html = C.http_get(src["stats_page"], retries=2)
    links = [BASE + l if l.startswith("/") else l for l in re.findall(r'href="([^"]+\.pdf)"', html)]
    year = src.get("year", "2026")
    mail = [l for l in links if "ballot-totals-by-affiliation" in l and "%s-ballot-totals" % year in l]
    inp = [l for l in links if "absentee-in-person-totals-by-affiliation" in l and "%s-absentee-in-person" % year in l
           and "inperson" in l.rsplit("/", 1)[-1].replace("-", "")]
    pick = lambda xs: max(xs, key=_file_key) if xs else None   # noqa: E731
    return pick(mail), pick(inp)


def parse_mail(text):
    """-> (election date, {COUNTY: {party: [sent, undeliverable, received]}})"""
    ed = re.search(r"Ballot Totals by Affiliation\s*\n\s*(\d{1,2}/\d{1,2}/\d{4})", text)
    out, cur = {}, None
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^(\d{2}) ([A-Z][A-Z .'-]*?) (%s) ([\d,]+) ([\d,]+) ([\d,]+)$" % PARTY_RE, line)
        if m:
            cur = m.group(2).strip()
            out.setdefault(cur, {})[PARTY[m.group(3).lower()]] = [_n(m.group(i)) for i in (4, 5, 6)]
            continue
        m = re.match(r"^(%s) ([\d,]+) ([\d,]+) ([\d,]+)$" % PARTY_RE, line)
        if m and cur:
            out[cur][PARTY[m.group(1).lower()]] = [_n(m.group(i)) for i in (2, 3, 4)]
    return (ed.group(1) if ed else ""), out


def parse_inperson(text):
    """-> (election date, {COUNTY: {party: count}})"""
    out, dates = {}, set()
    for block in re.split(r"County:\s*", text)[1:]:
        name = block.splitlines()[0].strip()
        d = re.search(r"^\s*(\d{2}/\d{2}/\d{4})\s*$", block, re.M)
        if d:
            dates.add(d.group(1))
        parties = {PARTY[p.lower()]: _n(n) for p, n in re.findall(r"^(%s) ([\d,]+)\s*$" % PARTY_RE, block, re.M)}
        if name and parties:
            out[name] = parties
    return (sorted(dates)[0] if len(dates) == 1 else ""), out


def _pb(d):
    return C.party_block(d.get("rep", 0), d.get("dem", 0), d.get("oth", 0), d.get("npa", 0))


def build(mail, inp):
    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}
    counties, unmatched = {}, []
    sw = {k: {"rep": 0, "dem": 0, "oth": 0, "npa": 0} for k in ("mail_voted", "mail_provided", "early_voted")}
    for name in sorted((set(mail) | set(inp)) - {"STATE"}):   # 'STATE' = the report's own total page
        g = gidx.get(_norm(name))
        if not g:
            unmatched.append(name)
            continue
        parts = {"mail_voted": {}, "mail_provided": {}, "early_voted": {}}
        for p, (sent, undel, recv) in (mail.get(name) or {}).items():
            parts["mail_voted"][p] = recv
            parts["mail_provided"][p] = max(0, sent - undel - recv)
        for p, n in (inp.get(name) or {}).items():
            parts["early_voted"][p] = n
        ent = {"fips": g["fips"], "registered": 0, "turnout_pct": None}
        for k, d in parts.items():
            ent[k] = _pb(d)
            for p, v in d.items():
                sw[k][p] += v
        ent["cast"] = C.add_blocks(ent["mail_voted"], ent["early_voted"])
        m = C.compute_mail(ent)
        if m:
            ent["mail"] = m
        counties[g["name"]] = ent
    statewide = {k: _pb(v) for k, v in sw.items()}
    statewide["cast"] = C.add_blocks(statewide["mail_voted"], statewide["early_voted"])
    statewide["registered"] = 0
    statewide["turnout_pct"] = None
    m = C.compute_mail(statewide)
    if m:
        statewide["mail"] = m
    # cross-check against the report's statewide page
    st_mail = sum(v[2] for v in (mail.get("STATE") or {}).values())
    st_inp = sum((inp.get("STATE") or {}).values())
    if (mail.get("STATE") and st_mail != statewide["mail_voted"]["total"]) or \
       (inp.get("STATE") and st_inp != statewide["early_voted"]["total"]):
        raise RuntimeError("OK: county rows don't add up to the report's STATE page")
    return statewide, counties, unmatched


def _arg(n):
    return sys.argv[sys.argv.index(n) + 1] if n in sys.argv else None


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg.get("source", {})
    want = _arg("--test-date") or src.get("election_date", "11/3/2026")
    test = bool(_arg("--test-mail") or _arg("--test-inperson"))
    try:
        mail_url, inp_url = (_arg("--test-mail"), _arg("--test-inperson")) if test else newest_links(src)
        mail, inp, seen = {}, {}, {}
        if mail_url:
            d, rows = parse_mail(_text(mail_url))
            seen["mail"] = d
            if _same_date(d, want):
                mail = rows
        if inp_url:
            d, rows = parse_inperson(_text(inp_url))
            seen["in_person"] = d
            if _same_date(d, want):
                inp = rows
    except Exception as e:  # noqa: BLE001
        print("OK fetch/parse failed: %s" % str(e)[:160], file=sys.stderr)
        return 0
    if not mail and not inp:
        print("ok: waiting — newest reports are for %s, not the %s general." % (seen or "nothing", want))
        return 0

    statewide, counties, unmatched = build(mail, inp)
    if test:
        c = statewide["cast"]
        print("TEST %s: %d counties | mail recv=%d outstanding=%d | in-person=%d | cast=%d (R%d D%d I%d L%d) | unmatched=%s"
              % (want, len(counties), statewide["mail_voted"]["total"], statewide["mail_provided"]["total"],
                 statewide["early_voted"]["total"], c["total"], c["rep"], c["dem"], c["npa"], c["oth"], unmatched))
        return 0

    methods_present = [k for k in ("mail_voted", "early_voted", "mail_provided") if statewide[k]["total"]]
    snap = {
        "state": STATE, "state_name": cfg.get("state_name", "Oklahoma"), "election": cfg.get("election", {}),
        "partisan": True,
        "source": {"primary": "Oklahoma State Election Board absentee statistics (county x party PDFs)",
                   "mail_report": mail_url if mail else None, "inperson_report": inp_url if inp else None},
        "source_compiled": "", "source_compiled_iso": C.utc_now_iso(),
        "methods_present": methods_present, "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties,
    }
    prev = load(LATEST_PATH, {}) or {}
    snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k not in ("source_compiled_iso",)})
    snap["generated_at"] = C.utc_now_iso()
    if not force and snap["data_hash"] == prev.get("data_hash"):
        print("NOCHANGE  (cast=%d)" % statewide["cast"]["total"])
        return 0
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    c = statewide["cast"]
    if c["total"]:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"generated_at": snap["generated_at"],
                                "statewide": {"cast": [c["rep"], c["dem"], c["oth"], c["npa"], c["total"]]}},
                               separators=(",", ":")) + "\n")
    print("CHANGED  counties=%d cast=%d (R%d D%d I%d) margin=%s%s"
          % (len(counties), c["total"], c["rep"], c["dem"], c["npa"], c["margin"],
             ("  unmatched: %s" % unmatched) if unmatched else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
