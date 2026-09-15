# Election Turnout Tracker

Tracks the **partisan composition of election turnout** — starting with **Florida, 2026** — by county and by method of voting (vote-by-mail, early in-person, and election day), shown as a sortable table and a red↔blue choropleth map. The site is static, hosted on GitHub Pages, and refreshes itself every ~10 minutes via GitHub Actions.

> **Live site:** `https://scrodden.github.io/election-turnout-tracker/` (enable GitHub Pages → Deploy from branch → `main` / root).

## What "partisan composition of turnout" means

Florida has closed primaries and therefore **party registration** for every voter. When a voter is sent or returns a ballot, the state reports which party that voter is *registered* with. This project aggregates those counts:

- **Republican / Democratic / Other / No Party Affiliation (NPA)** shares of ballots in each category, per county.
- **Partisan lean** = Republican share − Democratic share (e.g. `R+6.0` or `D+41.1`).

This reflects **who is voting**, not **how they voted**. A heavily Democratic-registration mail electorate does not guarantee Democratic results — it is a turnout signal, not a result. After the election, the [roadmap](#roadmap) compares this signal to the actual Governor and U.S. Senate outcomes.

## Data source

Florida Department of State, Division of Elections — **County Vote-by-Mail & Early Voting Reports**:

- Stats page: <https://countyfilesvbm-ev.floridados.gov/VoteByMailEarlyVotingReports/PublicStats>
- Underlying files (tab-separated), e.g. `https://electionfiles.floridados.gov/countyballotreportfiles/Stats_49894_VbmVoted.txt`

Reporting cadence (from the state): vote-by-mail activity is reported daily starting ~60 days before the election; early-voting activity starts the day after early voting opens. Counties file by 8 a.m. and the state refreshes at noon, 3 p.m., and 6 p.m. ET. Data is **cumulative through the prior day**. Because the source only changes a few times a day, most 10-minute update runs correctly detect "no change".

## How it works

```
GitHub Actions (cron */10)                         GitHub Pages (static)
        │                                                   ▲
        ▼                                                   │
scripts/fl_update.py  ──►  data/fl/latest.json  ──►  index.html + app.js
   (fetch + parse)         data/fl/history.jsonl        (map + table)
```

1. **`scripts/fl_update.py`** (Python standard library only) discovers the current data-file URLs from the stats page, downloads the tab-separated files, and computes per-county / statewide party shares and margins for each voting method.
2. It writes **`data/fl/latest.json`** (current snapshot) and appends to **`data/fl/history.jsonl`** — but only when the data actually changed (deduped by content hash), so commits stay meaningful.
3. The **GitHub Actions workflow** commits those files when they change. GitHub Pages serves the static site; the front end also re-fetches `latest.json` every 10 minutes.

The Cloudflare "managed challenge" on the portal is passed by sending a normal browser `User-Agent` (handled in `scripts/common.py`).

## Voting methods tracked

| Key | Meaning |
| --- | --- |
| `mail_provided` | Mail ballots **sent but not yet returned** (outstanding) |
| `mail_voted` | Mail ballots **returned / cast** |
| `early_voted` | Ballots cast **in person during early voting** |
| `election_day` | Ballots cast **on election day** |
| `cast` | Sum of all *cast* ballots (mail + early + election day) — the default map metric |

Early-voting and election-day files appear only once those periods begin; the updater picks them up automatically.

## Local development

No dependencies. From the repo root:

```bash
python scripts/fl_update.py     # fetch latest data into data/fl/
python -m http.server 8765      # then open http://127.0.0.1:8765
```

## Project structure

```
index.html, app.js, style.css      Static site (map + table)
assets/fl-counties.geojson         Slimmed Florida county boundaries (join by name/FIPS)
assets/states.json                 Registry of tracked states (multi-state ready)
config/fl.json                     Florida election + source + race configuration
scripts/common.py                  Shared, dependency-free fetch/parse helpers
scripts/fl_update.py               Florida fetcher / parser / writer
data/fl/latest.json                Current snapshot (committed by the workflow)
data/fl/history.jsonl              Time series (one line per change)
.github/workflows/update.yml       10-minute updater
```

## Roadmap

- [ ] **Results comparison** — after certification, load official Governor and U.S. Senate results by county and compare turnout lean vs. actual margin.
- [ ] **Frozen 2026 archive** — snapshot the final dataset for reference during the 2028 presidential cycle.
- [ ] **Time-series views** — charts of turnout and partisan lean over the early-vote window (from `history.jsonl`).
- [ ] **More states** — extend to every state that publishes early-vote turnout data, using the same state-module pattern (`config/<st>.json` + `scripts/<st>_update.py`).

## Disclaimer

Independent, non-partisan data project. Not affiliated with the State of Florida. All figures come directly from the official source linked above; see that source for authoritative numbers.
