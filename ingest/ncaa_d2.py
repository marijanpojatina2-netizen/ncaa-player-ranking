"""Division II ingestion from the official NCAA stats portal (stats.ncaa.org).

Unlike D1 (Bart Torvik), there is NO free advanced-metric source for D2. So we
pull the official **box-score season stats** for every D2 team and compute the
shooting-efficiency metrics (eFG%, TS%) ourselves. Fields that only exist in
paid feeds (BPM, usage, ORtg/DRtg, conference strength, ast%/reb% rates) are
stored as NULL — we never fabricate stats.

Source: stats.ncaa.org, via the maintained ``ncaa_stats_py`` library:
  * ``get_basketball_teams(season, level=2)``        -> every D2 team + team_id
  * ``get_basketball_player_season_stats(team_id)``  -> per-player season totals

The library returns SEASON TOTALS plus a few computed rate columns; we divide by
games played for per-game values and keep percentages.

IMPORTANT — where this runs
---------------------------
stats.ncaa.org must be reachable. Sandboxed/proxied environments that block
ncaa.org cannot fetch it; run this on a host with open internet (e.g. the
GitHub Actions runner used by .github/workflows/build-d2.yml).
"""
from __future__ import annotations

import argparse
import re
import unicodedata
from typing import Optional

from app.db import get_conn, init_db
from ingest.common import normalize_class, parse_height_to_inches, to_float, utcnow_iso

# These match the D1 ingest so the same DB/JSON consumers work unchanged.
PLAYER_FIELDS = [
    "name", "team", "conference", "division", "class", "position", "season",
    "gp", "min_pg", "min_pct", "pts_pg", "reb_pg", "oreb_pg", "dreb_pg",
    "orb_pct", "drb_pct", "ast_pg", "ast_pct", "stl_pg", "blk_pg", "tov_pg",
    "to_pct", "blk_pct", "stl_pct", "fg_pct", "fg2_pct", "fg3_pct", "ft_pct",
    "fg3a_rate", "fta_rate", "efg_pct", "ts_pct", "usage", "ortg", "drtg",
    "bpm", "torvik_pid", "height_in", "weight_lb", "dunk_rate", "rim_rate",
    "source", "updated_at",
]


def _slug(name: str) -> str:
    """Stable, accent-folded key used to link a player's seasons in the UI.

    stats.ncaa.org issues a fresh player_id every season, so we key career
    history on the normalized name instead (good enough for D2 grouping)."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return f"ncaa-{s}" if s else None


def _g(row: dict, *keys):
    """First present, non-empty value among possible column spellings."""
    for k in keys:
        if k in row and row[k] is not None:
            return row[k]
    return None


def _per_game(total, gp) -> Optional[float]:
    t, g = to_float(total), to_float(gp)
    if t is None or not g:
        return None
    return round(t / g, 4)


def _pct100(frac) -> Optional[float]:
    """Library reports rates as 0..1 fractions; our schema stores TS%/eFG%/TOV%
    on a 0..100 scale (matching the D1 Torvik values)."""
    v = to_float(frac)
    return round(v * 100, 2) if v is not None else None


def _map_row(row: dict, season: int) -> Optional[dict]:
    name = (str(_g(row, "player_full_name") or "")).strip()
    if not name or name.lower() in ("player", "name", "total", "totals"):
        return None
    gp = to_float(_g(row, "GP"))
    min_total = to_float(_g(row, "MP_total_seconds"))
    min_pg = round((min_total / 60.0) / gp, 4) if (min_total and gp) else None
    return {
        "name": name,
        "team": (str(_g(row, "school_name") or "")).strip() or None,
        "conference": (str(_g(row, "team_conference_name") or "")).strip() or None,
        "division": "D2",
        "class": normalize_class(_g(row, "player_class")),
        "position": (str(_g(row, "player_position") or "")).strip() or None,
        "season": season,
        "gp": gp,
        "min_pg": min_pg,
        "min_pct": None,
        "pts_pg": _per_game(_g(row, "PTS"), gp),
        "reb_pg": _per_game(_g(row, "TRB"), gp),
        "oreb_pg": _per_game(_g(row, "ORB"), gp),
        "dreb_pg": _per_game(_g(row, "DRB"), gp),
        "orb_pct": None,
        "drb_pct": None,
        "ast_pg": _per_game(_g(row, "AST"), gp),
        "ast_pct": None,
        "stl_pg": _per_game(_g(row, "STL"), gp),
        "blk_pg": _per_game(_g(row, "BLK"), gp),
        "tov_pg": _per_game(_g(row, "TOV"), gp),
        "to_pct": _pct100(_g(row, "TOV%")),
        "blk_pct": None,
        "stl_pct": None,
        "fg_pct": to_float(_g(row, "FG%")),
        "fg2_pct": to_float(_g(row, "2P%", "2FG%")),
        "fg3_pct": to_float(_g(row, "3P%", "3FG%")),
        "ft_pct": to_float(_g(row, "FT%")),
        "fg3a_rate": None,
        "fta_rate": None,
        "efg_pct": _pct100(_g(row, "eFG%")),
        "ts_pct": _pct100(_g(row, "TS%")),
        "usage": None,
        "ortg": None,
        "drtg": None,
        "bpm": None,
        "torvik_pid": _slug(name),     # career-link key (name-based for D2)
        "height_in": parse_height_to_inches(_g(row, "player_height")),
        "weight_lb": to_float(_g(row, "player_weight")),
        "dunk_rate": None,
        "rim_rate": None,
        "source": "stats.ncaa.org",
    }


def _install_ncaa_proxy() -> None:
    """stats.ncaa.org returns HTTP 403 to datacenter IPs (GitHub runners), so we
    reroute the library's single fetch point (``_get_webpage``) through a read
    proxy that renders from a real browser (r.jina.ai), with allorigins as a
    fallback. Same trick the D1 Torvik build uses for CloudFront. A polite delay
    keeps us under the proxy's free rate limit. Direct is tried first in case a
    given environment isn't blocked. Override via NCAA_FETCH_PROXIES."""
    import os
    import time
    from urllib.parse import quote

    import requests
    from ncaa_stats_py import basketball as _bb
    from ncaa_stats_py import utls as _utls

    templates = [t for t in os.environ.get(
        "NCAA_FETCH_PROXIES",
        "https://r.jina.ai/{rawurl} https://api.allorigins.win/raw?url={url}",
    ).split() if t]
    ua = {"User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_4) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )}
    delay = float(os.environ.get("NCAA_FETCH_DELAY", "3.0"))

    def fetch(url: str):
        candidates = [(url, ua)]
        for t in templates:
            pu = t.replace("{rawurl}", url).replace("{url}", quote(url, safe=""))
            h = dict(ua)
            if "r.jina.ai" in t:
                h["X-Return-Format"] = "html"   # raw HTML, not reformatted markdown
            candidates.append((pu, h))
        last = None
        for cu, h in candidates:
            try:
                resp = requests.get(cu, headers=h, timeout=(10, 75))
                if resp.status_code == 200 and resp.text and resp.text.strip():
                    time.sleep(delay)           # politeness / rate-limit guard
                    return resp
                last = f"HTTP {resp.status_code}"
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
        raise ConnectionRefusedError(f"[ncaa-d2 proxy] all candidates failed for {url}: {last}")

    _utls._get_webpage = fetch
    _bb._get_webpage = fetch
    print(f"[ncaa-d2] fetch proxy installed (direct -> {templates}, delay={delay}s)")


def fetch_players(season: int) -> list[dict]:
    # Imported lazily so the rest of the app never needs pandas/ncaa_stats_py.
    from ncaa_stats_py.basketball import (
        get_basketball_player_season_stats,
        get_basketball_teams,
    )
    _install_ncaa_proxy()

    teams = get_basketball_teams(season=season, level=2)  # men's D2
    team_ids = [int(t) for t in teams["team_id"].tolist()]
    print(f"[ncaa-d2] season {season}: {len(team_ids)} D2 teams to scrape")

    out: list[dict] = []
    for i, tid in enumerate(team_ids, 1):
        try:
            df = get_basketball_player_season_stats(tid)
        except Exception as exc:  # one bad team must not kill the whole build
            print(f"[ncaa-d2]   team {tid} failed: {exc}")
            continue
        rows = df.to_dict("records") if df is not None and not df.empty else []
        for row in rows:
            rec = _map_row(row, season)
            if rec:
                out.append(rec)
        if i % 25 == 0 or i == len(team_ids):
            print(f"[ncaa-d2]   {i}/{len(team_ids)} teams, {len(out)} players so far")
    return out


def upsert_players(records: list[dict]) -> int:
    if not records:
        return 0
    now = utcnow_iso()
    placeholders = ",".join("?" for _ in PLAYER_FIELDS)
    _no_update = ("name", "team", "season", "division")
    update_cols = ",".join(f"{c}=excluded.{c}" for c in PLAYER_FIELDS if c not in _no_update)
    sql = f"""
        INSERT INTO players ({",".join(PLAYER_FIELDS)})
        VALUES ({placeholders})
        ON CONFLICT(name, team, season, division) DO UPDATE SET {update_cols}
    """
    written = 0
    with get_conn() as conn:
        for rec in records:
            rec = {**rec, "updated_at": now}
            conn.execute(sql, [rec.get(f) for f in PLAYER_FIELDS])
            written += 1
        conn.commit()
    return written


def ingest(season: int) -> None:
    init_db()
    print(f"[ncaa-d2] ingesting D2 players for {season} ...")
    players = fetch_players(season)
    if not players:
        raise RuntimeError(
            f"NCAA returned no D2 players for {season} (blocked or layout changed). "
            "Refusing to commit empty data."
        )
    n = upsert_players(players)
    print(f"[ncaa-d2] wrote {n} D2 player rows for {season}")
    _sanity_check(season)


def ingest_many(seasons: list[int]) -> None:
    for s in seasons:
        ingest(s)


def _sanity_check(season: int) -> None:
    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM players WHERE division='D2' AND season=?", (season,)
        ).fetchone()["c"]
        print(f"[sanity] D2 {season}: {total} players")
        top = conn.execute(
            """SELECT name, team, conference, pts_pg FROM players
               WHERE division='D2' AND season=? AND pts_pg IS NOT NULL
               ORDER BY pts_pg DESC LIMIT 5""",
            (season,),
        ).fetchall()
        if top:
            print("[sanity] top D2 scorers:")
            for r in top:
                print(f"   {r['name']:<24} {str(r['team']):<20} {str(r['conference']):<10} {r['pts_pg']} ppg")
        else:
            print("[sanity] no pts_pg populated — column mapping likely changed")


def main():
    ap = argparse.ArgumentParser(description="NCAA Division II ingestion (stats.ncaa.org)")
    ap.add_argument("--season", type=int, help="single season, e.g. 2026")
    ap.add_argument("--seasons", type=int, nargs="+",
                    help="multiple seasons, e.g. --seasons 2023 2024 2025 2026")
    args = ap.parse_args()
    if args.seasons:
        ingest_many(args.seasons)
    elif args.season:
        ingest(args.season)
    else:
        ap.error("provide --season or --seasons")


if __name__ == "__main__":
    main()
