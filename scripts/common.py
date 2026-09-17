"""Shared, dependency-free helpers for the election turnout tracker.

Pure Python standard library only (urllib, json, hashlib, datetime, re) so the
GitHub Actions runner needs no `pip install` step.
"""
import json
import re
import ssl
import time
import hashlib
import urllib.request
from datetime import datetime, timezone

# A normal browser User-Agent. The Florida DOS portal sits behind a Cloudflare
# "managed challenge" that blocks default urllib/curl agents but lets ordinary
# browser agents through, so this header is required, not cosmetic.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

_SSL_CTX = ssl.create_default_context()


def http_get(url, retries=3, timeout=45, binary=False, no_cache=False, referer=None):
    """GET a URL with a browser User-Agent, small retry/backoff. Returns text
    (utf-8, replacement on bad bytes) or bytes when binary=True.

    The Florida data files sit behind a caching layer (observed `x-cache: HIT`)
    that can serve a stale copy to an automated fetcher. no_cache=True adds a
    unique query param (changes the cache key) plus no-cache headers so each
    run pulls the freshest object, not yesterday's cached one.

    Some portals (e.g. Nevada VIVID behind Imperva/Incapsula) require a Referer
    header; pass referer= for those."""
    last_err = None
    for attempt in range(retries):
        try:
            fetch_url = url
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
            }
            if referer:
                headers["Referer"] = referer
            if no_cache:
                sep = "&" if ("?" in url) else "?"
                fetch_url = url + sep + "_=" + str(int(time.time() * 1000)) + str(attempt)
                headers["Cache-Control"] = "no-cache, no-store, max-age=0"
                headers["Pragma"] = "no-cache"
            req = urllib.request.Request(fetch_url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
                raw = r.read()
                return raw if binary else raw.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 - want to retry any transient error
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError("GET failed after %d tries: %s (%s)" % (retries, url, last_err))


def parse_number(s):
    """'28,777' -> 28777 ; '01' -> 1 ; '' -> 0 ; None -> 0."""
    if s is None:
        return 0
    s = s.strip().replace(",", "")
    if s in ("", "-", "N/A"):
        return 0
    try:
        return int(s)
    except ValueError:
        try:
            return int(round(float(s)))
        except ValueError:
            return 0


def parse_compile_date(s):
    """Florida stamps look like '09/15/2026  8:04AM' (Eastern). Return a
    (raw, sortable_iso) tuple; sortable_iso is naive ET, good enough for
    ordering snapshots and picking the newest."""
    if not s:
        return ("", "")
    raw = s.strip()
    norm = re.sub(r"\s+", " ", raw)
    for fmt in ("%m/%d/%Y %I:%M%p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(norm, fmt)
            return (raw, dt.strftime("%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
    return (raw, "")


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def data_hash(obj):
    """Stable sha256 of a JSON-able object (keys sorted, compact)."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def pct(n, d):
    return round(100.0 * n / d, 2) if d else None


def party_block(rep, dem, oth, npa, compiled="", compiled_iso=""):
    """Build one party-breakdown block with shares and partisan margin.
    margin > 0 means a Republican lean; margin < 0 means Democratic.
    `compiled` is the raw display stamp; `compiled_iso` is a sortable form."""
    total = rep + dem + oth + npa
    rp, dp = pct(rep, total), pct(dem, total)
    return {
        "rep": rep, "dem": dem, "oth": oth, "npa": npa, "total": total,
        "rep_pct": rp, "dem_pct": dp,
        "oth_pct": pct(oth, total), "npa_pct": pct(npa, total),
        "margin": (round(rp - dp, 2) if (rp is not None and dp is not None) else None),
        "compiled": compiled, "compiled_iso": compiled_iso,
    }


def add_blocks(*blocks):
    """Sum several party_blocks (ignoring None/empty) into one, keeping the
    latest compile stamp (compared via the sortable ISO form)."""
    rep = dem = oth = npa = 0
    compiled, compiled_iso = "", ""
    for b in blocks:
        if not b:
            continue
        rep += b.get("rep", 0); dem += b.get("dem", 0)
        oth += b.get("oth", 0); npa += b.get("npa", 0)
        if b.get("compiled_iso", "") > compiled_iso:
            compiled_iso = b.get("compiled_iso", "")
            compiled = b.get("compiled", "")
    return party_block(rep, dem, oth, npa, compiled, compiled_iso)


# --- town/municipality -> county aggregation (for MCD states: ME/NH/VT/MA/RI/CT/WI) ---
_TOWN_CLASS = re.compile(r"\s+(city|town|village|plantation|gore|grant|township|reservation|"
                         r"unorganized territory|ut|ccd|borough|municipality)$")


def norm_town(name):
    """Normalize a municipality name to match a town->county crosswalk key:
    lowercase, drop the class suffix (city/town/village/plantation/...),
    saint->st, strip non-alphanumerics. Must match how the crosswalk was built
    (scripts that generate assets/crosswalks/<st>-town-county.json use this)."""
    n = (name or "").lower()
    n = _TOWN_CLASS.sub("", n)
    n = n.replace("saint ", "st ").replace("st. ", "st ")
    return re.sub(r"[^a-z0-9]", "", n)


def load_crosswalk(path):
    """Load assets/crosswalks/<st>-town-county.json -> {norm_town: {county, fips}}."""
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("map", {})


def aggregate_towns(rows, crosswalk):
    """Aggregate town-level rows to counties via a crosswalk.

    rows: {town_name: party_block-like dict OR {'rep','dem','oth','npa'} OR int total}
    Returns ({county_name: {fips, rep, dem, oth, npa}}, unmatched_town_names[]).
    An int value is treated as a turnout total and placed in 'oth' only if you
    want party-less; here ints go to a 'total' key instead. Party dicts sum by party.
    """
    out = {}
    unmatched = []
    for town, val in rows.items():
        m = crosswalk.get(norm_town(town))
        if not m:
            unmatched.append(town)
            continue
        c = out.setdefault(m["county"], {"fips": m["fips"], "rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": 0})
        if isinstance(val, dict):
            for k in ("rep", "dem", "oth", "npa"):
                c[k] += int(val.get(k, 0) or 0)
            c["total"] += int(val.get("total", val.get("rep", 0) + val.get("dem", 0) + val.get("oth", 0) + val.get("npa", 0)) or 0)
        else:
            c["total"] += int(val or 0)
    return out, unmatched


def _total_block(t):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(t or 0),
            "rep_pct": None, "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def merge_eday(snap, eday_path):
    """Merge a scraped Election-Day turnout file (data/<st>/eday.json, written by
    scripts/eday_scrape.py) into a TURNOUT-ONLY snapshot as an `election_day`
    method of party-less totals, recomputing each county's `cast` and the
    statewide totals. Idempotent; a missing/empty file leaves snap unchanged.
    Used by GA/TX (county/hub Election-Day feeds are totals, not by party)."""
    try:
        with open(eday_path, encoding="utf-8") as f:
            ed = json.load(f)
    except (OSError, ValueError):
        return snap
    counties = ed.get("counties", {})
    if not counties:
        return snap
    dst = snap.setdefault("counties", {})
    for name, rec in counties.items():
        c = dst.setdefault(name, {"fips": rec.get("fips", "")})
        c["election_day"] = _total_block(rec.get("total", 0))
        cast_total = sum(int((c.get(m) or {}).get("total", 0)) for m in ("mail_voted", "early_voted", "election_day"))
        c["cast"] = _total_block(cast_total)
    mp = set(snap.get("methods_present", []))
    mp.add("election_day")
    snap["methods_present"] = sorted(mp)
    sw = snap.setdefault("statewide", {})
    sw["election_day"] = _total_block(sum(int((dst[n].get("election_day") or {}).get("total", 0)) for n in dst))
    sw["cast"] = _total_block(sum(int((dst[n].get("cast") or {}).get("total", 0)) for n in dst))
    return snap
