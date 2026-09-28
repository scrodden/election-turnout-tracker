#!/usr/bin/env python3
"""Illinois mail and early ballots by county (turnout-only; IL has no party
registration).

Source: Illinois State Board of Elections "Pre-Election Ballot Counts"
(VotingAndRegistrationSystems/PreElectionCounts.aspx). It's an ASP.NET page:
choosing the election posts back and returns links to the current report in
several formats; we take the 'ASCII Comma Delimited' CSV:
  JID, Name, ElectionDate, By-Mail, By-Mail Returned, Early, Grace
one row per election jurisdiction (102 counties + 6 city boards) plus a
'STATEWIDE COUNTS' row (used as a check). City boards are folded into their
counties for the map: Chicago -> Cook, Bloomington -> McLean, Danville ->
Vermilion, East St. Louis -> St. Clair, Galesburg -> Knox, Rockford -> Winnebago.

mail_voted = By-Mail Returned; early_voted = Early + Grace (grace-period
in-person voting); mail_provided = By-Mail - Returned (outstanding) -> ballot
chase. If the page can't be read, falls back to the UF Election Lab stand-in.

Run:  python scripts/il_update.py [--force]
"""
import csv
import html as H
import http.cookiejar
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "il"
CONFIG_PATH = os.path.join(ROOT, "config", "il.json")
GEO_PATH = os.path.join(ROOT, "assets", "il-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
PAGE = "https://www.elections.il.gov/VotingAndRegistrationSystems/PreElectionCounts.aspx"
CITY_TO_COUNTY = {"chicago": "cook", "bloomington": "mclean", "danville": "vermilion",
                  "eaststlouis": "stclair", "galesburg": "knox", "rockford": "winnebago"}


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _norm(s):
    s = str(s).lower().replace("saint ", "st ")
    s = re.sub(r"\s+county$", "", s.strip())
    return re.sub(r"[^a-z0-9]", "", s)


def _int(v):
    v = str(v or "").replace(",", "").strip()
    try:
        return int(float(v)) if v else 0
    except ValueError:
        return 0


def _block(total):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(total),
            "rep_pct": None, "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def fetch_csv(election_name):
    """-> (csv text, 'last updated' text)."""
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj),
                                     urllib.request.HTTPSHandler(context=C._SSL_CTX))
    op.addheaders = [("User-Agent", C.USER_AGENT)]
    h = op.open(PAGE, timeout=60).read().decode("utf-8", "replace")
    opt = re.search(r'<option[^>]*value="(\d+)"[^>]*>\s*%s\s*</option>' % re.escape(election_name), h)
    if not opt:
        raise RuntimeError("IL: election %r not offered yet" % election_name)
    fields = {}
    for m in re.finditer(r'<input[^>]+type="hidden"[^>]*>', h):
        n, v = re.search(r'name="([^"]+)"', m.group(0)), re.search(r'value="([^"]*)"', m.group(0))
        if n:
            fields[n.group(1)] = H.unescape(v.group(1)) if v else ""   # values are HTML-escaped
    fields.update({"__EVENTTARGET": "ctl00$ContentPlaceHolder1$ddlElection", "__EVENTARGUMENT": "",
                   "ctl00$ContentPlaceHolder1$ddlElection": opt.group(1)})
    r = op.open(urllib.request.Request(PAGE, data=urllib.parse.urlencode(fields).encode(), method="POST"),
                timeout=90).read().decode("utf-8", "replace")
    link = re.search(r'<a[^>]+href="([^"]*NewDocDisplay\.aspx\?[^"]+)"[^>]*>\s*ASCII Comma Delimited', r)
    if not link:
        raise RuntimeError("IL: CSV link not found after selecting the election")
    stamp = re.search(r"last updated:\s*([^<]+)", r, re.I)
    body = op.open(urllib.request.Request(urllib.parse.urljoin(PAGE, H.unescape(link.group(1))),
                                          headers={"Referer": PAGE}), timeout=90).read()
    return body.decode("utf-8-sig", "replace"), (stamp.group(1).strip() if stamp else "")


def parse(text, gidx):
    rows = list(csv.DictReader(io.StringIO(text)))
    state = next((r for r in rows if (r.get("Name") or "").strip().upper() == "STATEWIDE COUNTS"), None)
    if not state:
        raise RuntimeError("IL: STATEWIDE COUNTS row missing")
    keys = ("By-Mail", "By-Mail Returned", "Early", "Grace")
    out, unmatched = {}, []
    for r in rows:
        name = (r.get("Name") or "").strip()
        if not name or r is state:
            continue
        k = _norm(re.sub(r"^City of\s+", "", name)) if name.lower().startswith("city of") else _norm(name)
        k = CITY_TO_COUNTY.get(k, k) if name.lower().startswith("city of") else k
        g = gidx.get(k)
        if not g:
            unmatched.append(name)
            continue
        slot = out.setdefault(g["name"], {"fips": g["fips"], **{x: 0 for x in keys}})
        for x in keys:
            slot[x] += _int(r.get(x))
    for x in keys:
        if sum(v[x] for v in out.values()) != _int(state.get(x)):
            raise RuntimeError("IL: jurisdictions don't add up to STATEWIDE COUNTS for %s" % x)
    return out, unmatched


def entity(v):
    ret, inp, req = v["By-Mail Returned"], v["Early"] + v["Grace"], v["By-Mail"]
    e = {"mail_voted": _block(ret), "early_voted": _block(inp), "mail_provided": _block(max(0, req - ret)),
         "cast": _block(ret + inp), "registered": 0, "turnout_pct": None}
    m = C.compute_mail(e)
    if m:
        e["mail"] = m
    return e


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg.get("source", {})
    if not force and (load(LATEST_PATH, {}) or {}).get("counties") and C.checked_recently(DATA_DIR):
        print("il: checked under an hour ago.")   # ISBE updates a few times a day
        return 0
    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}
    try:
        text, stamp = fetch_csv(src.get("election_name", "2026 General Election"))
        rows, unmatched = parse(text, gidx)
        if unmatched:
            raise RuntimeError("IL: unmatched jurisdictions %s" % unmatched[:5])
    except Exception as e:  # noqa: BLE001
        print("IL ISBE counts unavailable: %s" % str(e)[:160], file=sys.stderr)
        import lab_standin as LAB
        LAB.run(STATE, cfg, GEO_PATH, LATEST_PATH, HISTORY_PATH, partisan=False, force=force)
        return 0

    counties = {name: dict(entity(v), fips=v["fips"]) for name, v in rows.items()}
    tot = {x: sum(v[x] for v in rows.values()) for x in ("By-Mail", "By-Mail Returned", "Early", "Grace")}
    statewide = entity(tot)
    snap = {
        "state": STATE, "state_name": cfg.get("state_name", "Illinois"), "election": cfg.get("election", {}),
        "partisan": False,
        "source": {"primary": "Illinois State Board of Elections Pre-Election Ballot Counts (by election jurisdiction; "
                              "city boards folded into their counties), last updated %s" % stamp,
                   "url": PAGE, "as_of": stamp},
        "source_compiled": stamp, "source_compiled_iso": C.utc_now_iso(),
        "methods_present": [k for k in ("mail_voted", "early_voted", "mail_provided") if statewide[k]["total"]],
        "method_labels": cfg.get("method_labels", {}), "statewide": statewide, "counties": counties,
    }
    prev = load(LATEST_PATH, {}) or {}
    snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k != "source_compiled_iso"})
    snap["generated_at"] = C.utc_now_iso()
    if not force and snap["data_hash"] == prev.get("data_hash"):
        print("NOCHANGE  (ISBE counts, last updated %s)" % stamp)
        return 0
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    c = statewide["cast"]["total"]
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"generated_at": snap["generated_at"], "cast": c, "data_hash": snap["data_hash"]},
                           separators=(",", ":")) + "\n")
    print("CHANGED  ISBE (last updated %s): %d counties | cast=%d (mail %d, early %d) | mail requested=%d"
          % (stamp, len(counties), c, tot["By-Mail Returned"], tot["Early"] + tot["Grace"], tot["By-Mail"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
