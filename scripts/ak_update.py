#!/usr/bin/env python3
"""Alaska early/absentee ballots: statewide by ballot type, and by house district.

Source: the Alaska Division of Elections' statistics page
(elections.alaska.gov/statistics/, "Absentee, Early, and Questioned Ballot
Statistics"): for each election, ballots issued and received by type (By Mail,
By Fax, Online Delivery, Federal Write-In, Early Vote, In-Person absentee,
Special Needs, Questioned) with totals and a "Table last updated" stamp.
  received by mail/fax/online/FWAB -> mail_voted; issued minus received for those -> mail_provided
  received early vote/in-person absentee/special needs -> early_voted; questioned -> provisional

The Division's own per-house-district "Combined Ballot Report" (htm; by ballot
type, no party) answers scripts with a CloudFront "Human Verification" page
(HTTP 405, a 2,299-byte challenge page; measured 2026-10-21 and again 2026-10-28
from Sam's PC on the 10/28 10:39 AM report, the August one and the 2024 general's
-- every .html report file; the statistics page, the Division's PDFs and its
registration pages load normally). Not bypassed. The map uses the UF Election
Lab's copy of the same report instead (AK_sdl.csv: requests, mail ballots
accepted, in-person, total per house district, plus "99" for ballots outside the
40 districts), which runs about a day behind the statistics page, so the
statewide cards stay on the Division's own totals and the map note says the
district figures trail them. Registered voters per district (turnout %): the
Division's monthly "Voters by Party and Precinct" report, its "TOTAL DISTRICT"
rows. Map: assets/ak-sh.geojson (scripts/build_sld_geo.py ak 02 lower
"District " sh, with the Aleutians' east longitudes shifted by -360).

The site's certificate (since 2026-10-07) is served without its intermediate;
common.py loads that intermediate from assets/certs so it verifies.

Registered voters: data/registration.json (the Division's monthly voters-by-party
report, read by registration_update.py) -> turnout %.

Run:  python scripts/ak_update.py [--force]
"""
import html as H
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "ak"
CONFIG_PATH = os.path.join(ROOT, "config", STATE + ".json")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
REG_PATH = os.path.join(ROOT, "data", "registration.json")
STATS_PAGE = "https://www.elections.alaska.gov/statistics/"
GEO_PATH = os.path.join(ROOT, "assets", "ak-sh.geojson")
DISTRICTS_PATH = os.path.join(DATA_DIR, "_districts.json")   # the Lab's district figures + district registration
LAB_BASE = "https://election.lab.ufl.edu/data-downloads/earlyvote/2026/"
REG_REPORT = "https://www.elections.alaska.gov/statistics/2026/%s/VOTERS%%20BY%%20PARTY%%20AND%%20PRECINCT.htm"
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
PARTY_COLS = ("D", "L", "R", "C", "G", "I", "J", "M", "Q", "S", "V", "W", "Y", "Z", "N", "U")
MAIL = ("by mail", "by fax", "online delivery", "federal write-in (absentee)")
IN_PERSON = ("early vote", "in-person (absentee)", "special needs")
QUESTIONED = ("questioned",)


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def blk(t):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(t), "rep_pct": None,
            "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def _n(s):
    s = re.sub(r"<[^>]+>", "", s or "").strip()
    return int(s.replace(",", "")) if s else 0


def parse(page, title):
    """-> {"issued", "received", "updated", "rows": {type: (issued, received)}} for the election whose heading
    starts with `title` (e.g. "2026 General Election"), checked against the block's own totals."""
    m = re.search(r"<h3[^>]*>\s*" + re.escape(title) + r"\s*(?:&#x2013;|&#8211;|–|-)", page)
    if not m:
        return None
    seg = page[m.end():]
    nxt = seg.find("<h3")
    seg = seg[:nxt] if nxt > 0 else seg
    iss = re.search(r"Total Ballots Issued:\s*<em>([\d,]*)</em>", seg)
    rec = re.search(r"Total Ballots Received:\s*<em>([\d,]*)</em>", seg)
    upd = re.search(r"Table last updated\s+([A-Z][a-z]+ \d{1,2}, \d{4}) at (\d{1,2}:\d{2}\s*[ap]m)", seg)
    rows = {}
    for t, a, b in re.findall(r"<tr>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>", seg, re.S):
        rows[H.unescape(re.sub(r"<[^>]+>", "", t)).strip().lower()] = (_n(a), _n(b))
    if not (iss and rec and rows):
        raise ValueError("no totals/rows in the %s block" % title)
    out = {"issued": _n(iss.group(1)), "received": _n(rec.group(1)), "rows": rows,
           "updated": "%s %s" % (upd.group(1), upd.group(2)) if upd else ""}
    if sum(v[0] for v in rows.values()) != out["issued"] or sum(v[1] for v in rows.values()) != out["received"]:
        raise ValueError("rows don't add up to the totals (%d/%d)" % (out["issued"], out["received"]))
    unknown = [k for k in rows if k not in MAIL + IN_PERSON + QUESTIONED and any(rows[k])]
    if unknown:
        raise ValueError("unexpected ballot types %s" % unknown)
    return out


def parse_registration(page):
    """Voters by Party and Precinct report -> (as_of "10/03/2026", {district: {"total", "dem", "rep", "npa", "oth"}}),
    from each district's "TOTAL DISTRICT(n PRECINCTS)" row (party columns D L R ... N U, which add up to the total)."""
    def cells(r):
        return [H.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c))).strip()
                for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", r)]
    out, cur = {}, None
    for r in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", page):
        c = cells(r)
        m = re.fullmatch(r"DISTRICT 0*(\d+)", c[0]) if c else None
        if m:
            cur = int(m.group(1))
        elif c and c[0].startswith("TOTAL DISTRICT") and cur is not None:
            n = [int(x.replace(",", "")) for x in c[1:1 + 1 + len(PARTY_COLS)]]
            w = dict(zip(PARTY_COLS, n[1:]))
            if len(n) != 1 + len(PARTY_COLS) or sum(w.values()) != n[0]:
                raise ValueError("district %d: parties don't add up to its total" % cur)
            npa = w["N"] + w["U"]
            out[cur] = {"total": n[0], "dem": w["D"], "rep": w["R"], "npa": npa, "oth": n[0] - w["D"] - w["R"] - npa}
            cur = None
    d = re.search(r"DATE:\s*(\d{1,2}/\d{1,2}/\d{4})", page)
    return (d.group(1) if d else ""), out


def lab_districts(numbers):
    """The Election Lab's AK_sdl.csv (its copy of the Division's report by house district) -> ({district: {"req",
    "acc", "inp"}}, ballots outside the 40 districts {"req", "acc", "inp"}, the Lab's as-of date), checked against
    the Lab's statewide row; raises ValueError if they don't add up."""
    import csv
    import io
    row = next((r for r in csv.DictReader(io.StringIO(C.http_get(LAB_BASE + "US.csv", no_cache=True)))
                if (r.get("state_abbv") or "").upper() == "AK"), None)
    parts = list(csv.DictReader(io.StringIO(C.http_get(LAB_BASE + "AK_sdl.csv", no_cache=True, retries=1))))
    if not row or not parts:
        raise ValueError("no Alaska rows")
    keys = (("req", "request_all"), ("acc", "accept_all"), ("inp", "inperson_all"))
    out, rest = {}, {k: 0 for k, _ in keys}
    for r in parts:
        v = str(r.get("sdl") or "").strip()
        acc = out.setdefault(int(v), {k: 0 for k, _ in keys}) if v.isdigit() and int(v) in numbers else rest
        for k, col in keys:
            acc[k] += C.parse_number(r.get(col) or 0) or 0
    for k, col in keys:
        if sum(d[k] for d in out.values()) + rest[k] != (C.parse_number(row.get(col) or 0) or 0):
            raise ValueError("the district file's %s doesn't add up to the Lab's statewide row" % col)
    if len(out) != len(numbers):
        raise ValueError("%d of the %d districts in the file" % (len(out), len(numbers)))
    return out, rest, row.get("last_update", "")


def districts(force=False):
    """-> the saved district figures, refreshed about hourly (Lab) / daily (registration); the previous ones are kept
    when a source is unavailable or doesn't add up."""
    geo = load(GEO_PATH, {"features": []})
    numbers = {f["properties"]["district_number"] for f in geo["features"]}
    saved = load(DISTRICTS_PATH, {}) or {}
    if force or not saved.get("reg") or not C.checked_recently(DATA_DIR, "reg_hd", minutes=24 * 60):
        now = datetime.now(timezone.utc)
        for mi in range(now.month, 0, -1):
            try:
                as_of, reg = parse_registration(C.http_get(REG_REPORT % MONTHS[mi - 1], no_cache=True, retries=1, timeout=60))
            except Exception as e:  # noqa: BLE001
                print("ak: %s registration report unavailable (%s)" % (MONTHS[mi - 1], str(e)[:100]), file=sys.stderr)
                continue
            if set(reg) == numbers:
                saved.update(reg={str(k): v for k, v in reg.items()}, reg_as_of=as_of)
                break
    if force or not saved.get("lab") or not C.checked_recently(DATA_DIR, "lab_sdl", minutes=55):
        try:
            lab, rest, as_of = lab_districts(numbers)
            saved.update(lab={str(k): v for k, v in lab.items()}, lab_rest=rest, lab_as_of=as_of)
        except Exception as e:  # noqa: BLE001
            print("ak: Lab district file not used (%s)" % str(e)[:120], file=sys.stderr)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DISTRICTS_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(saved, f, separators=(",", ":"), sort_keys=True)
    return saved, geo


def district_units(saved, geo):
    """-> {name: unit} for the map (ballots from the Lab's file, registered voters from the Division's report)."""
    out = {}
    for f in geo["features"]:
        p = f["properties"]
        b = (saved.get("lab") or {}).get(str(p["district_number"]))
        if not b:
            return {}
        reg = int(((saved.get("reg") or {}).get(str(p["district_number"])) or {}).get("total") or 0)
        cast = b["acc"] + b["inp"]
        u = {"fips": p["fips"], "registered": reg, "cast": blk(cast), "mail_voted": blk(b["acc"]),
             "early_voted": blk(b["inp"]), "turnout_pct": C.pct(cast, reg) if reg else None}
        if b["req"] > b["acc"]:
            u["mail_provided"] = blk(b["req"] - b["acc"])
        u["mail"] = C.compute_mail(u)
        out[p["name"]] = u
    return out


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {})
    prev = load(LATEST_PATH, {}) or {}
    if not force and C.checked_recently(DATA_DIR, "source", minutes=30):
        print("ak: checked under 30 minutes ago.")
        return 0
    title = cfg["source"].get("stats_election", "2026 General Election")
    r = parse(C.http_get(STATS_PAGE, no_cache=True, timeout=60), title)
    if not r:
        print("ak: no '%s' block on the statistics page yet" % title)
        return 0

    def got(types, i):
        return sum(v[i] for k, v in r["rows"].items() if k in types)
    mail, ip, q = got(MAIL, 1), got(IN_PERSON, 1), got(QUESTIONED, 1)
    outstanding = got(MAIL, 0) - mail
    reg = ((load(REG_PATH, {}) or {}).get("states") or {}).get(STATE) or {}
    registered = int(reg.get("total") or 0)
    sw = {"cast": blk(r["received"]), "registered": registered,
          "turnout_pct": round(100.0 * r["received"] / registered, 2) if registered else None}
    if mail:
        sw["mail_voted"] = blk(mail)
    if outstanding > 0:
        sw["mail_provided"] = blk(outstanding)
    if ip:
        sw["early_voted"] = blk(ip)
    if q:
        sw["provisional"] = blk(q)
    if mail or outstanding > 0:
        sw["mail"] = C.compute_mail(sw)   # the page's "ballot chase" panel: sent (issued) vs returned
    iso = ""
    if r["updated"]:
        t = datetime.strptime(r["updated"].upper(), "%B %d, %Y %I:%M %p")   # "October 20, 2026 2:16 pm", Alaska time
        iso = (t + timedelta(hours=8)).replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")   # AKDT = UTC-8
    saved, geo = districts(force)
    units = district_units(saved, geo)
    if units:
        cast_d = sum(u["cast"]["total"] for u in units.values())
        rest = saved.get("lab_rest") or {}
        rest_cast = rest.get("acc", 0) + rest.get("inp", 0)
        note = ("House district figures are the Division's Combined Ballot Report by district as republished by the "
                "UF Election Lab (as of %s): %s ballots in the 40 districts%s. They trail the statewide totals above "
                "(the Division's own, as of %s) until the next copy. Registered voters per district: the Division's "
                "voters-by-party report of %s. The Lab's mail / in-person split is its own grouping of the report's "
                "ballot types, so compare districts on total ballots."
                % (saved.get("lab_as_of") or "?", format(cast_d, ","),
                   ", plus %s listed under district 99, outside them" % format(rest_cast, ",") if rest_cast else "",
                   r["updated"] or "?", saved.get("reg_as_of") or "?"))
    else:
        note = ("The Division of Elections publishes these totals statewide; the house district figures aren't "
                "available right now, so there's no district map.")
    body = {
        "state": STATE, "state_name": cfg.get("state_name", "Alaska"), "election": cfg.get("election", {}),
        "partisan": False, "statewide_only": not units, "unit_label": "House District",
        "unit_label_plural": "House Districts",
        "methods_present": [k for k in ("mail_voted", "early_voted", "provisional", "mail_provided") if k in sw],
        "method_labels": cfg.get("method_labels", {}), "mail_base_label": "Ballots sent",
        "statewide": sw, "counties": units, "map_note": note,
        "no_party_text": ("The Division of Elections doesn't report these ballots by party, so this is turnout only; "
                          "the L2 card below has a modeled party profile for each district."),
    }
    import lab_standin as LAB
    bad = LAB.suspicious_drop(prev, r["received"])
    if bad:
        print("ak: %s" % bad, file=sys.stderr)
        return 0
    h = C.data_hash(body)
    changed = force or h != prev.get("data_hash")
    now = C.utc_now_iso()
    snap = dict(body, source={"primary": "Alaska Division of Elections, Absentee, Early, and Questioned Ballot Statistics "
                                         "(statewide, by ballot type)%s" % (
                                             "; house districts: the Division's Combined Ballot Report via the UF "
                                             "Election Lab (AK_sdl.csv, as of %s; CC BY-NC-ND 4.0)" % saved.get("lab_as_of")
                                             if units else ""), "url": STATS_PAGE, "as_of": r["updated"],
                              "issued": r["issued"], "received": r["received"],
                              **({"registration_as_of": reg.get("as_of")} if registered else {})},
                source_compiled=r["updated"], source_compiled_iso=iso or now,
                data_hash=h, generated_at=now if changed else prev.get("generated_at", now))
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LATEST_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(snap, f, separators=(",", ":"))
    if changed:
        with open(HISTORY_PATH, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"generated_at": now, "compiled": r["updated"], "cast": r["received"]},
                               separators=(",", ":")) + "\n")
    print("%s  AK as of %s | received %d (mail %d, in person %d, questioned %d) of %d issued | turnout %s%%"
          % ("CHANGED" if changed else "NOCHANGE", r["updated"], r["received"], mail, ip, q, r["issued"], sw["turnout_pct"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
