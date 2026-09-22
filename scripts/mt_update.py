#!/usr/bin/env python3
"""Montana absentee-ballot turnout by county (turnout-only; MT has no party
registration). Source: the MT SoS "Montana Absentee Ballots By County" Tableau
dashboard (sosmt.gov/elections/absentee-ballot-count/ -> tableau-ext.mt.gov),
which tracks absentee ballots SENT vs RECEIVED by county for the 2026 general.
We report RECEIVED as ballots cast.

Tableau has no flat file, so we run its bootstrap flow: GET the view (cookies) ->
POST startSession/viewing (mints sessionid + stickySessionKey) -> POST
bootstrapSession (returns "len;json len;json"; the 2nd chunk holds the data
dictionary). We assemble the 'Absentee Map' worksheet rows (County, SUM(Ballots
Received), SUM(Ballots Sent), AGG(% Received)) from the column-oriented value
pools. tableau-ext.mt.gov is not bot-blocked, so this runs fine from CI.

Run:  python scripts/mt_update.py [--force]
"""
import os
import re
import sys
import json
import urllib.request
import urllib.parse
import http.cookiejar

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "mt"
CONFIG_PATH = os.path.join(ROOT, "config", "mt.json")
GEO_PATH = os.path.join(ROOT, "assets", "mt-counties.geojson")
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


# ---- Tableau bootstrap extraction -----------------------------------------
def _find_first(o, key, d=0):
    if d > 60:
        return None
    if isinstance(o, dict):
        if key in o:
            return o[key]
        for v in o.values():
            r = _find_first(v, key, d + 1)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _find_first(v, key, d + 1)
            if r is not None:
                return r
    return None


def _find_all(o, key, hits, d=0):
    if d > 60:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                hits.append(v)
            _find_all(v, key, hits, d + 1)
    elif isinstance(o, list):
        for v in o:
            _find_all(v, key, hits, d + 1)


def _split_chunks(s):
    out, i = [], 0
    while i < len(s):
        j = s.find(";", i)
        if j < 0:
            break
        try:
            n = int(s[i:j])
        except ValueError:
            break
        out.append(s[j + 1:j + 1 + n])
        i = j + 1 + n
    return out


def tableau_worksheet_rows(host, view, vizql_root, worksheet):
    """Return (columns_by_caption, updated_note). columns_by_caption maps
    fieldCaption -> list of values (parallel rows) for the given worksheet."""
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj),
                                     urllib.request.HTTPSHandler(context=C._SSL_CTX))
    ua = C.USER_AGENT

    def get(url):
        r = urllib.request.Request(url)
        r.add_header("User-Agent", ua)
        r.add_header("Accept", "text/html,application/xhtml+xml,*/*;q=0.8")
        return op.open(r, timeout=60).read().decode("utf-8", "replace")

    def post(url, form):
        r = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST")
        r.add_header("User-Agent", ua)
        r.add_header("Accept", "text/javascript")
        r.add_header("X-Requested-With", "XMLHttpRequest")
        r.add_header("Content-Type", "application/x-www-form-urlencoded")
        return op.open(r, timeout=90).read().decode("utf-8", "replace")

    qs = "?:embed=y&:showVizHome=no&:tabs=no&:toolbar=bottom"
    get(host + view + qs)
    cfg = json.loads(post(host + vizql_root + "/startSession/viewing" + qs + "&:redirect=auth", {}))
    sid = cfg["sessionid"]
    sheet = cfg.get("sheetId") or view.rsplit("/", 1)[-1]
    sticky = cfg.get("stickySessionKey", "")
    form = {
        "worksheetPortSize": '{"w":1000,"h":800}', "dashboardPortSize": '{"w":1000,"h":800}',
        "clientDimension": '{"w":1000,"h":800}', "renderMapsClientSide": "true",
        "isBrowserRendering": "true", "browserRenderingThreshold": "100",
        "formatDataValueLocally": "false", "clientNum": "", "navType": "Reload",
        "navSrc": "Boot", "devicePixelRatio": "1", "clientRenderPixelLimit": "25000000",
        "allowAutogenWorksheetPhoneLayouts": "true", "sheet_id": sheet,
        "stickySessionKey": sticky, "filterTileSize": "200", "locale": "en_US",
        "language": "en", "verboseMode": "false", "keychain_version": "1",
    }
    raw = post(host + vizql_root + "/bootstrapSession/sessions/" + sid, form)
    parsed = []
    for c in _split_chunks(raw):
        try:
            parsed.append(json.loads(c))
        except ValueError:
            pass

    # typed value pools
    seg_hits = []
    for p in parsed:
        _find_all(p, "dataSegments", seg_hits)
    pools = {}
    for seg in seg_hits:
        for _segid, segval in (seg or {}).items():
            for col in (segval or {}).get("dataColumns", []):
                pools.setdefault(col["dataType"], col["dataValues"])

    # worksheet column mapping
    vizmap = None
    for p in parsed:
        vizmap = _find_first(p, "genPresModelMapPresModel")
        if vizmap:
            break
    pmm = (vizmap or {}).get("presModelMap", {})
    ws = pmm.get(worksheet)
    if not ws:
        raise RuntimeError("MT: worksheet %r not in %s" % (worksheet, list(pmm.keys())))
    pcd = _find_first(ws, "paneColumnsData")
    cols = pcd["vizDataColumns"]
    pane = pcd["paneColumnsList"][0]["vizPaneColumns"]

    out = {}
    for c in cols:
        cap = c.get("fieldCaption")
        if not cap:
            continue
        dt = c["dataType"]
        ci = c["columnIndices"][0]
        vpc = pane[ci]
        idxs = vpc.get("valueIndices") or vpc.get("aliasIndices") or []
        pool = pools.get(dt, [])
        out[cap] = [pool[i] if 0 <= i < len(pool) else None for i in idxs]
    updated = _find_first({"x": parsed}, "lastUpdatedAt") or ""
    return out


def _block(total):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(total),
            "rep_pct": None, "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg.get("source", {})
    geo = load(GEO_PATH, {"features": []})
    gidx = {_norm(f["properties"]["name"]): f["properties"] for f in geo["features"]}

    counties = {}
    cast_total = sent_total = 0
    unmatched = []
    try:
        colmap = tableau_worksheet_rows(src["tableau_host"], src["tableau_view"],
                                        src["tableau_vizql_root"], src.get("worksheet", "Absentee Map"))
        names = colmap.get("County", [])
        recv = colmap.get("SUM(Ballots Received)", [])
        sent = colmap.get("SUM(Ballots Sent)", [])
        for i, nm in enumerate(names):
            if nm is None:
                continue
            g = gidx.get(_norm(nm))
            if not g:
                unmatched.append(nm)
                continue
            r = int(recv[i] or 0) if i < len(recv) else 0
            s = int(sent[i] or 0) if i < len(sent) else 0
            counties[g["name"]] = {"fips": g["fips"], "cast": _block(r), "sent": s,
                                   "turnout_pct": None, "registered": 0}
            cast_total += r
            sent_total += s
    except Exception as e:  # noqa: BLE001
        print("MT fetch/parse failed: %s" % str(e)[:120], file=sys.stderr)

    statewide = {"cast": _block(cast_total), "sent": sent_total, "registered": 0, "turnout_pct": None}
    snap = {
        "state": STATE, "state_name": cfg.get("state_name", "Montana"),
        "election": cfg.get("election", {}), "partisan": False,
        "source": {"primary": "MT SoS Absentee Ballots by County (Tableau); ballots returned by county, 2026 general (turnout-only)"},
        "source_compiled": "", "source_compiled_iso": C.utc_now_iso(),
        "methods_present": [], "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties,
    }
    snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k != "source_compiled_iso"})
    snap["generated_at"] = C.utc_now_iso()

    prev = load(LATEST_PATH, {}) or {}
    changed = force or snap["data_hash"] != prev.get("data_hash")
    os.makedirs(DATA_DIR, exist_ok=True)
    if changed:
        with open(LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        if cast_total:
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"generated_at": snap["generated_at"], "cast": cast_total,
                                    "sent": sent_total}, separators=(",", ":")) + "\n")
        print("CHANGED  counties=%d  received=%d  sent=%d" % (len(counties), cast_total, sent_total))
    else:
        print("NOCHANGE  (received=%d, %d counties)" % (cast_total, len(counties)))
    if unmatched:
        print("  unmatched:", unmatched[:10])
    return 0


if __name__ == "__main__":
    sys.exit(main())
