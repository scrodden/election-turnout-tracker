#!/usr/bin/env python3
"""Fill Virginia per-locality early-vote turnout (data/va/locality.json) by
scraping VPAP's per-locality pages, which are the only public source of
locality-level Virginia early voting (VA ELECT's locality file is a paid,
voter-level list; VPAP's only bulk feed is the by-CD file used by va_update.py).

VPAP sits behind Cloudflare, which rate-limits bursts (HTTP 202 challenge stub
with an empty/tiny body). VPAP only refreshes once a day, so we scrape gently:
GROUP_SIZE localities per group, GROUP_PAUSE seconds between groups, at most once
a day. Anything that comes back challenged/unparseable is skipped and the prior
run's value for that locality is kept (never fabricated); coverage is recorded so
the page can flag partial/stale data. config/va_localities.json also powers the
outbound-link directory on the turnout page, so locality detail is always
reachable even when a scrape is fully blocked.

Each locality page embeds the cumulative split in inline chart JS
(value:N,label:'In Person' / 'Mail'), the headline total in an SVG <text
class="total">, per-1,000-registered in the barchart, and an "(As of M/D/YY)".

Run:  python scripts/va_locality_update.py [--force] [--limit N] [--slugs a,b,c]
      (--force ignores the once-a-day throttle; --limit/--slugs scrape a subset)
"""
import os
import re
import sys
import json
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "va"
CONFIG_PATH = os.path.join(ROOT, "config", "va_localities.json")
GEO_PATH = os.path.join(ROOT, "assets", "va-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
OUT_PATH = os.path.join(DATA_DIR, "locality.json")

GROUP_SIZE = int(os.environ.get("VA_LOC_GROUP_SIZE", "10"))
GROUP_PAUSE = float(os.environ.get("VA_LOC_GROUP_PAUSE", "60"))   # seconds between groups
ITEM_PAUSE = float(os.environ.get("VA_LOC_ITEM_PAUSE", "1.5"))    # seconds between pages
REFRESH_HOURS = 20.0    # VPAP updates ~daily; scrape at most this often


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_hours(iso):
    from datetime import datetime, timezone
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return 1e9


def geo_name_by_fips():
    """fips -> the geojson feature name (the map/table unit key)."""
    g = load(GEO_PATH, {"features": []})
    return {ft["properties"]["fips"]: ft["properties"]["name"] for ft in g["features"]}


def _block(total):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(total),
            "rep_pct": None, "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def _find_int(pat, html):
    m = re.search(pat, html)
    return int(m.group(1).replace(",", "")) if m else None


def parse_locality(html):
    """Return dict of numbers, or None if the page is a Cloudflare stub / not a
    real locality page (so the caller keeps the previous value)."""
    if not html or len(html) < 4000:
        return None
    if "label: 'In Person'" not in html and 'id="barchart"' not in html \
            and "<strong>2026</strong>" not in html:
        return None
    # headline total: SVG <text class="total">N</text> right before <strong>2026</strong>
    total = _find_int(r'class="total">([\d,]+)</text>\s*</svg>\s*</div>\s*<div><strong>2026</strong>', html)
    # cumulative split from inline chart JS
    inp = _find_int(r"value:\s*(\d+),\s*label:\s*'In Person'", html)
    mail = _find_int(r"value:\s*(\d+),\s*label:\s*'Mail'", html)
    if total is None and inp is None and mail is None:
        return None
    inp = inp or 0
    mail = mail or 0
    if total is None:
        total = inp + mail
    # per-1,000 registered: first barchart row is this locality
    per1000 = None
    b = html.find('id="barchart"')
    if b >= 0:
        m = re.search(r'<div class="bar background">\s*([\d.]+)\s*</div>', html[b:b + 3000])
        if m:
            per1000 = float(m.group(1))
    registered = int(round(total / per1000 * 1000)) if per1000 else 0
    turnout_pct = round(per1000 / 10.0, 2) if per1000 else None
    prev2022 = _find_int(r'class="total">([\d,]+)</text>\s*</svg>\s*</div>\s*<div><strong>2022</strong>', html)
    m = re.search(r'\(As of ([\d/]+)\)', html)
    as_of = m.group(1) if m else ""
    return {"total": total, "in_person": inp, "mail": mail, "registered": registered,
            "turnout_pct": turnout_pct, "final_2022": prev2022, "as_of": as_of}


def entry_from(fips, per):
    e = {"fips": fips, "cast": _block(per["total"]),
         "early_voted": _block(per["in_person"]), "mail_voted": _block(per["mail"]),
         "turnout_pct": per["turnout_pct"], "registered": per["registered"],
         "as_of": per["as_of"]}
    if per.get("final_2022") is not None:
        e["final_2022"] = per["final_2022"]
    return e


def main():
    force = "--force" in sys.argv
    limit = None
    slugs_filter = None
    for a in sys.argv:
        if a.startswith("--limit"):
            limit = int(a.split("=", 1)[1]) if "=" in a else int(sys.argv[sys.argv.index(a) + 1])
        if a.startswith("--slugs"):
            slugs_filter = set((a.split("=", 1)[1] if "=" in a else sys.argv[sys.argv.index(a) + 1]).split(","))

    prev = load(OUT_PATH, {}) or {}
    if prev.get("counties") and not force and age_hours(prev.get("generated_at", "")) < REFRESH_HOURS:
        print("locality.json is fresh (<%.0fh) — skipping (VPAP refreshes ~daily)." % REFRESH_HOURS)
        return 0

    cfg = load(CONFIG_PATH, {}) or {}
    locs = cfg.get("localities", [])
    if slugs_filter:
        locs = [x for x in locs if x["slug"] in slugs_filter]
    if limit:
        locs = locs[:limit]
    if not locs:
        print("no localities configured", file=sys.stderr)
        return 1

    name_by_fips = geo_name_by_fips()
    prev_counties = prev.get("counties", {}) or {}
    counties = {}
    ok = failed = kept = 0
    fail_slugs = []

    for i, loc in enumerate(locs):
        if i and i % GROUP_SIZE == 0:
            time.sleep(GROUP_PAUSE)
        elif i:
            time.sleep(ITEM_PAUSE)
        fips = loc["fips"]
        key = name_by_fips.get(fips, loc["name"])
        per = None
        try:
            html = C.http_get(loc["url"], retries=2, timeout=45,
                              accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
            per = parse_locality(html)
        except Exception as e:  # noqa: BLE001
            print("  ! %s fetch error: %s" % (loc["slug"], str(e)[:60]), file=sys.stderr)
        if per and per["total"] >= 0 and (per["in_person"] or per["mail"] or per["total"]):
            e = entry_from(fips, per)
            e["url"] = loc["url"]
            counties[key] = e
            ok += 1
        else:
            failed += 1
            fail_slugs.append(loc["slug"])
            if key in prev_counties:                 # keep last-good; never fabricate
                counties[key] = prev_counties[key]
                kept += 1

    def sw(field):
        return sum((counties[k].get(field, {}) or {}).get("total", 0) for k in counties)
    cast_total = sw("cast")
    mail_total = sw("mail_voted")
    inp_total = sw("early_voted")
    reg_total = sum(counties[k].get("registered", 0) for k in counties)
    as_ofs = sorted([counties[k].get("as_of", "") for k in counties if counties[k].get("as_of")])

    statewide = {"cast": _block(cast_total), "mail_voted": _block(mail_total),
                 "early_voted": _block(inp_total), "registered": reg_total,
                 "turnout_pct": (round(100.0 * cast_total / reg_total, 2) if reg_total else None)}
    doc = {
        "state": STATE, "election": cfg.get("election", "2026 November General"),
        "unit_label": "Locality", "unit_label_plural": "Localities",
        "source": "Virginia Public Access Project (VPAP), per-locality early-voting pages",
        "methods_present": [m for m, t in (("mail_voted", mail_total), ("early_voted", inp_total)) if t],
        "method_labels": {"cast": "All early ballots", "mail_voted": "By mail", "early_voted": "In person"},
        "coverage": {"ok": ok, "kept_prev": kept, "failed": failed,
                     "total": len(locs), "fail_slugs": fail_slugs[:20]},
        "as_of": (as_ofs[-1] if as_ofs else ""),
        "statewide": statewide, "counties": counties,
        "generated_at": now_iso(),
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    # only overwrite if we have at least some real coverage (avoid wiping to empty
    # when a whole run is Cloudflare-blocked)
    if counties or not prev.get("counties"):
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"))
    print("va locality: ok=%d kept=%d failed=%d / %d  cast=%d  as_of=%s"
          % (ok, kept, failed, len(locs), cast_total, doc["as_of"]))
    if fail_slugs:
        print("  failed:", ", ".join(fail_slugs[:15]), ("…" if len(fail_slugs) > 15 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
