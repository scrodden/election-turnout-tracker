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
(outstanding) -> partisan mail chase via common.compute_mail (its base is
ballots SENT so far, not requests). mail_requested = Requested_Count, the
dashboard's "Total Requests Reported" (voters who have requested an absentee
ballot), by county and party.

The SoS refreshes once a day (~noon ET) and stamps REFRESH_DATE, so this only
polls when a newer refresh could exist (see due()) and only pulls the county
rows once the stamp moves; the result is cached in data/oh/state_snapshot.json.

County boards publish their own data hours (or a day) ahead of the state:
summary reports (Cuyahoga), voter-level absentee lists with each voter's
party (Franklin and Butler Election Vault, Clermont's candidate tool, Morrow,
Crawford, Henry, Trumbull, Hancock) and Ottawa's request list (no party). scripts/oh_county_reports.py reads them (config
source.county_reports, checked ~every 20 minutes, cached counts in
data/oh/county_reports.json -- never voter-level rows). Where a county's figure
is ahead for requests, ballots sent, mail returned or early in person, it is
used: list sources replace the state's party split; totals-only reports count
the excess as party not reported (common.party_block unk).

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
PS = ("rep", "dem", "oth", "npa")
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


STATE_CACHE = os.path.join(DATA_DIR, "state_snapshot.json")
REPORTS_CACHE = os.path.join(DATA_DIR, "county_reports.json")
REPORT_FIELDS = (("mail_requested", "requested", "requests"), ("mail_voted", "mail_returned", "mail returned"),
                 ("early_voted", "eip", "early in person"))


def state_refresh(cfg, src, cached, force):
    """The SoS dashboard's county x party figures. Queried only when a newer
    daily refresh could exist; otherwise the cached result (data/oh/
    state_snapshot.json) is returned. -> {"refreshed", "counties", "statewide"} or {}."""
    have = cached.get("refreshed", "")
    if cached and not force and not due(have, int(src.get("refresh_after_utc_hour", 15))):
        return cached
    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}
    where = {"Election Ballot Return Date Range": ["true"], "VALID_DATE_BOOL_AGG": ["true"],
             "Election_Description": ["'%s'" % src["election_description"]]}
    try:
        pbi = PowerBI(src["pbi_api"], src["pbi_resource_key"])
        stamp = pbi.query(src["pbi_entity"], [], [], where, aggs=[("REFRESH_DATE", 4)])  # Max
        refreshed = datetime.fromtimestamp(stamp[0][0] / 1000, timezone.utc).strftime("%Y-%m-%d") if stamp and stamp[0][0] else ""
        if cached and not force and refreshed and refreshed == have:
            return cached
        rows = pbi.query(src["pbi_entity"], ["County_Name", "Voter_Party_Bucketed"], SUMS, where)
        req_rows = pbi.query(src["pbi_entity"], ["County_Name", "Voter_Party_Bucketed"], ["Requested_Count"], where)
    except Exception as e:  # noqa: BLE001
        print("OH Power BI fetch failed: %s" % str(e)[:160], file=sys.stderr)
        return cached
    if not rows:
        print("OH: no rows for %r — keeping the previous state data." % src["election_description"])
        return cached

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
    req, sw_req = {}, _zero()
    for county, party, n in req_rows:
        g = gidx.get(_norm(county or ""))
        if not g:
            continue
        p = PARTY.get((party or "").strip().lower(), "oth")
        req.setdefault(g["name"], _zero())[p] += int(n or 0)
        sw_req[p] += int(n or 0)

    def entity(r, requested=None):
        s_mail, r_mail, s_all, r_all = (r[m] for m in SUMS)
        ps = ("rep", "dem", "oth", "npa")
        ent = {"mail_voted": _blk(r_mail),
               "mail_provided": _blk({p: max(0, s_mail[p] - r_mail[p]) for p in ps}),
               "early_voted": _blk({p: max(0, r_all[p] - r_mail[p]) for p in ps}),
               "cast": _blk(r_all), "registered": 0, "turnout_pct": None}
        if requested and sum(requested.values()):
            ent["mail_requested"] = _blk(requested)
        m = C.compute_mail(ent)
        if m:
            ent["mail"] = m
        return ent

    counties = {name: dict(entity(r, req.get(name)), fips=r["fips"]) for name, r in raw.items()}
    for name, rq in req.items():   # counties with requests but nothing sent yet
        if name not in counties:
            counties[name] = dict(entity({m: _zero() for m in SUMS}, rq), fips=gidx[_norm(name)]["fips"])
    state = {"refreshed": refreshed, "counties": counties, "statewide": entity(sw_raw, sw_req)}
    if unmatched:
        print("  unmatched:", unmatched[:10])
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_CACHE, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    return state


def _total(v):
    return sum(v.values()) if isinstance(v, dict) else v


def apply_reports(state, reports):
    """Where a county's own data is ahead of the state's figure for that
    county, use it. Voter-list sources carry each voter's party, so their
    party split replaces the state's; summary reports (totals only) count the
    excess as party not reported ('unk'). Ballots sent (voter lists) reset the
    outstanding count (sent - returned) used by the mail chase."""
    counties = json.loads(json.dumps(state["counties"]))
    statewide = json.loads(json.dumps(state["statewide"]))
    zero = C.party_block(0, 0, 0, 0)
    delta = {}
    notes, with_party, no_party = [], [], []

    def bump(key, old, new):
        d = delta.setdefault(key, dict.fromkeys(PS + ("unk",), 0))
        for p in PS:
            d[p] += new[p] - old[p]
        d["unk"] += new.get("unk", 0) - old.get("unk", 0)

    for county, rep in sorted(reports.items()):
        c = counties.get(county)
        if not c:
            continue
        state_sent = (c.get("mail_provided") or zero)["total"] + (c.get("mail_voted") or zero)["total"]
        used, partisan = [], False
        for key, field, label in REPORT_FIELDS:
            n = rep.get(field)
            b = c.get(key) or zero
            if n is None or _total(n) <= b["total"]:
                continue
            if isinstance(n, dict):
                c[key] = C.party_block(n["rep"], n["dem"], n["oth"], n["npa"])
                partisan = True
            else:
                known = b["rep"] + b["dem"] + b["oth"] + b["npa"]
                c[key] = C.party_block(b["rep"], b["dem"], b["oth"], b["npa"], unk=n - known)
            bump(key, b, c[key])
            used.append("%s %s (state %s)" % (label, format(_total(n), ","), format(b["total"], ",")))
        sent = rep.get("sent")
        if isinstance(sent, dict) and _total(sent) > state_sent:
            mv = c.get("mail_voted") or zero
            old = c.get("mail_provided") or zero
            c["mail_provided"] = C.party_block(*(max(0, sent[p] - mv[p]) for p in PS))
            bump("mail_provided", old, c["mail_provided"])
            used.append("ballots sent %s (state %s)" % (format(_total(sent), ","), format(state_sent, ",")))
            partisan = True
        old_cast = c.get("cast") or zero
        if used:
            c["cast"] = C.add_blocks(c.get("mail_voted"), c.get("early_voted"))
        cast = rep.get("cast")   # all methods, from a source with no method split
        if cast is not None and _total(cast) > (c.get("cast") or zero)["total"]:
            b = c.get("cast") or zero
            if isinstance(cast, dict):
                c["cast"] = C.party_block(cast["rep"], cast["dem"], cast["oth"], cast["npa"])
                partisan = True
            else:
                known = b["rep"] + b["dem"] + b["oth"] + b["npa"]
                c["cast"] = C.party_block(b["rep"], b["dem"], b["oth"], b["npa"], unk=cast - known)
            used.append("ballots returned, any method %s (state %s)" % (format(_total(cast), ","), format(old_cast["total"], ",")))
            c["cast_note"] = "all methods; the county's report doesn't split mail and in person"
        if not used:
            continue
        bump("cast", old_cast, c["cast"])
        m = C.compute_mail(c)
        if m:
            c["mail"] = m
        label = rep.get("label") or "%s County BOE reports" % county
        c["county_report"] = {"as_of": rep.get("as_of", ""), "url": rep.get("url", ""), "used": used,
                              "label": label, "partisan": partisan}
        c["source_url"], c["source_label"] = rep.get("url", ""), label
        (with_party if partisan else no_party).append(county)
        when = rep.get("as_of", "")
        notes.append("%s%s: %s" % (county, (" (as of %s)" % when) if when else "", "; ".join(used)))
    for key, d in delta.items():
        b = statewide.get(key) or zero
        statewide[key] = C.party_block(*(b[p] + d[p] for p in PS), unk=b.get("unk", 0) + d["unk"])
    if delta:
        m = C.compute_mail(statewide)
        if m:
            statewide["mail"] = m
    note = None
    if notes:
        note = "County boards' own data runs ahead of the state's daily dashboard; where a county's figure is higher, it is used."
        if with_party:
            note += (" %s publish%s voter-level lists that include each voter's party, so %s party splits are used too."
                     % (_join(with_party), "es" if len(with_party) == 1 else "", "its" if len(with_party) == 1 else "their"))
        if no_party:
            note += (" %s report%s totals only, so the extra ballots count in totals but not party shares."
                     % (_join(no_party), "s" if len(no_party) == 1 else ""))
        note += " " + " ".join(n + "." for n in notes)
    return counties, statewide, note


def _join(names):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg.get("source", {})
    prev = load(LATEST_PATH, {}) or {}
    state = state_refresh(cfg, src, load(STATE_CACHE, {}) or {}, force)
    if not state:
        print("OH: no state data yet.")
        return 0
    report_cfg = src.get("county_reports") or {}
    reports = {k: v for k, v in (load(REPORTS_CACHE, {}) or {}).items() if k in report_cfg or k == "_errors"}
    if report_cfg and (force or not C.checked_recently(DATA_DIR, "county_reports", minutes=20)):
        import oh_county_reports
        reports = oh_county_reports.fetch_all(report_cfg, reports)
        with open(REPORTS_CACHE, "w", encoding="utf-8") as f:
            json.dump(reports, f, separators=(",", ":"))
    errors = reports.get("_errors") or {}
    reports = {k: v for k, v in reports.items() if not k.startswith("_")}
    counties, statewide, note = apply_reports(state, reports)
    refreshed = state["refreshed"]
    newest = max([refreshed] + [r.get("as_of", "") for r in reports.values()])
    methods_present = [k for k in ("mail_voted", "early_voted", "mail_requested")
                       if (statewide.get(k) or {}).get("total")]
    body = {
        "state": STATE, "state_name": cfg.get("state_name", "Ohio"),
        "election": cfg.get("election", {}), "partisan": True,
        "source": {"primary": "Ohio SoS Absentee and Early Voting Data dashboard (Power BI); ballots by county & "
                              "voter-affiliated party" + ("; county boards' daily reports where ahead" if note else ""),
                   "election_description": src["election_description"], "data_last_updated": refreshed},
        "county_sources": {k: {"label": v.get("label", ""), "url": v.get("url", ""), "as_of": v.get("as_of", "")}
                           for k, v in sorted(reports.items())},
        "county_source_errors": {k: v.get("error", "") for k, v in sorted(errors.items())},
        "source_compiled": newest,
        "methods_present": methods_present, "method_labels": cfg.get("method_labels", {}),
        "mail_base_label": "Ballots sent",
        "statewide": statewide, "counties": counties,
    }
    if note:
        body["map_note"] = note
    h = C.data_hash(body)
    changed = force or h != prev.get("data_hash")
    now = C.utc_now_iso()
    snap = dict(body, data_hash=h, source_compiled_iso=now, generated_at=now if changed else prev.get("generated_at", now))
    os.makedirs(DATA_DIR, exist_ok=True)
    cast = statewide["cast"]
    if changed:
        with open(LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        if cast["total"]:
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"generated_at": now, "statewide": {"cast": [cast["rep"], cast["dem"], cast["oth"],
                                                                                cast["npa"], cast["total"]]}},
                                   separators=(",", ":")) + "\n")
        print("CHANGED  counties=%d  cast=%d (party not reported %d)  requested=%s  state data %s, newest report %s"
              % (len(counties), cast["total"], cast.get("unk", 0), (statewide.get("mail_requested") or {}).get("total"),
                 refreshed, newest))
    else:
        print("NOCHANGE  (cast=%d, state data %s, newest report %s)" % (cast["total"], refreshed, newest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
