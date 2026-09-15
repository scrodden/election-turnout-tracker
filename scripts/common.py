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


def http_get(url, retries=3, timeout=45, binary=False, no_cache=False):
    """GET a URL with a browser User-Agent, small retry/backoff. Returns text
    (utf-8, replacement on bad bytes) or bytes when binary=True.

    The Florida data files sit behind a caching layer (observed `x-cache: HIT`)
    that can serve a stale copy to an automated fetcher. no_cache=True adds a
    unique query param (changes the cache key) plus no-cache headers so each
    run pulls the freshest object, not yesterday's cached one."""
    last_err = None
    for attempt in range(retries):
        try:
            fetch_url = url
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
            }
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
