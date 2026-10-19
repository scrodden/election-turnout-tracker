#!/usr/bin/env python3
"""Iowa turnout by county and registered party.

Official source: Iowa SoS "Daily Absentee Statistics - By County" PDF, linked
from sos.iowa.gov/iowans/election-results-statistics (the SoS site moved in
2026: the 2026 primary file is sites/default/files/2026-06/ABS Counties
2026.pdf; the old /elections/pdf/<year>/general/ path holds 2024 and earlier).
Hierarchical layout:
  <County>  Requested Issued Received
    <Party> Requested Issued Received      (Democrat/Republican/No Party/Libertarian/Other)
      <Receipt Method> ...                 (Counter/In-Office, Mail, Drop Box, ...)
We take each party's RECEIVED (returned) as ballots cast. A county's Received
equals the sum of its parties' Received (validated). Iowa early voting is
absentee (in-person at the office/satellite + mail), so this is the full
early-vote electorate by party. The file has no registration totals, so there is
no turnout%.

Until the general-election file is posted (a 'Daily Absentee Statistics - By
County' link under a 2026 Sept-Dec upload folder or /2026/general/), Iowa shows
the UF Election Lab's figures (built from the SoS's absentee file; IA_county.csv
plus IA_cd / IA_sdl / IA_sdu for the district maps), used unaltered under
CC BY-NC-ND 4.0. Requires pypdf for the official file.

Run:  python scripts/ia_update.py [--force]
      python scripts/ia_update.py --validate   (parse the persisted 2024 file to prove the parser)
"""
import os
import re
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "ia"
CONFIG_PATH = os.path.join(ROOT, "config", "ia.json")
GEO_PATH = os.path.join(ROOT, "assets", "ia-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
GENERAL_LINK = re.compile(r"/2026-(0[89]|1[0-2])/|/2026/general/", re.I)
ROW_RE = re.compile(r"^(.+?)\s+(\d+)\s+(\d+)\s+(\d+)$")


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def geo_index():
    idx = {}
    for ft in load(GEO_PATH)["features"]:
        p = ft["properties"]
        idx[norm(p["name"])] = {"name": p["name"], "fips": p["fips"]}
    return idx


def parse_absentee(full, idx, party_map):
    """Return ({county: {rep,dem,oth,npa}}, {county: total_received}) from the PDF."""
    counties = {}
    county_total = {}
    cur = None
    for line in full.split("\n"):
        m = ROW_RE.match(line.strip())
        if not m:
            continue
        label = m.group(1).strip()
        received = int(m.group(4))
        k = norm(label)
        if k in idx:                              # county line
            name = idx[k]["name"]
            # The PDF concatenates ~daily cumulative snapshots (newest first); once
            # a county repeats we've reached an older snapshot -> stop.
            if name in counties:
                break
            cur = name
            counties[cur] = {"fips": idx[k]["fips"], "rep": 0, "dem": 0, "oth": 0, "npa": 0}
            county_total[cur] = received
        elif cur and label in party_map:          # party line
            counties[cur][party_map[label]] += received
        # receipt-method lines (3 ints, not a county/party) are ignored
    return counties, county_total


# receipt methods -> in person vs by mail. The statewide PDF labels mailed-back
# ballots "UD"; the county PDF calls them "Mail" (same totals). Health-care-facility
# ballots are voted with visiting officials, not at an early-voting site, so they
# go with the mail/absentee group.
IN_PERSON = {"counter / in-office", "satellite", "in-person"}
BY_MAIL = {"mail", "ud", "drop box", "e-mail", "health care facility"}
NOT_RETURNED = "ballot not yet returned by voter"
LINE_RE = re.compile(r"^(.+?)\s+(\d+)\s+(\d+)(?:\s+(\d+))?$")


def parse_methods(full, idx, party_map):
    """County -> party -> receipt method, from the daily absentee PDF.
    -> ({county: {"fips", "early_voted", "mail_voted", "mail_provided": {rep,dem,oth,npa}}}, unknown_labels).
    Raises ValueError if a party's method lines don't add up to its Received."""
    zero = lambda: {"rep": 0, "dem": 0, "oth": 0, "npa": 0}   # noqa: E731
    out, unknown = {}, set()
    cur = party = None
    want = got = 0

    def close():
        if cur and party is not None and want != got:
            raise ValueError("%s / %s: methods sum to %d, Received %d" % (cur, party, got, want))
    for line in full.split("\n"):
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        label = m.group(1).strip()
        recv = int(m.group(4)) if m.group(4) is not None else 0
        k = norm(label)
        if label.lower().startswith("grand total"):   # end of the newest snapshot
            break
        if label in party_map:
            close()
            party, want, got = party_map[label], recv, 0
        elif k in idx:
            close()
            name = idx[k]["name"]
            if name in out:   # the PDF can repeat older daily snapshots after the newest one
                break
            cur, party = name, None
            out[cur] = {"fips": idx[k]["fips"], "early_voted": zero(), "mail_voted": zero(), "mail_provided": zero()}
        elif cur and party is not None:
            lab = label.lower()
            if lab == NOT_RETURNED:
                out[cur]["mail_provided"][party] += int(m.group(3))   # issued, not yet returned
                continue
            if lab not in IN_PERSON and lab not in BY_MAIL:
                unknown.add(label)
            out[cur]["early_voted" if lab in IN_PERSON else "mail_voted"][party] += recv
            got += recv
    close()
    return out, unknown


def pulled_at(full):
    """'Data Pulled: 8:00 AM (UTC) 10/16/2026' -> '2026-10-16T08:00:00Z' (or '')."""
    from datetime import datetime
    m = re.search(r"Data Pulled:\s*(\d{1,2}:\d{2}\s*[AP]M)\s*\(UTC\)\s*(\d{1,2}/\d{1,2}/\d{4})", full)
    if not m:
        return ""
    try:
        return datetime.strptime(m.group(2) + " " + m.group(1).replace(" ", ""), "%m/%d/%Y %I:%M%p").strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return ""


def build_method_snapshot(cfg, counties, src_label, pulled):
    counties_out = {}
    for name, s in counties.items():
        ent = {"fips": s["fips"]}
        for mk in ("early_voted", "mail_voted", "mail_provided"):
            b = s[mk]
            ent[mk] = C.party_block(b["rep"], b["dem"], b["oth"], b["npa"])
        ent["cast"] = C.add_blocks(ent["early_voted"], ent["mail_voted"])
        mail = C.compute_mail(ent)
        if mail:
            ent["mail"] = mail
        counties_out[name] = ent
    statewide = {mk: C.add_blocks(*[c[mk] for c in counties_out.values()]) for mk in ("early_voted", "mail_voted", "mail_provided", "cast")}
    mail = C.compute_mail(statewide)
    if mail:
        statewide["mail"] = mail
    return {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"],
        "source": {"primary": "Iowa Secretary of State Absentee Ballot Statistics (ballots by county, registered party and "
                              "receipt method: in person at the auditor's office or a satellite site vs. by mail)"},
        "source_compiled": src_label, "source_compiled_iso": pulled or C.utc_now_iso(),
        "methods_present": [m for m in ("early_voted", "mail_voted", "mail_provided") if statewide[m]["total"]],
        "method_labels": cfg.get("method_labels", {}), "mail_base_label": "Mail ballots issued",
        "statewide": statewide, "counties": counties_out,
    }


def build_snapshot(cfg, counties, src_label):
    counties_out = {}
    for name, s in counties.items():
        counties_out[name] = {"fips": s["fips"], "cast": C.party_block(s["rep"], s["dem"], s["oth"], s["npa"])}
    statewide = {"cast": C.add_blocks(*[c["cast"] for c in counties_out.values()]) if counties_out
                 else C.party_block(0, 0, 0, 0)}
    snap = {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"],
        "source": {"primary": "Iowa Secretary of State Absentee Ballot Statistics (ballots returned by county & registered party)"},
        "source_compiled": src_label, "source_compiled_iso": (C.utc_now_iso() if counties_out else ""),
        "methods_present": [], "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties_out,
    }
    return snap


def read_pdf_text(raw):
    import io
    import pypdf
    return "\n".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(raw)).pages)


def find_general_file(cfg):
    """The general-election 'Daily Absentee Statistics - By County' PDF, if
    posted: a link on the SoS statistics page under a Sept-Dec 2026 upload
    folder or /2026/general/ (the June primary's file is in 2026-06)."""
    page = cfg["source"]["stats_page"]
    try:
        h = C.http_get(page, retries=2, timeout=60, no_cache=True)
    except Exception as e:  # noqa: BLE001
        print("IA statistics page unavailable: %s" % str(e)[:80])
        return None
    import html as H
    import urllib.parse
    for href, txt in re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', h, re.S | re.I):
        t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", H.unescape(txt))).strip()
        u = urllib.parse.urljoin(page, H.unescape(href))
        if re.search(r"daily absentee statistics\W+by county", t, re.I) and GENERAL_LINK.search(u):
            prev_url = ((load(LATEST_PATH) if os.path.exists(LATEST_PATH) else {}).get("source") or {}).get("url")
            return newest_upload(u, also=prev_url)
    return None


def newest_upload(u, also=None):
    """The SoS site stores each re-upload under the next number (AbsenteeCounty2026_1.pdf,
    _2.pdf, ...), the page's link can lag, and an older day's file can be re-posted under a
    new number (10/19: _2 = 10/19 data, then _3 = the 10/16 file again). So check numbers
    1 .. linked+5 plus `also` (the last file used), read each one's "Data Pulled" stamp
    (cached in data/ia/_uploads.json by Last-Modified, so a file is only downloaded when
    it changes) and return the one with the newest data. Each existence check is a HEAD,
    or a 1-byte ranged GET where HEAD is refused (it was from GitHub's runners)."""
    import urllib.error
    import urllib.request
    m = re.match(r"^(.*?)(?:_(\d+))?\.pdf$", u)
    if not m:
        return u
    base, n = m.group(1), int(m.group(2) or 0)

    def modified(url):
        for method, extra in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
            try:
                req = urllib.request.Request(url, method=method, headers=dict({"User-Agent": C.USER_AGENT, "Accept": "*/*"}, **extra))
                with urllib.request.urlopen(req, timeout=30, context=C._SSL_CTX) as r:
                    return r.headers.get("Last-Modified") or "?"
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
            except Exception:  # noqa: BLE001
                pass
        return None
    cache_path = os.path.join(DATA_DIR, "_uploads.json")
    try:
        cache = load(cache_path)
    except (OSError, ValueError):
        cache = {}
    cands = [base + ".pdf"] + ["%s_%d.pdf" % (base, k) for k in range(1, n + 6)]
    if also and also not in cands and also.startswith(base):
        cands.append(also)
    found = []
    for c in cands:
        lm = modified(c)
        if lm is None:
            continue
        ent = cache.get(c) or {}
        if ent.get("last_modified") != lm or not ent.get("pulled"):
            try:
                ent = {"last_modified": lm, "pulled": pulled_at(read_pdf_text(C.http_get(c, binary=True, no_cache=True, retries=2)))}
            except Exception as e:  # noqa: BLE001
                print("IA: couldn't read %s (%s)" % (c.rsplit("/", 1)[-1], str(e)[:60]))
                continue
            cache[c] = ent
        found.append((ent.get("pulled") or "", c))
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cache, f, indent=1)
    print("IA uploads: %s" % ", ".join("%s=%s" % (c.rsplit("/", 1)[-1], p or "?") for p, c in sorted(found)))
    if not found:
        return u
    return max(found)[1]   # newest "Data Pulled" (ties: the later-numbered name sorts last)


def main():
    force = "--force" in sys.argv
    validate = "--validate" in sys.argv
    cfg = load(CONFIG_PATH)
    idx = geo_index()
    party_map = cfg["source"]["party_map"]

    if validate:
        raw = C.http_get(cfg["source"]["validation_url"], binary=True, no_cache=True)
        counties, totals = parse_absentee(read_pdf_text(raw), idx, party_map)
        snap = build_snapshot(cfg, counties, "validation")
        sw = snap["statewide"]["cast"]
        ok = sum(1 for c, s in counties.items()
                 if (s["rep"] + s["dem"] + s["oth"] + s["npa"]) == totals.get(c))
        print("Validation: matched counties=%d/%d  party-sum==county-total for %d/%d"
              % (len(counties), len(idx), ok, len(counties)))
        print("  statewide received: total=%s R=%s D=%s NPA=%s Oth=%s margin=%s"
              % (sw["total"], sw["rep"], sw["dem"], sw["npa"], sw["oth"], sw["margin"]))
        return 0

    import lab_standin as LAB
    os.makedirs(DATA_DIR, exist_ok=True)
    for kind, suffix in (("cd", "cd"), ("sdl", "sh"), ("sdu", "ss")):   # district maps, ~hourly
        LAB.districts(STATE, cfg, os.path.join(ROOT, "assets", "ia-%s.geojson" % suffix),
                      os.path.join(DATA_DIR, "districts_%s.json" % suffix), partisan=True, force=force, kind=kind)

    counties = {}
    src_label = ""
    method_snap = None
    url = find_general_file(cfg)
    if url:
        try:
            raw = C.http_get(url, binary=True, no_cache=True, retries=2)
            full = read_pdf_text(raw)
            counties, totals = parse_absentee(full, idx, party_map)
            if counties:
                src_label = cfg["election"]["name"]
                try:   # in person vs by mail (and ballots still out), checked against the party totals
                    by_method, unknown = parse_methods(full, idx, party_map)
                    def mtot(c):
                        b = by_method.get(c)
                        return sum(b["early_voted"][p] + b["mail_voted"][p] for p in ("rep", "dem", "oth", "npa")) if b else 0

                    def ptot(c):   # parse_absentee skips counties with nothing returned yet (2-number lines, e.g. Henry)
                        b = counties.get(c)
                        return sum(b[p] for p in ("rep", "dem", "oth", "npa")) if b else 0
                    if any(mtot(c) != ptot(c) for c in set(by_method) | set(counties)):
                        raise ValueError("method totals differ from the party totals")
                    if unknown:
                        print("IA: receipt methods counted with mail: %s" % sorted(unknown))
                    method_snap = build_method_snapshot(cfg, by_method, src_label, pulled_at(full))
                except Exception as e:  # noqa: BLE001
                    print("IA: method breakdown not used (%s) -- party totals only" % str(e)[:120], file=sys.stderr)
                    method_snap = None
        except Exception as e:  # noqa: BLE001
            print("IA official file unreadable: %s" % str(e)[:90])
    if not counties:
        if not LAB.run(STATE, cfg, GEO_PATH, LATEST_PATH, HISTORY_PATH, partisan=True, force=force,
                       role="stand-in until the SoS posts its general-election daily absentee statistics"):
            print("ia: no official file and no Election Lab figures yet.")
        return 0

    snap = method_snap or build_snapshot(cfg, counties, src_label)
    snap["source"]["url"] = url
    snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k != "source_compiled_iso"})
    snap["generated_at"] = C.utc_now_iso()

    prev = None
    if os.path.exists(LATEST_PATH):
        try:
            prev = load(LATEST_PATH).get("data_hash")
        except (ValueError, OSError):
            pass
    changed = force or (snap["data_hash"] != prev)
    os.makedirs(DATA_DIR, exist_ok=True)
    if changed:
        with open(LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        cast = snap["statewide"]["cast"]
        if cast["total"]:
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"generated_at": snap["generated_at"], "compiled": src_label,
                                    "data_hash": snap["data_hash"], "cast": cast["total"],
                                    "margin": cast["margin"]}, separators=(",", ":")) + "\n")
        print("CHANGED  counties=%d  cast=%s (R%s D%s NPA%s) margin=%s"
              % (len(snap["counties"]), cast["total"], cast["rep"], cast["dem"], cast["npa"], cast["margin"]))
    else:
        print("NOCHANGE  (hash %s)" % (prev or "")[:12])
    return 0


if __name__ == "__main__":
    sys.exit(main())
