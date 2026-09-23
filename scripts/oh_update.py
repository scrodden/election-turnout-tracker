#!/usr/bin/env python3
"""Ohio absentee (mail) + early in-person ballots by county AND party.

Source: the Ohio SoS "Absentee and Early Voting Data" dashboard
(data.ohiosos.gov/portal/election-dashboards), a Power BI publish-to-web report.
The portal page and the SoS's downloadable text file sit behind a Cloudflare
browser challenge, but the report's own public query API
(wabi-us-gov-iowa-api.analysis.usgovcloudapi.net/public/reports/...) is not, so
we ask it the same aggregate questions the dashboard visuals do:

  table  'absentee v_detailed_grouped'
  dims   County_Name, Voter_Party_Bucketed, Election_Description
  sums   BALLOTS_SENT_NO_EIP / BALLOTS_RECEIVED_NO_EIP     (absentee by mail)
         BALLOTS_SENT_INCL_EIP / BALLOTS_RECEIVED_INCL_EIP (mail + early in person)
  page filters (locked in the report): 'Election Ballot Return Date Range' and
         VALID_DATE_BOOL_AGG = true -- without them the sums double count.

Ohio has no party registration; 'Voter_Party_Bucketed' is the SoS's voter-
affiliated party (from partisan-primary ballot history). Republican->rep,
Democratic->dem, Unaffiliated->npa, Minor Party + blank->oth.
cast = received incl. early in person; mail_voted = received by mail;
early_voted = the in-person difference; mail_provided = mail sent - received
(outstanding) -> partisan mail chase via common.compute_mail.

The SoS refreshes once a day (~noon ET) and stamps REFRESH_DATE, so this only
polls when a newer refresh could exist (see due()) and only pulls the county
rows once the stamp moves -- no request churn, no empty commits.

Run:  python scripts/oh_update.py [--force]
"""
import gzip
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "oh"
CONFIG_PATH = os.path.join(ROOT, "config", "oh.json")
GEO_PATH = os.path.join(ROOT, "assets", "oh-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")

PARTY = {"republican": "rep", "democratic": "dem", "unaffiliated": "npa"}
SUMS = ["BALLOTS_SENT_NO_EIP", "BALLOTS_RECEIVED_NO_EIP", "BALLOTS_SENT_INCL_EIP", "BALLOTS_RECEIVED_INCL_EIP"]


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def due(last_refresh, after_utc_hour):
    """True when a refresh newer than `last_refresh` (YYYY-MM-DD) could be out:
    never once we hold today's; before `after_utc_hour` UTC, not if we hold
    yesterday's (today's hasn't been published yet)."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if not last_refresh:
        return True
    if last_refresh >= today:
        return False
    return not (now.hour < after_utc_hour and last_refresh >= yesterday)


class PowerBI:
    """Minimal client for a Power BI publish-to-web report's public query API."""

    def __init__(self, api, key):
        self.api, self.key = api.rstrip("/") + "/", key
        m = self._call("%s/modelsAndExploration?preferReadOnlySession=true" % key)
        self.model = m["models"][0]["id"]
        self.dataset = m["models"][0]["dbName"]
        self.report = m["exploration"]["report"]["objectId"]

    def _call(self, path, body=None):
        req = urllib.request.Request(
            self.api + path, data=(json.dumps(body).encode() if body is not None else None),
            headers={"X-PowerBI-ResourceKey": self.key, "User-Agent": C.USER_AGENT,
                     "Accept": "application/json", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60, context=C._SSL_CTX) as r:
            raw = r.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw)

    def query(self, entity, dims, sums, where, aggs=None):
        """Group `entity` by `dims`, Sum() each of `sums` (plus any extra
        (column, function) `aggs`), filtered by {column: [literal, ...]}.
        Returns a list of row lists in select order."""
        def col(p):
            return {"Column": {"Expression": {"SourceRef": {"Source": "a"}}, "Property": p}}
        sel = [dict(col(d), Name="d%d" % i) for i, d in enumerate(dims)]
        sel += [{"Aggregation": {"Expression": col(s), "Function": 0}, "Name": "s%d" % i} for i, s in enumerate(sums)]
        sel += [{"Aggregation": {"Expression": col(c), "Function": fn}, "Name": "g%d" % i}
                for i, (c, fn) in enumerate(aggs or [])]
        cond = [{"Condition": {"In": {"Expressions": [col(c)], "Values": [[{"Literal": {"Value": v}}] for v in vals]}}}
                for c, vals in where.items()]
        q = {"Version": 2, "From": [{"Name": "a", "Entity": entity, "Type": 0}], "Select": sel, "Where": cond}
        body = {"version": "1.0.0", "cancelQueries": [], "modelId": self.model, "queries": [{
            "Query": {"Commands": [{"SemanticQueryDataShapeCommand": {
                "Query": q,
                "Binding": {"Primary": {"Groupings": [{"Projections": list(range(len(sel)))}]},
                            "DataReduction": {"DataVolume": 4, "Primary": {"Window": {"Count": 30000}}},
                            "Version": 1}}}]},
            "QueryId": "",
            "ApplicationContext": {"DatasetId": self.dataset, "Sources": [{"ReportId": self.report}]}}]}
        return _decode_dsr(self._call("querydata?synchronous=true", body))


def _decode_dsr(resp):
    """Flatten a Power BI data-shape result. Each row carries only the values
    that changed: bit i of 'R' = repeat column i from the previous row, bit i
    of 'Ø' = column i is null; dictionary-encoded columns index ValueDicts."""
    dsr = resp["results"][0]["result"]["data"]["dsr"]
    if "DS" not in dsr:
        raise RuntimeError("Power BI query error: %s" % json.dumps(dsr)[:200])
    ds = dsr["DS"][0]
    dicts = ds.get("ValueDicts", {})
    raw = ds["PH"][0].get("DM0", [])
    if not raw:
        return []
    schema = raw[0]["S"]
    out, prev = [], [None] * len(schema)
    for r in raw:
        # grouped rows pack values in 'C'; an aggregate-only row names them (M0, M1, ...)
        vals = list(r["C"]) if "C" in r else [r[s["N"]] for s in schema if s["N"] in r]
        rep, nul = r.get("R", 0), r.get("Ø", 0)
        row = []
        for i, s in enumerate(schema):
            if rep & (1 << i):
                v = prev[i]
            elif nul & (1 << i):
                v = None
            else:
                v = vals.pop(0)
                if s.get("DN") is not None and isinstance(v, int):
                    v = dicts[s["DN"]][v]
            row.append(v)
        out.append(row)
        prev = row
    return out


def _zero():
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0}


def _blk(d):
    return C.party_block(d["rep"], d["dem"], d["oth"], d["npa"])


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg.get("source", {})
    prev = load(LATEST_PATH, {}) or {}
    have = (prev.get("source") or {}).get("data_last_updated", "") if prev.get("counties") else ""
    if not force and not due(have, int(src.get("refresh_after_utc_hour", 15))):
        print("oh: holding the %s refresh; next one not due yet — skipping." % have)
        return 0

    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}

    where = {"Election Ballot Return Date Range": ["true"], "VALID_DATE_BOOL_AGG": ["true"],
             "Election_Description": ["'%s'" % src["election_description"]]}
    try:
        pbi = PowerBI(src["pbi_api"], src["pbi_resource_key"])
        stamp = pbi.query(src["pbi_entity"], [], [], where, aggs=[("REFRESH_DATE", 4)])  # Max
        refreshed = datetime.fromtimestamp(stamp[0][0] / 1000, timezone.utc).strftime("%Y-%m-%d") if stamp and stamp[0][0] else ""
        if not force and refreshed and refreshed == have:
            print("NOCHANGE  (still the %s refresh)" % have)
            return 0
        rows = pbi.query(src["pbi_entity"], ["County_Name", "Voter_Party_Bucketed"], SUMS, where)
    except Exception as e:  # noqa: BLE001
        print("OH Power BI fetch failed: %s" % str(e)[:160], file=sys.stderr)
        return 0
    if not rows:
        print("OH: no rows for %r — keeping previous snapshot." % src["election_description"])
        return 0

    # raw[county][measure] -> party dict of the four source sums; the statewide
    # entity is built from its own totals so it matches the dashboard exactly
    raw, unmatched = {}, []
    sw_raw = {m: _zero() for m in SUMS}
    for county, party, *vals in rows:
        g = gidx.get(_norm(county or ""))
        if not g:
            unmatched.append(county)
            continue
        p = PARTY.get((party or "").strip().lower(), "oth")
        r = raw.setdefault(g["name"], {"fips": g["fips"], **{m: _zero() for m in SUMS}})
        for m, v in zip(SUMS, vals):
            r[m][p] += int(v or 0)
            sw_raw[m][p] += int(v or 0)

    def entity(r):
        s_mail, r_mail, s_all, r_all = (r[m] for m in SUMS)
        ps = ("rep", "dem", "oth", "npa")
        ent = {"mail_voted": _blk(r_mail),
               "mail_provided": _blk({p: max(0, s_mail[p] - r_mail[p]) for p in ps}),
               "early_voted": _blk({p: max(0, r_all[p] - r_mail[p]) for p in ps}),
               "cast": _blk(r_all), "registered": 0, "turnout_pct": None}
        m = C.compute_mail(ent)
        if m:
            ent["mail"] = m
        return ent

    counties = {name: dict(entity(r), fips=r["fips"]) for name, r in raw.items()}
    statewide = entity(sw_raw)

    methods_present = [k for k in ("mail_voted", "early_voted") if statewide[k]["total"]]
    snap = {
        "state": STATE, "state_name": cfg.get("state_name", "Ohio"),
        "election": cfg.get("election", {}), "partisan": True,
        "source": {"primary": "Ohio SoS Absentee and Early Voting Data dashboard (Power BI); ballots by county & voter-affiliated party",
                   "election_description": src["election_description"], "data_last_updated": refreshed},
        "source_compiled": refreshed, "source_compiled_iso": C.utc_now_iso(),
        "methods_present": methods_present, "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties,
    }
    snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k != "source_compiled_iso"})
    snap["generated_at"] = C.utc_now_iso()

    changed = force or snap["data_hash"] != prev.get("data_hash")
    os.makedirs(DATA_DIR, exist_ok=True)
    cast = statewide["cast"]
    if changed:
        with open(LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        if cast["total"]:
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"generated_at": snap["generated_at"],
                                    "statewide": {"cast": [cast["rep"], cast["dem"], cast["oth"], cast["npa"], cast["total"]]}},
                                   separators=(",", ":")) + "\n")
        print("CHANGED  counties=%d  cast=%d  requested=%s  (R%s D%s NPA%s margin=%s)  as of %s"
              % (len(counties), cast["total"], (statewide.get("mail") or {}).get("requested"),
                 cast["rep"], cast["dem"], cast["npa"], cast["margin"], refreshed))
    else:
        print("NOCHANGE  (cast=%d, %d counties, as of %s)" % (cast["total"], len(counties), refreshed))
    if unmatched:
        print("  unmatched:", unmatched[:10])
    return 0


if __name__ == "__main__":
    sys.exit(main())
