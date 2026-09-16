#!/usr/bin/env python3
"""Build a static 2022 General Election partisan-turnout baseline from the TQV
archive -- the SAME free source as 2026, so it's fully apples-to-apples and
includes ELECTION DAY by party (not just mail + early).

The TQV S3 bucket is publicly listable and keeps prior elections. For each
county we list its election folders, cheaply identify the 2022 General via a
Range request on data.json's Summary, then pull that data.json (party x method,
incl. Mail/EarlyVoting/ElectionDay/Provisional) and registered voters.

Falls back to the DOS 2022 VBM/EV archive PDF (mail + early only) for any county
missing from TQV, and cross-checks totals against the official turnout report.

One-time build (2022 is final). Needs `pypdf` only if the PDF fallback is used.
Output: data/fl/baseline_2022.json
"""
import os
import re
import sys
import json
import urllib.request
import ssl
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

OUT_PATH = os.path.join(ROOT, "data", "fl", "baseline_2022.json")
COUNTIES_PATH = os.path.join(ROOT, "config", "fl_counties.json")
CONFIG_PATH = os.path.join(ROOT, "config", "fl.json")
TURNOUT_URL = "https://results.elections.myflorida.com/TurnoutRpt.asp?ElectionDate=11/8/2022&DATAMODE="
S3 = "https://s3.amazonaws.com/turnoutquickview.electionsfl.org/"
FVRS_2022 = 26906
VOTED = ["mail_voted", "early_voted", "election_day"]
_CTX = ssl.create_default_context()
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0 Safari/537.36"}


def _get(url, rng=None, retries=3):
    hdr = dict(UA)
    if rng:
        hdr["Range"] = "bytes=0-%d" % rng
    for a in range(retries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=hdr),
                                          timeout=40, context=_CTX).read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            if a == retries - 1:
                raise
    return ""


def n(s):
    return int(str(s).replace(",", "").strip() or 0)


def keyname(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def block(r, d, o, npa):
    return {"rep": r, "dem": d, "oth": o, "npa": npa, "total": r + d + o + npa}


def add_eday_mix(ent):
    tot = ent.get("turnout_total") or ((ent.get("mail_voted") or {}).get("total", 0)
                                       + (ent.get("early_voted") or {}).get("total", 0)
                                       + (ent.get("election_day") or {}).get("total", 0)
                                       + (ent.get("provisional") or {}).get("total", 0))
    if not tot:
        return
    def g(k):
        return (ent.get(k) or {}).get("total", 0)
    ent["turnout_total"] = tot
    mail, early, prov = g("mail_voted"), g("early_voted"), g("provisional")
    eday = max(0, tot - mail - early - prov)  # party-less volume (works for fallback counties)
    ent["method_mix"] = {"mail": round(100.0 * mail / tot, 1), "early": round(100.0 * early / tot, 1),
                         "eday": round(100.0 * eday / tot, 1), "prov": round(100.0 * prov / tot, 1)}
    ent["election_day_total"] = eday


# ---- TQV archive ----------------------------------------------------------
def resolve_2022(code):
    try:
        lst = _get("%s?list-type=2&prefix=data/FL/%s/&delimiter=/" % (S3, code))
    except Exception:  # noqa: BLE001
        return None
    ids = re.findall(r"data/FL/%s/(\d+)/" % code, lst)
    for eid in ids:
        try:
            head = _get("%sdata/FL/%s/%s/data.json" % (S3, code, eid), rng=400)
        except Exception:  # noqa: BLE001
            continue
        if re.search(r'"ElectionName"\s*:\s*"2022 General', head):
            return eid
    return None


def parse_tqv_2022(cfg, code, eid):
    party_map = cfg["tqv"]["party_map"]
    method_map = cfg["tqv"]["method_map"]
    d = json.loads(_get("%sdata/FL/%s/%s/data.json" % (S3, code, eid)))
    summ = d.get("Summary", {})
    turnout = d.get("Turnout", {}) or {}
    methods = {}
    for pcode, by_m in (turnout.get("PartyType") or {}).items():
        tgt = party_map.get(str(pcode).upper(), "oth")
        for m_label, cnt in (by_m or {}).items():
            mk = method_map.get(m_label)
            if not mk:
                continue
            slot = methods.setdefault(mk, {"rep": 0, "dem": 0, "oth": 0, "npa": 0})
            slot[tgt] += int(cnt or 0)
    ent = {}
    for mk, s in methods.items():
        ent[mk] = block(s["rep"], s["dem"], s["oth"], s["npa"])
    ent["registered"] = int(summ.get("TotalRegisteredVoters") or 0)
    ent["src"] = "tqv"
    return ent


# ---- DOS turnout report (registration/turnout cross-check + fallback) ------
def parse_turnout():
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", C.http_get(TURNOUT_URL, retries=3)))
    out = {}
    for m in re.finditer(r"([A-Za-z][A-Za-z .'\-]+?)\s+([\d,]+)\s+([\d,]+)\s+([\d.]+)%", txt):
        k = keyname(m.group(1))
        if k in ("county", "total", "statetotal"):
            continue
        out[k] = {"registered": n(m.group(2)), "turnout_total": n(m.group(3)), "turnout_pct": float(m.group(4))}
    return out


# ---- DOS 2022 VBM/EV PDF (fallback: mail + early only) ---------------------
def parse_pdf_fallback():
    try:
        from pypdf import PdfReader
    except ImportError:
        return {}
    import io
    raw = C.http_get("https://files.floridados.gov/media/706191/2022-ge-rptstatuscountsarchive-ev-vbm.pdf",
                     binary=True, timeout=120, retries=3)
    text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages)
    data_re = re.compile(r"^([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)(.+)$")
    county_re = re.compile(r"TOTAL(STATE TOTAL|[A-Z][A-Za-z .'\-]+?)\s*$")
    labels = [("Voted Vote-by-Mail", "mail_voted"), ("Voted Early", "early_voted")]
    out, cur = {}, None
    for line in text.splitlines():
        cm = county_re.search(line.rstrip())
        if cm:
            cur = keyname(cm.group(1))
            out.setdefault(cur, {})
        dm = data_re.match(line.strip())
        if dm and cur:
            mk = next((k for lbl, k in labels if lbl in dm.group(6)), None)
            if mk:
                out[cur][mk] = block(n(dm.group(1)), n(dm.group(2)), n(dm.group(3)), n(dm.group(4)))
    return out


def main():
    cfg = json.load(open(CONFIG_PATH, encoding="utf-8"))
    counties = json.load(open(COUNTIES_PATH, encoding="utf-8"))["counties"]
    turnout = parse_turnout()

    print("Resolving 2022 General ids from TQV archive (67 counties)...")
    ids = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for c, eid in zip(counties, ex.map(lambda c: resolve_2022(c["code"]), counties)):
            ids[c["code"]] = eid

    result = {}
    fallback_codes = []
    pdf = None
    for c in counties:
        code, name = c["code"], c["name"]
        eid = ids.get(code)
        ent = None
        if eid:
            try:
                ent = parse_tqv_2022(cfg, code, eid)
            except Exception as e:  # noqa: BLE001
                print("  ! TQV parse failed %s/%s: %s" % (code, eid, str(e)[:50]))
        if ent is None:
            if pdf is None:
                pdf = parse_pdf_fallback()
            fb = pdf.get(keyname(name), {})
            if fb:
                ent = dict(fb); ent["src"] = "dos-pdf-fallback"
                fallback_codes.append(code)
            else:
                continue
        # registered / turnout from official report (authoritative for turnout_total)
        t = turnout.get(keyname(name), {})
        if t.get("registered"):
            ent["registered"] = t["registered"]
        if t.get("turnout_total"):
            ent["turnout_total"] = t["turnout_total"]
        if t.get("turnout_pct") is not None:
            ent["turnout_pct"] = t["turnout_pct"]
        v = [ent[m] for m in VOTED if ent.get(m)]
        ent["cast"] = block(sum(b["rep"] for b in v), sum(b["dem"] for b in v),
                            sum(b["oth"] for b in v), sum(b["npa"] for b in v))
        add_eday_mix(ent)
        ent["fips"] = c["fips"]; ent["code"] = code
        result[name] = ent

    # statewide = sum of counties
    def sum_method(mk):
        bs = [e[mk] for e in result.values() if e.get(mk)]
        return block(sum(b["rep"] for b in bs), sum(b["dem"] for b in bs),
                     sum(b["oth"] for b in bs), sum(b["npa"] for b in bs)) if bs else None
    statewide = {}
    for mk in ["mail_voted", "early_voted", "election_day", "provisional", "cast"]:
        b = sum_method(mk)
        if b:
            statewide[mk] = b
    statewide["registered"] = sum(e.get("registered", 0) for e in result.values())
    statewide["turnout_total"] = sum(e.get("turnout_total", 0) for e in result.values())
    statewide["turnout_pct"] = round(100.0 * statewide["turnout_total"] / statewide["registered"], 2) \
        if statewide["registered"] else None
    add_eday_mix(statewide)

    out = {
        "election": {"name": "2022 General", "number": "26906", "date": "2022-11-08"},
        "source": "TQV archive (VR Systems) 2022 General #26906 -- party x method incl. election day; DOS turnout report for registration/turnout",
        "eday_by_party": True,
        "fallback_counties": fallback_codes,
        "statewide": statewide, "counties": result,
    }
    json.dump(out, open(OUT_PATH, "w", encoding="utf-8"), separators=(",", ":"))
    tqv_n = sum(1 for e in result.values() if e.get("src") == "tqv")
    print("Counties: %d (%d from TQV, %d PDF-fallback). Wrote %s (%d bytes)."
          % (len(result), tqv_n, len(fallback_codes), os.path.relpath(OUT_PATH, ROOT), os.path.getsize(OUT_PATH)))
    if fallback_codes:
        print("  PDF-fallback (mail+early only, no e-day):", ", ".join(fallback_codes))
    s = statewide
    def mstr(mk):
        b = s.get(mk) or {}
        t = b.get("total", 0)
        return "%s (R%s/D%s, %s)" % (t, b.get("rep"), b.get("dem"),
                                     ("R+%.1f" % (100*(b['rep']-b['dem'])/t) if t and b['rep']>=b['dem'] else "D+%.1f" % (100*(b['dem']-b['rep'])/t) if t else "-"))
    print("Statewide 2022  mail=%s  early=%s  eday=%s" % (mstr("mail_voted"), mstr("early_voted"), mstr("election_day")))
    print("  cast(all)=%s  turnout=%s%%  mix=%s" % ((s.get("cast") or {}).get("total"), s.get("turnout_pct"), s.get("method_mix")))


if __name__ == "__main__":
    main()
