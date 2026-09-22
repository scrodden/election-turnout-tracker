#!/usr/bin/env python3
"""Wisconsin absentee-ballot turnout by county (turnout-only; WI has no party
registration). Source: the WI Elections Commission daily 'Absentee Ballot
Report' county CSV. The dynamic report page is WAF-blocked to non-browsers, but
the static CSV under /sites/default/files/documents/ is fetchable. Columns:
Election, HINDI (county code), Jurisdiction, AbsenteeApplications, BallotsSent,
BallotsReturned, InPersonAbsentee.

cast = BallotsReturned + InPersonAbsentee. We also emit the mail 'ballot chase'
(BallotsSent vs BallotsReturned) as party-less mail_provided/mail_voted so the
front-end mail panel shows sent/returned/outstanding. The filename carries an
as-of date; we walk back from today to the newest posted file.

Run:  python scripts/wi_update.py [--force]
"""
import os
import re
import sys
import csv as csvmod
import json
import io
import urllib.parse
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "wi"
CONFIG_PATH = os.path.join(ROOT, "config", "wi.json")
GEO_PATH = os.path.join(ROOT, "assets", "wi-counties.geojson")
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
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _block(total):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(total),
            "rep_pct": None, "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def fetch_csv(cfg):
    """Walk back from today to the newest posted daily county CSV. Returns
    (text, as_of_date) or (None, None)."""
    pat = cfg["source"]["county_csv_pattern"]
    days = int(cfg["source"].get("date_walkback_days", 7))
    today = datetime.now(timezone.utc).date()
    for i in range(days + 1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        url = urllib.parse.quote(pat.replace("{date}", d), safe=":/?&=%")
        try:
            return C.http_get(url, timeout=60), d
        except Exception:  # noqa: BLE001
            continue
    return None, None


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}

    text, as_of = fetch_csv(cfg)
    counties = {}
    cast_total = mail_total = inperson_total = sent_total = 0
    unmatched = []
    if text:
        reader = csvmod.reader(io.StringIO(text))
        header = next(reader, [])
        col = {name.strip(): idx for idx, name in enumerate(header)}

        def num(row, name):
            i = col.get(name)
            if i is None or i >= len(row):
                return 0
            s = (row[i] or "").strip().replace(",", "")
            try:
                return int(float(s)) if s else 0
            except ValueError:
                return 0
        for row in reader:
            if not row or col.get("Jurisdiction") is None:
                continue
            juris = (row[col["Jurisdiction"]] or "").strip()
            key = _norm(re.sub(r"\s+county$", "", juris, flags=re.I))
            if not key or key == "total":
                continue
            g = gidx.get(key)
            if not g:
                unmatched.append(juris)
                continue
            sent = num(row, "BallotsSent")
            returned = num(row, "BallotsReturned")
            inperson = num(row, "InPersonAbsentee")
            cast = returned + inperson
            ent = {"fips": g["fips"], "cast": _block(cast),
                   "mail_voted": _block(returned), "early_voted": _block(inperson),
                   "mail_provided": _block(max(0, sent - returned)),
                   "turnout_pct": None, "registered": 0}
            m = C.compute_mail(ent)
            if m:
                ent["mail"] = m
            counties[g["name"]] = ent
            cast_total += cast
            mail_total += returned
            inperson_total += inperson
            sent_total += sent

    methods_present = [m for m, t in (("mail_voted", mail_total), ("early_voted", inperson_total)) if t]
    statewide = {"cast": _block(cast_total), "mail_voted": _block(mail_total),
                 "early_voted": _block(inperson_total),
                 "mail_provided": _block(max(0, sent_total - mail_total)),
                 "registered": 0, "turnout_pct": None}
    sw_mail = C.compute_mail(statewide)
    if sw_mail:
        statewide["mail"] = sw_mail
    snap = {
        "state": STATE, "state_name": cfg.get("state_name", "Wisconsin"),
        "election": cfg.get("election", {}), "partisan": False,
        "source": {"primary": "WI Elections Commission daily Absentee Ballot Report (county CSV); turnout-only"},
        "source_compiled": as_of or "", "source_compiled_iso": C.utc_now_iso(),
        "methods_present": methods_present, "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties,
    }
    snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k not in ("source_compiled", "source_compiled_iso")})
    snap["generated_at"] = C.utc_now_iso()

    prev = load(LATEST_PATH, {}) or {}
    changed = force or snap["data_hash"] != prev.get("data_hash")
    os.makedirs(DATA_DIR, exist_ok=True)
    if changed:
        with open(LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        if cast_total:
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"generated_at": snap["generated_at"], "compiled": as_of,
                                    "cast": cast_total, "mail": mail_total, "in_person": inperson_total},
                                   separators=(",", ":")) + "\n")
        print("CHANGED  as_of=%s  counties=%d  cast=%d (mail=%d, in_person=%d)  sent=%d"
              % (as_of, len(counties), cast_total, mail_total, inperson_total, sent_total))
    else:
        print("NOCHANGE  (cast=%d, %d counties)" % (cast_total, len(counties)))
    if unmatched:
        print("  unmatched:", unmatched[:10])
    return 0


if __name__ == "__main__":
    sys.exit(main())
