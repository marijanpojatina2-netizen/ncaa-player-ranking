"""Division II ingestion from ncaa.com's official stat leaderboards.

There is NO free advanced-metric source for D2, and the complete portal
(stats.ncaa.org) is behind bot-protection that blocks servers. ncaa.com's public
stat pages ARE reachable, and expose per-category leaderboards as clean JSON via
the ncaa-api proxy (henrygd). We fetch ~12 individual categories, merge them per
player (by name + team), and compute shooting efficiency (eFG%, TS%) ourselves.

Limitations (be honest):
  * Only players who QUALIFY for a leaderboard appear (NCAA minimums), i.e. the
    better/higher-minute D2 players — not every roster player.
  * A player only carries the stats for the leaderboards they ranked in, so a
    pure scorer may have NULL rebounds, etc. Multi-category players are richest.
  * Advanced metrics (BPM, usage, ORtg/DRtg, rate%) don't exist free for D2 ->
    stored as NULL. We never fabricate stats.
  * ncaa.com only serves the CURRENT season, so D2 is a single season.

Source: https://ncaa-api.henrygd.me  (mirrors www.ncaa.com/stats/...).
Reachable from GitHub runners; blocked from the sandbox proxy.
"""
from __future__ import annotations

import argparse
import os
import re
import time
import unicodedata
from typing import Optional

import requests

from app.db import get_conn, init_db
from ingest.common import normalize_class, parse_height_to_inches, to_float, utcnow_iso

API_BASE = os.environ.get("NCAA_API_BASE", "https://ncaa-api.henrygd.me")
UA = {"User-Agent": "Mozilla/5.0 ncaa-scouting-tool/1.0 (personal scouting use)"}
DELAY = float(os.environ.get("NCAA_API_DELAY", "0.4"))

# Each individual category -> {our_field_or_tmpkey: ncaa_column}. Temp keys start
# with "_" and are used only to compute efficiency; they are not stored directly.
# NB: column NAMES collide across categories (e.g. "RPG" means total/off/def in
# 137/856/858), so we map explicitly per category rather than blindly merging.
CAT_MAP: dict[int, dict[str, str]] = {
    136: {"pts_pg": "PPG", "_fgm": "FGM", "_3fg": "3FG", "_ft": "FT", "_pts": "PTS"},
    137: {"reb_pg": "RPG"},
    140: {"ast_pg": "APG"},
    139: {"stl_pg": "STPG"},
    138: {"blk_pg": "BKPG"},
    628: {"min_pg": "MPG"},
    141: {"fg_pct": "FG%", "_fgm": "FGM", "_fga": "FGA"},
    142: {"ft_pct": "FT%", "_fta": "FTA"},
    143: {"fg3_pct": "3FG%", "_3fga": "3FGA"},
    856: {"oreb_pg": "RPG"},
    858: {"dreb_pg": "RPG"},
    473: {"_ast": "AST", "_to": "TO"},
}
IDENTITY = ("Name", "Team", "Cl", "Height", "Position", "G")

PLAYER_FIELDS = [
    "name", "team", "conference", "division", "class", "position", "season",
    "gp", "min_pg", "min_pct", "pts_pg", "reb_pg", "oreb_pg", "dreb_pg",
    "orb_pct", "drb_pct", "ast_pg", "ast_pct", "stl_pg", "blk_pg", "tov_pg",
    "to_pct", "blk_pct", "stl_pct", "fg_pct", "fg2_pct", "fg3_pct", "ft_pct",
    "fg3a_rate", "fta_rate", "efg_pct", "ts_pct", "usage", "ortg", "drtg",
    "bpm", "torvik_pid", "height_in", "weight_lb", "dunk_rate", "rim_rate",
    "source", "updated_at",
]


def _slug(name: str) -> Optional[str]:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return f"ncaa-{s}" if s else None


def _fetch_category(div: str, cid: int) -> list[dict]:
    """Fetch every page of one leaderboard category."""
    rows: list[dict] = []
    page, pages = 1, 1
    while page <= pages:
        url = f"{API_BASE}/stats/basketball-men/{div}/current/individual/{cid}"
        last = None
        for attempt in range(3):
            try:
                r = requests.get(url, params={"page": page}, headers=UA, timeout=(10, 60))
                if r.status_code == 200:
                    j = r.json()
                    pages = int(j.get("pages") or 1)
                    rows.extend(j.get("data", []))
                    break
                last = f"HTTP {r.status_code}"
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(1.5 * (attempt + 1))
        else:
            print(f"[ncaa-d2]   category {cid} page {page} failed: {last}")
            break
        page += 1
        time.sleep(DELAY)
    return rows


def _merge(div: str) -> dict[tuple, dict]:
    """Fetch all categories and merge rows per (name, team)."""
    players: dict[tuple, dict] = {}
    for cid, mapping in CAT_MAP.items():
        rows = _fetch_category(div, cid)
        print(f"[ncaa-d2]   category {cid}: {len(rows)} rows")
        for row in rows:
            name = (row.get("Name") or "").strip()
            team = (row.get("Team") or "").strip()
            if not name or not team:
                continue
            key = (name.lower(), team.lower())
            p = players.setdefault(key, {})
            for k in IDENTITY:
                if row.get(k) and not p.get(k):
                    p[k] = row[k]
            for field, col in mapping.items():
                if row.get(col) not in (None, ""):
                    p[field] = row[col]
    return players


def _eff(p: dict) -> tuple[Optional[float], Optional[float]]:
    """eFG% and TS% (0-100) from season totals, when available."""
    fgm, fga = to_float(p.get("_fgm")), to_float(p.get("_fga"))
    fta, pts = to_float(p.get("_fta")), to_float(p.get("_pts"))
    tfg = to_float(p.get("_3fg"))
    efg = ts = None
    if fga and fga > 0:
        if fgm is not None and tfg is not None:
            efg = round((fgm + 0.5 * tfg) / fga * 100, 2)
        if pts is not None and fta is not None:
            tsa = fga + 0.44 * fta
            if tsa > 0:
                ts = round(pts / (2 * tsa) * 100, 2)
    return efg, ts


def _frac(v) -> Optional[float]:
    """ncaa.com percentages are 0-100; store shooting %s as 0-1 fractions
    (matching the D1 Torvik fg2/fg3/ft values)."""
    f = to_float(v)
    return round(f / 100, 4) if f is not None else None


def _build_record(p: dict, season: int) -> Optional[dict]:
    name = (p.get("Name") or "").strip()
    team = (p.get("Team") or "").strip()
    if not name or not team:
        return None
    gp = to_float(p.get("G"))
    tov_total = to_float(p.get("_to"))
    tov_pg = round(tov_total / gp, 4) if (tov_total is not None and gp) else None
    # Assists: prefer the APG leaderboard; else derive from the A/TO total.
    ast_pg = to_float(p.get("ast_pg"))
    if ast_pg is None and p.get("_ast") and gp:
        ast_pg = round(to_float(p.get("_ast")) / gp, 4)
    efg, ts = _eff(p)
    return {
        "name": name,
        "team": team,
        "conference": None,                 # ncaa.com leaderboards omit conference
        "division": "D2",
        "class": normalize_class(p.get("Cl")),
        "position": (p.get("Position") or "").strip() or None,
        "season": season,
        "gp": gp,
        "min_pg": to_float(p.get("min_pg")),
        "min_pct": None,
        "pts_pg": to_float(p.get("pts_pg")),
        "reb_pg": to_float(p.get("reb_pg")),
        "oreb_pg": to_float(p.get("oreb_pg")),
        "dreb_pg": to_float(p.get("dreb_pg")),
        "orb_pct": None,
        "drb_pct": None,
        "ast_pg": ast_pg,
        "ast_pct": None,
        "stl_pg": to_float(p.get("stl_pg")),
        "blk_pg": to_float(p.get("blk_pg")),
        "tov_pg": tov_pg,
        "to_pct": None,
        "blk_pct": None,
        "stl_pct": None,
        "fg_pct": _frac(p.get("fg_pct")),
        "fg2_pct": None,
        "fg3_pct": _frac(p.get("fg3_pct")),
        "ft_pct": _frac(p.get("ft_pct")),
        "fg3a_rate": None,
        "fta_rate": None,
        "efg_pct": efg,
        "ts_pct": ts,
        "usage": None,
        "ortg": None,
        "drtg": None,
        "bpm": None,
        "torvik_pid": _slug(name),
        "height_in": parse_height_to_inches(p.get("Height")),
        "weight_lb": None,
        "dunk_rate": None,
        "rim_rate": None,
        "source": "ncaa.com",
    }


def fetch_players(season: int) -> list[dict]:
    merged = _merge("d2")
    out = []
    for p in merged.values():
        rec = _build_record(p, season)
        if rec:
            out.append(rec)
    print(f"[ncaa-d2] merged into {len(out)} distinct players")
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
    print(f"[ncaa-d2] ingesting D2 players (ncaa.com current season, stamped {season}) ...")
    players = fetch_players(season)
    if not players:
        raise RuntimeError(
            "ncaa.com returned no D2 players (API down or layout changed). "
            "Refusing to commit empty data."
        )
    n = upsert_players(players)
    print(f"[ncaa-d2] wrote {n} D2 player rows")
    _sanity_check(season)


def ingest_many(seasons: list[int]) -> None:
    # ncaa.com only serves the current season; use the latest requested.
    ingest(max(seasons))


def _sanity_check(season: int) -> None:
    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM players WHERE division='D2' AND season=?", (season,)
        ).fetchone()["c"]
        print(f"[sanity] D2 {season}: {total} players")
        top = conn.execute(
            """SELECT name, team, pts_pg, ts_pct FROM players
               WHERE division='D2' AND season=? AND pts_pg IS NOT NULL
               ORDER BY pts_pg DESC LIMIT 5""",
            (season,),
        ).fetchall()
        for r in top:
            print(f"   {r['name']:<22} {str(r['team']):<20} {r['pts_pg']} ppg  TS%={r['ts_pct']}")


def main():
    ap = argparse.ArgumentParser(description="NCAA Division II ingestion (ncaa.com)")
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--seasons", type=int, nargs="+")
    args = ap.parse_args()
    if args.seasons:
        ingest_many(args.seasons)
    else:
        ingest(args.season)


if __name__ == "__main__":
    main()
