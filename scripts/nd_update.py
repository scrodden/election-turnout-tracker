#!/usr/bin/env python3
"""North Dakota absentee/VBM turnout by county (turnout-only; ND has no party
registration). Source: ND SoS 'Absentee/Vote-by-Mail Ballot & Early Voting
Numbers' (vip.sos.nd.gov/abev.aspx). The page shows statewide totals; a county
dropdown (ASP.NET __doPostBack) reveals each county's 'N Ballots Sent, M Ballots
Returned' (absentee + VBM combined). We GET the page for the ViewState + county
options, then POST once per county (reusing the initial ViewState). cast =
returned; the mail chase comes from sent vs returned.

~54 requests per full run, so self-throttled (~3h) via source.refresh_hours; the
numbers move slowly. --force ignores the throttle; --limit N does a subset.

Run:  python scripts/nd_update.py [--force] [--limit N]
"""
import os
import re
import sys
import json
import time
import urllib.request
import urllib.parse
import http.cookiejar

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "nd"
CONFIG_PATH = os.path.join(ROOT, "config", "nd.json")
GEO_PATH = os.path.join(ROOT, "assets", "nd-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
DDL = "ctl00$ContentPlaceHolder1$ddlCounty"


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _int(s):
    s = str(s or "").replace(",", "").strip()
    return int(s) if s.isdigit() else 0


def age_hours(iso):
    from datetime import datetime, timezone
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return 1e9


def _block(total):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(total),
            "rep_pct": None, "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def main():
    force = "--force" in sys.argv
    limit = None
    for a in sys.argv:
        if a.startswith("--limit"):
            limit = int(a.split("=", 1)[1]) if "=" in a else int(sys.argv[sys.argv.index(a) + 1])
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg.get("source", {})
    url = src["url"]
    refresh_h = float(src.get("refresh_hours", 3))

    prev = load(LATEST_PATH, {}) or {}
    if prev.get("counties") and not force and age_hours(prev.get("generated_at", "")) < refresh_h:
        print("nd latest is fresh (<%.0fh) — skipping (self-throttled)." % refresh_h)
        return 0

    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}

    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj),
                                     urllib.request.HTTPSHandler(context=C._SSL_CTX))
    op.addheaders = [("User-Agent", C.USER_AGENT)]

    try:
        h = op.open(url, timeout=60).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        print("ND GET failed: %s" % str(e)[:100], file=sys.stderr)
        return 0

    def hid(name):
        m = re.search(r'id="%s" value="([^"]*)"' % name, h)
        return m.group(1) if m else ""
    vs, vsg, ev = hid("__VIEWSTATE"), hid("__VIEWSTATEGENERATOR"), hid("__EVENTVALIDATION")
    options = re.findall(r'<option[^>]*value="(\d{2})"[^>]*>([^<]+)</option>', h)

    # statewide early voting (from the initial page)
    def sw_label(label):
        m = re.search(re.escape(label) + r"\s*</td>\s*<td[^>]*>\s*<span[^>]*>([\d,]+)", h, re.S)
        return _int(m.group(1)) if m else 0
    early_voting = sw_label("Early Voting Turnout")

    if limit:
        options = options[:limit]

    counties = {}
    unmatched = []
    cast_total = sent_total = 0
    for code, name in options:
        g = gidx.get(_norm(name))
        form = {"__EVENTTARGET": DDL, "__EVENTARGUMENT": "", "__VIEWSTATE": vs,
                "__VIEWSTATEGENERATOR": vsg, "__EVENTVALIDATION": ev, DDL: code}
        try:
            req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST")
            req.add_header("User-Agent", C.USER_AGENT)
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            r = op.open(req, timeout=60).read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r))
        m = re.search(r"([\d,]+)\s*Ballots Sent\s*([\d,]+)\s*Ballots Returned", txt)
        if not m:
            continue
        sent, returned = _int(m.group(1)), _int(m.group(2))
        if not g:
            unmatched.append(name)
            continue
        ent = {"fips": g["fips"], "cast": _block(returned), "mail_voted": _block(returned),
               "mail_provided": _block(max(0, sent - returned)), "turnout_pct": None, "registered": 0}
        mm = C.compute_mail(ent)
        if mm:
            ent["mail"] = mm
        counties[g["name"]] = ent
        cast_total += returned
        sent_total += sent
        time.sleep(0.3)

    statewide = {"cast": _block(cast_total + early_voting), "mail_voted": _block(cast_total),
                 "mail_provided": _block(max(0, sent_total - cast_total)),
                 "early_voted": _block(early_voting), "registered": 0, "turnout_pct": None}
    sw_mail = C.compute_mail(statewide)
    if sw_mail:
        statewide["mail"] = sw_mail
    methods_present = [m for m, t in (("mail_voted", cast_total), ("early_voted", early_voting)) if t]
    snap = {
        "state": STATE, "state_name": cfg.get("state_name", "North Dakota"),
        "election": cfg.get("election", {}), "partisan": False,
        "source": {"primary": "ND SoS Absentee/VBM & Early Voting Numbers (per-county); turnout-only"},
        "source_compiled": C.utc_now_iso()[:10], "source_compiled_iso": C.utc_now_iso(),
        "methods_present": methods_present, "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties,
    }
    snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k not in ("source_compiled", "source_compiled_iso")})
    snap["generated_at"] = C.utc_now_iso()

    changed = force or snap["data_hash"] != prev.get("data_hash")
    os.makedirs(DATA_DIR, exist_ok=True)
    if changed and (counties or not prev.get("counties")):
        with open(LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        if cast_total:
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"generated_at": snap["generated_at"], "cast": cast_total + early_voting,
                                    "mail": cast_total, "sent": sent_total}, separators=(",", ":")) + "\n")
        print("CHANGED  counties=%d  cast=%d  sent=%d  early=%d" % (len(counties), cast_total, sent_total, early_voting))
    else:
        print("NOCHANGE  (cast=%d, %d counties)" % (cast_total, len(counties)))
    if unmatched:
        print("  unmatched:", unmatched[:10])
    return 0


if __name__ == "__main__":
    sys.exit(main())
