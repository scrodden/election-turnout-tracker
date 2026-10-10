#!/usr/bin/env python3
"""California vote-by-mail ballots by county, with a statewide party split.

Source: California Secretary of State "VBM Ballot Statistics" workbook
(bsr-statistics.xlsx, linked from the 2026 general election page; first cell
= data date as an Excel serial). Per county: Total Voters Issued VBM Ballots,
returned ballots by method, Total Returned VBM Ballots, Total Accepted VBM
Ballots; plus a Total row (checked). California mails every active voter a
ballot; per the SoS, "issued" also counts voters who have since moved or been
inactivated, so "outstanding" (issued - returned) runs slightly high.

mail_voted = Total Returned (accepted is a subset, behind by signature checks);
mail_provided = issued - returned -> ballot chase. Registered voters for
turnout %: the SoS Report of Registration by county (15-day report when
posted, else the 60-day). Fallback: the UF Election Lab's statewide row.

Party: the SoS report has none. The UF Election Lab's statewide California
row carries ballots requested and returned by party (from Political Data Inc,
per the Lab), used unaltered with attribution (CC BY-NC-ND 4.0) for the
STATEWIDE blocks only: returned by party = the Lab's accept_dem / accept_rep /
accept_none; any SoS-counted ballots beyond those have no party ('unk'), so
shares and lean are over party-known ballots. Outstanding by party = the
Lab's requested minus returned, the rest unk. The Lab's county file has no
party, so counties stay turnout-only (data units_partisan: false).

Run:  python scripts/ca_update.py [--force]
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "ca"
CONFIG_PATH = os.path.join(ROOT, "config", "ca.json")
GEO_PATH = os.path.join(ROOT, "assets", "ca-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _norm(s):
    return re.sub(r"[^a-z]", "", str(s).lower())


def _block(total):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(total),
            "rep_pct": None, "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def read_vbm(raw):
    """-> (as-of datetime, {county: (issued, returned, accepted)}) with the Total row checked."""
    rows = next(iter(C.read_xlsx(raw, positional=True).values()))
    stamp = str(rows[0][0]).strip()
    try:   # an Excel serial, or (since 10/10/26) text like 'Saturday, 10/10/2026 (a.m)'
        as_of = datetime(1899, 12, 30) + timedelta(days=float(stamp))
    except ValueError:
        m = re.search(r"(\d{1,2})/(\d{1,2})/(20\d{2})(?:\D*\(?\s*([ap])\.?\s*m)?", stamp, re.I)
        if not m:
            raise RuntimeError("CA: unrecognised data date %r" % stamp[:40])
        hour = {"a": 9, "p": 17}.get((m.group(4) or "").lower(), 0)   # the SoS marks only a.m. / p.m.
        as_of = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)), hour)
    hi = next(i for i, r in enumerate(rows) if r and r[0].strip().upper() == "COUNTY")
    head = [c.strip() for c in rows[hi]]

    def col(name):
        return next(i for i, h in enumerate(head) if h.lower().startswith(name.lower()))
    ci, cr, ca = col("Total Voters Issued"), col("Total Returned"), col("Total Accepted")
    out, total = {}, None
    for r in rows[hi + 1:]:
        if not r or not r[0].strip():
            continue
        if len(r) <= ci or not str(r[ci]).strip().replace(".", "", 1).isdigit():
            continue   # notes / blank rows
        vals = tuple(C.parse_number(r[i]) if i < len(r) else 0 for i in (ci, cr, ca))
        if r[0].strip().lower() == "total":
            total = vals
            break   # footnotes follow
        out[r[0].strip()] = vals
    if not total or tuple(sum(v[i] for v in out.values()) for i in range(3)) != total:
        raise RuntimeError("CA: county rows don't add up to the Total row")
    return as_of, out


def read_registration(src):
    """-> ({normalized county: total registered}, report name) from the newest report posted."""
    for url in src["registration"]:
        try:
            raw = C.http_get(url, binary=True, retries=1)
        except Exception:  # noqa: BLE001 - later reports appear once published
            continue
        rows = next(iter(C.read_xlsx(raw, positional=True).values()))
        head = [c.strip() for c in rows[0]]
        it = head.index("Total Registered")
        reg = {_norm(r[0]): C.parse_number(r[it]) for r in rows[1:]
               if r and r[0].strip() and r[0].strip() not in ("Percent", "State Total") and len(r) > it}
        return reg, ("15-day" if "15day" in url else "60-day")
    return {}, ""


def entity(issued, returned, reg):
    e = {"mail_voted": _block(returned), "mail_provided": _block(max(0, issued - returned)), "cast": _block(returned),
         "registered": reg, "turnout_pct": C.pct(returned, reg) if reg else None}
    m = C.compute_mail(e)
    if m:
        e["mail"] = m
    return e


def lab_party(statewide):
    """Statewide party split from the UF Election Lab's California row (PDI).
    -> note text, or None if the Lab has no party figures."""
    import csv
    import io
    try:
        row = next((r for r in csv.DictReader(io.StringIO(C.http_get(
            "https://election.lab.ufl.edu/data-downloads/earlyvote/2026/US.csv", no_cache=True, retries=2)))
            if (r.get("state_abbv") or "").upper() == "CA"), None)
    except Exception as e:  # noqa: BLE001
        print("CA: Election Lab party figures unavailable: %s" % str(e)[:100], file=sys.stderr)
        return None

    def n(k):
        v = str((row or {}).get(k, "") or "").replace(",", "").strip()
        return int(float(v)) if v and v.upper() != "NA" else 0
    acc = {"rep": n("accept_rep"), "dem": n("accept_dem"), "npa": n("accept_none")}
    req = {"rep": n("request_rep"), "dem": n("request_dem"), "npa": n("request_none")}
    known = sum(acc.values())
    if not known:
        return None
    ret = statewide["mail_voted"]["total"]
    statewide["mail_voted"] = C.party_block(acc["rep"], acc["dem"], 0, acc["npa"], unk=max(0, ret - known))
    out = {p: max(0, req[p] - acc[p]) for p in acc}
    prov = statewide["mail_provided"]["total"]
    statewide["mail_provided"] = C.party_block(out["rep"], out["dem"], 0, out["npa"],
                                               unk=max(0, prov - sum(out.values())))
    statewide["cast"] = statewide["mail_voted"]
    m = C.compute_mail(statewide)
    if m:
        statewide["mail"] = m
    unk = statewide["mail_voted"].get("unk", 0)
    return ("Party (statewide only): UF Election Lab, from Political Data Inc (as of %s) -- %s of the returned ballots "
            "have a party%s. Counties are shown without party." % (
                row.get("last_update", "?"), format(known, ","),
                (" (%s counted by the SoS but not yet in the party data)" % format(unk, ",")) if unk else
                (" (more than the SoS's %s; the party data is ahead)" % format(ret, ",") if known > ret else "")))


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg.get("source", {})
    prev = load(LATEST_PATH, {}) or {}
    os.makedirs(DATA_DIR, exist_ok=True)
    if not force and prev.get("counties") and C.checked_recently(DATA_DIR, minutes=20):
        print("ca: checked under 20 minutes ago.")
        return 0
    try:
        as_of, rows = read_vbm(C.http_get(src["vbm_xlsx"], binary=True, no_cache=True, retries=2))
    except Exception as e:  # noqa: BLE001
        print("CA SoS VBM statistics unavailable: %s" % str(e)[:140], file=sys.stderr)
        import lab_standin as LAB
        LAB.run(STATE, cfg, GEO_PATH, LATEST_PATH, HISTORY_PATH, partisan=False, force=force)
        return 0
    reg, reg_report = read_registration(src)
    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}
    unmatched = [n for n in rows if _norm(n) not in gidx]
    if unmatched:
        print("CA: unmatched counties %s — keeping previous snapshot." % unmatched[:5], file=sys.stderr)
        return 0
    counties = {}
    for name, (iss, ret, acc) in rows.items():
        g = gidx[_norm(name)]
        counties[g["name"]] = dict(entity(iss, ret, reg.get(_norm(name), 0)), fips=g["fips"], accepted=acc)
    t_iss, t_ret, t_acc = (sum(v[i] for v in rows.values()) for i in range(3))
    statewide = dict(entity(t_iss, t_ret, sum(reg.values())), accepted=t_acc)
    party_note = lab_party(statewide)
    stamp = as_of.strftime("%Y-%m-%d %H:%M")
    body = {
        "state": STATE, "state_name": cfg.get("state_name", "California"), "election": cfg.get("election", {}),
        "partisan": bool(party_note), "units_partisan": False,
        "unk_label": "counted by the SoS, not yet in the party data",
        "methods_present": [k for k in ("mail_voted", "mail_provided") if statewide[k]["total"]],
        "method_labels": cfg.get("method_labels", {}), "mail_base_label": cfg.get("mail_base_label", "Ballots issued"),
        "statewide": statewide, "counties": counties,
        "map_note": ("Returned ballots (%s of them accepted so far after signature checks). 'Issued' counts every voter "
                     "sent a ballot, including some who have since moved or been inactivated, so 'outstanding' runs "
                     "slightly high." % format(t_acc, ",")) + ((" " + party_note) if party_note else ""),
        "source": {"primary": "California Secretary of State VBM Ballot Statistics (data through %s PT); registered "
                              "voters: %s Report of Registration%s" % (
                                  stamp, reg_report, "; statewide party split: UF Election Lab (Political Data Inc), "
                                                     "CC BY-NC-ND 4.0" if party_note else ""),
                   "url": src.get("page"), "as_of": stamp},
        "source_compiled": stamp,
    }
    h = C.data_hash(body)
    changed = force or h != prev.get("data_hash")
    now = C.utc_now_iso()
    snap = dict(body, data_hash=h, source_compiled_iso=now, generated_at=now if changed else prev.get("generated_at", now))
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    if changed and t_ret:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            c = statewide["cast"]
            f.write(json.dumps({"generated_at": now, "cast": t_ret, "registered": statewide["registered"],
                                "statewide": {"cast": [c["rep"], c["dem"], c["oth"], c["npa"], c["total"]]},
                                "data_hash": h}, separators=(",", ":")) + "\n")
    print("%s  CA SoS VBM statistics through %s: %d counties | returned=%d (accepted %d) of %d issued | turnout %s%%"
          % ("CHANGED" if changed else "NOCHANGE", stamp, len(counties), t_ret, t_acc, t_iss, statewide["turnout_pct"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
