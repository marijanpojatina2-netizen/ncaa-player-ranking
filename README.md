# NCAA Player Ranking Search (Division I)

A self-hosted web app to **search, filter, and rank NCAA Division I men's
basketball players** by any combination of stats using **weight sliders**,
blending raw production with **conference strength** into a single tunable
composite score — built for scouting European-import candidates.

- **Backend:** Python 3.11+, FastAPI + uvicorn
- **Storage:** SQLite (`data/ncaa.db`), plain `sqlite3` (no ORM)
- **Frontend:** single page served by FastAPI — Tabulator.js table + range-input
  weight sliders + Tailwind (all via CDN, no build step)
- **Data sources (free):** Bart Torvik (stats, conference strength, height,
  player-id for career linking) + ESPN's public roster API (position, height,
  weight). No API keys.
- **Multi-season:** ingest several seasons at once; each player's 4-year college
  career is linked by Torvik player id and shown on row click.
- **No VPS? Build it on GitHub.** A `workflow_dispatch` GitHub Action scrapes on
  GitHub's runners (which have internet) and commits the finished DB — see
  "Building the database on GitHub" below.

> **Why no paid API?** Free Bart Torvik already exposes per-game production,
> efficiency, advanced metrics, and conference strength — everything the
> composite ranking needs for D1. Paid feeds (e.g. CollegeBasketballData.com)
> only buy a cleaner JSON contract and uptime guarantees; they are optional.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1) (Optional) Try it immediately with SYNTHETIC demo data — no network needed.
python -m ingest.seed_demo --season 2026 --clear

# 2) Run the server
uvicorn app.main:app --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

Default landing view: **current season, Seniors**, sorted by a balanced
composite with conference strength weighted.

> **Demo data is clearly fake.** Every seeded row has `source='DEMO_SYNTHETIC'`
> and names like `D1 Demo Player 7`. It exists only to exercise the UI/scoring
> offline. Replace it with real data via the ingestion command below.

---

## Ingesting real data

Ingestion is **idempotent** and parameterized by `--season YYYY`. Raw HTTP
responses are cached under `data/cache/`; pass `--refresh` to bypass.

```bash
# Single season:
python -m ingest.torvik_d1 --season 2026
# Multiple seasons (for 4-year career history):
python -m ingest.torvik_d1 --seasons 2023 2024 2025 2026
# Then enrich with position / height / weight from ESPN rosters:
python -m ingest.espn_roster --seasons 2023 2024 2025 2026
```

Torvik provides player-season advanced stats (`getadvstats.php`), team ratings
(`trank.php`) → per-conference 0–1 strength, plus **height** and a stable
**player id** used to link a player's seasons across team changes. ESPN's roster
API then fills **position, height, and weight** (matched by name). It prints a
sanity check (row counts + top senior scorers).

**Athleticism:** measured athleticism (wingspan/vertical/sprint) only exists for
NBA-combine invitees (~80/yr) — i.e. *not* the players a European club imports —
so it is intentionally not ingested. Instead an **athleticism index (proxy)** is
computed from box stats (block%, steal%, ORB%, dunk rate, rim-attempt rate) and
exposed as a normal weightable metric. It is labeled a proxy, not a measurement.

> ⚠️ **Verify the column mapping each season.** Torvik's CSV endpoints have **no
> header row**, so columns are mapped positionally and the layout can drift. The
> per-game counting stats (PPG/RPG/APG…) live in the tail of the row and are the
> most likely to move; they are left **unmapped (NULL) by default** until you
> confirm their indices:
>
> ```bash
> python -m ingest.torvik_d1 --season 2025 --inspect          # prints indexed columns
> python -m ingest.torvik_d1 --season 2025 --inspect-what team
> ```
>
> Then either edit `TORVIK_PLAYER_TAIL` in `ingest/torvik_d1.py`, **or** (no code
> edit) drop a `data/torvik_columns.json` override, e.g.:
>
> ```json
> { "player_tail": { "pts_pg": 60, "reb_pg": 58, "ast_pg": 56 },
>   "team": { "barthag": 18 } }
> ```

**Conference strength** is derived automatically during ingestion: the mean
team `barthag` per conference, min-max normalized to 0–1.

> **Note:** run ingestion where outbound HTTPS to `barttorvik.com` / ESPN is
> allowed. Some sandboxed/CI environments block it — GitHub Actions runners do
> not (see below).

---

## Building the database on GitHub (no VPS needed)

The data is concluded, so you don't need a server running 24/7 — you just need
the database built once. The included Action does exactly that on GitHub's
runners (which have open internet):

1. Push this repo to GitHub.
2. Go to **Actions → "Build NCAA database" → Run workflow**, set the seasons
   (default `2023 2024 2025 2026`), and run it.
3. It scrapes Torvik + ESPN, then commits `data/ncaa.db` and a portable
   `web/data.json` snapshot back to the branch.

Workflow file: `.github/workflows/build-ncaa-db.yml`. After it runs, pull the
branch and `uvicorn app.main:app …` locally against the committed DB.

---

## How the composite score works

1. Every numeric metric is converted to a **percentile (0–100) within the
   season's population** — so a player's standing reflects all peers, not just
   the filtered subset.
2. The composite is a **weighted average** of the player's percentiles over the
   metrics you gave a positive weight to.
3. **Lower-is-better** metrics (turnovers, TO%, DRtg) are inverted automatically.
4. **Conference strength** is just another weightable metric (already 0–1,
   scored as `value × 100`).
5. **Missing (NULL) metrics** — default `exclude`: dropped from that player's
   average and the remaining weights renormalized. Alternative `median`: treated
   as the 50th percentile. Toggle in the UI.

---

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/meta` | seasons, conferences (+strength), classes, positions, rankable metrics (with `higher_is_better`), presets |
| `GET /api/players` | ranked rows + `composite_score`, paginated |
| `GET /api/career` | one player's year-by-year history (`?pid=` or `?name=&team=`) |
| `GET /api/export.csv` | same filters/weights → CSV download |
| `POST /api/refresh` | trigger re-ingestion (requires `REFRESH_TOKEN`) |

`/api/players` query params: `season`, `class` (repeatable, default `Sr`; use
`all` for every class), `conference` (repeatable), `position`, `min_gp`,
`min_minutes`, `min_conf_strength`, `min_height_in`, `max_height_in`,
`null_policy` (`exclude`|`median`), `page`,
`page_size`, `sort`, `dir`, and one `w_<metric>=0..100` per weighted metric
(e.g. `w_pts_pg=80`).

Protected refresh example:

```bash
export REFRESH_TOKEN=changeme
curl -X POST "http://localhost:8000/api/refresh?season=2025&token=changeme"
```

---

## Frontend features

- Filters: season, class (default Senior, toggleable + "All"), conference
  multi-select (strength shown), position, min games, min minutes, min
  conference strength, **height window (min/max inches)**, NULL-handling policy.
- A **weight slider** for every rankable metric — including the **athleticism
  index** — default = a balanced preset.
- **Presets:** Balanced, Scoring big, 3-and-D wing, Floor general, Rim protector,
  Rebounder, Efficiency, plus Reset.
- **Tabulator** results table: Composite first, conference + strength, class,
  **position / height / weight / athleticism**; sortable by any column; per-game
  cells show percentile on hover.
- **Row click → career panel:** the player's full year-by-year history (linked
  by Torvik player id), fetched from `/api/career`.
- **CSV export** and **shareable URL** (all filters + weights live in the query
  string) buttons.
- Data freshness (`updated_at`) shown in the header; `source` per row.

---

## Refreshing

The data is concluded, so no schedule is needed — rebuild only when you want a
new season (locally, or via the GitHub Action above):

```bash
python -m ingest.torvik_d1 --seasons 2024 2025 2026 2027 --refresh
python -m ingest.espn_roster --seasons 2024 2025 2026 2027
```

---

## Tests

```bash
python -m pytest tests/ -q
```

Covers percentile normalization (ordering, ties, NULLs, inversion) and composite
scoring (weighting, NULL policies).

---

## Honest limitations

- **Not a pro-projection model.** This ranks *college production adjusted for
  level of competition*. It does not capture measured athleticism, physical
  measurables beyond height/weight, or shot detail. Treat it as a tool to narrow
  ~2,000 names to a watchlist, not to rank pro readiness.
- **Athleticism is a proxy.** Real combine measurements exist only for ~80 NBA
  prospects/year (not typical import targets), so the athleticism column is a
  box-stat-derived index, clearly labeled, not a measurement.
- **Position/height/weight depend on name-matching** Torvik ↔ ESPN rosters;
  a few players may be unmatched (left NULL). Heights also come from Torvik.
- **Source layout drifts.** Torvik CSV column order and ESPN endpoints may change;
  re-run `--inspect` and adjust the mapping (no code edit needed for Torvik
  columns — use `data/torvik_columns.json`).
- We **do not bulk-scrape sports-reference.com** (their TOS prohibits it) and we
  **never fabricate stats** — unavailable fields are stored as NULL.
- **Division II is intentionally out of scope.** The schema keeps a `division`
  column (defaults `D1`) so D2 could be re-added later without a migration.

## Project layout

```
app/      main.py · api.py · db.py · scoring.py
ingest/   torvik_d1.py · espn_roster.py · conferences.py
          export_json.py · seed_demo.py · common.py
data/     cache/ · ncaa.db
web/      index.html · app.js   (+ generated data.json)
tests/    test_scoring.py
.github/workflows/build-ncaa-db.yml   (one-click DB build on GitHub)
```
