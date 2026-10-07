#!/usr/bin/env python3
"""Ohio county boards of elections' own daily reports, which run well ahead of
the Secretary of State's once-a-day dashboard (e.g. on 10/6/26 Cuyahoga's
early in-person report showed 1,911 ballots while the state showed 2
statewide). No party breakdown, so oh_update counts any excess over the
state's party data as "party not reported".

Each county has its own format, so each gets a small parser registered in
PARSERS (keyed by county name as in assets/oh-counties.geojson) and listed in
config/oh.json county_reports. A parser returns any of:
  requested     absentee applications processed to date
  mail_returned valid mail ballots returned to date
  eip           early in-person ballots cast to date
  as_of         the report's own time stamp ("YYYY-MM-DD HH:MM")
"""
import io
import re
import sys
from datetime import datetime

import common as C


def _pdf_text(url):
    import pypdf
    raw = C.http_get(url, binary=True, no_cache=True, retries=2)
    return "\n".join(p.extract_text(extraction_mode="layout") for p in pypdf.PdfReader(io.BytesIO(raw)).pages)


def _num(text, label):
    m = re.search(re.escape(label) + r"\s*:?\s*([\d,]+)", text)
    return C.parse_number(m.group(1)) if m else None


def _stamp(text):
    """The report header's date and time (layout puts them on separate lines)."""
    d = re.search(r"(\d{1,2}/\d{1,2}/20\d{2})", text)
    t = re.search(r"(\d{1,2}:\d{2}:\d{2})\s*([AP]M)", text)
    if not (d and t):
        return ""
    return datetime.strptime("%s %s%s" % (d.group(1), t.group(1), t.group(2)), "%m/%d/%Y %I:%M:%S%p").strftime("%Y-%m-%d %H:%M")


def cuyahoga(cfg):
    """Cuyahoga BOE 'current election' page: Vote-by-Mail Daily Update and
    Early In-Person Daily Update PDFs (stable paths; the ?sfvrsn suffix only
    versions them)."""
    vbm, eip = _pdf_text(cfg["vbm_pdf"]), _pdf_text(cfg["eip_pdf"])
    out = {"requested": _num(vbm, "Applications Processed to Date"),
           "mail_returned": _num(vbm, "Valid Ballots Returned to Date"),
           "eip": _num(eip, "EIP Ballots Issued and Cast to Date")}
    stamps = [s for s in (_stamp(vbm), _stamp(eip)) if s]
    out["as_of"] = max(stamps) if stamps else ""
    out["as_of_by_report"] = {"vote-by-mail": _stamp(vbm), "early in person": _stamp(eip)}
    if all(v is None for k, v in out.items() if k in ("requested", "mail_returned", "eip")):
        raise RuntimeError("Cuyahoga: no figures found in the daily reports")
    return out


PARSERS = {"Cuyahoga": cuyahoga}


def fetch_all(reports_cfg):
    """-> {county: figures}; a county whose report fails is left out (the state's data stands)."""
    out = {}
    for county, cfg in (reports_cfg or {}).items():
        fn = PARSERS.get(county)
        if not fn or county.startswith("_"):
            continue
        try:
            out[county] = dict(fn(cfg), url=cfg.get("page", ""))
        except Exception as e:  # noqa: BLE001
            print("OH: %s county report unavailable: %s" % (county, str(e)[:120]), file=sys.stderr)
    return out
