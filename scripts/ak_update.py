#!/usr/bin/env python3
"""Alaska early/absentee ballots, statewide (turnout only for now).

Source: the Alaska Division of Elections' statistics page
(elections.alaska.gov/statistics/, "Absentee, Early, and Questioned Ballot
Statistics"): for each election, ballots issued and received by type (By Mail,
By Fax, Online Delivery, Federal Write-In, Early Vote, In-Person absentee,
Special Needs, Questioned) with totals and a "Table last updated" stamp.
  received by mail/fax/online/FWAB -> mail_voted; issued minus received for those -> mail_provided
  received early vote/in-person absentee/special needs -> early_voted; questioned -> provisional

The per-house-district, per-party "Combined Report (htm)" linked from the same
table answers scripts with a CloudFront "Human Verification" page (HTTP 405,
2026-10-21; the statistics page itself, the Division's PDFs and its registration
pages load normally), so it isn't used: statewide totals only, no party split.

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
    body = {
        "state": STATE, "state_name": cfg.get("state_name", "Alaska"), "election": cfg.get("election", {}),
        "partisan": False, "statewide_only": True,
        "methods_present": [k for k in ("mail_voted", "early_voted", "provisional", "mail_provided") if k in sw],
        "method_labels": cfg.get("method_labels", {}), "mail_base_label": "Ballots sent",
        "statewide": sw, "counties": {},
        "map_note": ("The Division of Elections publishes these totals statewide; its report by house district and "
                     "party wasn't readable by our updater, so there's no map or party split for Alaska yet."),
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
                                         "(statewide, by ballot type)", "url": STATS_PAGE, "as_of": r["updated"],
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
