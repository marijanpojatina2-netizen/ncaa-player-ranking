"""Division II ingestion from ncaa.com's official stat leaderboards.

There is NO free advanced-metric source for D2, and the complete portal
(stats.ncaa.org) is behind bot-protection that blocks servers. ncaa.com's public
stat pages ARE reachable and expose per-category leaderboards as clean JSON via
the ncaa-api proxy (henrygd). Each leaderboard is capped at the ~250-300
qualified players, so we merge MANY categories (per-game + season totals +
shooting) by player to maximise both coverage and stat completeness, deriving
per-game values from totals where a per-game board didn't rank the player, and
computing eFG%/TS% ourselves.

Limitations (honest): only players who rank in some leaderboard appear (the
statistically notable D2 players, not every roster player); advanced metrics
(BPM/usage/ORtg/DRtg/rate%) don't exist free for D2 -> NULL; current season only.

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

# category id -> {temp_key: ncaa_column}. Temp keys (all start "_") are merged per
# player; _build_record turns them into schema fields (per-game prefers the
# *_pg board, else total/G). Column names collide across boards, so map per board.
CAT_MAP: dict[int, dict[str, str]] = {
    # --- per-game leaderboards (authoritative per-game value) ---
    136: {"_ppg": "PPG", "_pts": "PTS", "_fgm": "FGM", "_3fg": "3FG", "_ft": "FT"},
    137: {"_rpg": "RPG", "_reb": "REB"},
    140: {"_apg": "APG", "_ast": "AST"},
    139: {"_spg": "STPG", "_stl": "ST"},
    138: {"_bpg": "BKPG", "_blk": "BLKS"},
    628: {"_mpg": "MPG"},
    856: {"_orpg": "RPG"},
    858: {"_drpg": "RPG"},
    # --- season-total leaderboards (breadth + per-game fallback + efficiency) ---
    600: {"_pts": "PTS", "_fgm": "FGM", "_ft": "FT"},
    601: {"_reb": "REB", "_oreb": "ORebs", "_dreb": "DRebs"},
    605: {"_ast": "AST"},
    608: {"_blk": "BLKS"},
    615: {"_stl": "ST"},
    611: {"_fgm": "FGM", "_fga": "FGA"},
    850: {"_ft": "FT", "_fta": "FTA"},
    621: {"_3fg": "3FG", "_3fga": "3FGA"},
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
    players: dict[tuple, dict] = {}
    for cid, mapping in CAT_MAP.items():
        rows = _fetch_category(div, cid)
        print(f"[ncaa-d2]   category {cid}: {len(rows)} rows ({len(players)} players so far)")
        for row in rows:
            name = (row.get("Name") or "").strip()
            team = (row.get("Team") or "").strip()
            if not name or not team:
                continue
            p = players.setdefault((name.lower(), team.lower()), {})
            for k in IDENTITY:
                if row.get(k) and not p.get(k):
                    p[k] = row[k]
            for field, col in mapping.items():
                v = row.get(col)
                if v not in (None, "") and not p.get(field):
                    p[field] = v
    return players


def _pg(p: dict, total_key: str, gp: Optional[float]) -> Optional[float]:
    t = to_float(p.get(total_key))
    return round(t / gp, 4) if (t is not None and gp) else None


def _ratio(num, den) -> Optional[float]:
    n, d = to_float(num), to_float(den)
    return round(n / d, 4) if (n is not None and d) else None


def _build_record(p: dict, season: int) -> Optional[dict]:
    name = (p.get("Name") or "").strip()
    team = (p.get("Team") or "").strip()
    if not name or not team:
        return None
    gp = to_float(p.get("G"))
    # Per-game: prefer the per-game board's value, else derive from the total.
    pts_pg = to_float(p.get("_ppg")) or _pg(p, "_pts", gp)
    reb_pg = to_float(p.get("_rpg")) or _pg(p, "_reb", gp)
    ast_pg = to_float(p.get("_apg")) or _pg(p, "_ast", gp)
    stl_pg = to_float(p.get("_spg")) or _pg(p, "_stl", gp)
    blk_pg = to_float(p.get("_bpg")) or _pg(p, "_blk", gp)
    oreb_pg = to_float(p.get("_orpg")) or _pg(p, "_oreb", gp)
    dreb_pg = to_float(p.get("_drpg")) or _pg(p, "_dreb", gp)
    min_pg = to_float(p.get("_mpg"))
    tov_pg = _pg(p, "_to", gp)
    # Shooting % (fractions) from totals.
    fg_pct = _ratio(p.get("_fgm"), p.get("_fga"))
    fg3_pct = _ratio(p.get("_3fg"), p.get("_3fga"))
    ft_pct = _ratio(p.get("_ft"), p.get("_fta"))
    # Efficiency (0-100) from totals when the inputs are present.
    fgm, fga = to_float(p.get("_fgm")), to_float(p.get("_fga"))
    fta, pts, tfg = to_float(p.get("_fta")), to_float(p.get("_pts")), to_float(p.get("_3fg"))
    efg = round((fgm + 0.5 * tfg) / fga * 100, 2) if (fga and fgm is not None and tfg is not None) else None
    ts = None
    if fga and pts is not None and fta is not None:
        tsa = fga + 0.44 * fta
        if tsa > 0:
            ts = round(pts / (2 * tsa) * 100, 2)
    return {
        "name": name, "team": team, "conference": None, "division": "D2",
        "class": normalize_class(p.get("Cl")),
        "position": (p.get("Position") or "").strip() or None,
        "season": season, "gp": gp,
        "min_pg": min_pg, "min_pct": None,
        "pts_pg": pts_pg, "reb_pg": reb_pg, "oreb_pg": oreb_pg, "dreb_pg": dreb_pg,
        "orb_pct": None, "drb_pct": None,
        "ast_pg": ast_pg, "ast_pct": None, "stl_pg": stl_pg, "blk_pg": blk_pg,
        "tov_pg": tov_pg, "to_pct": None, "blk_pct": None, "stl_pct": None,
        "fg_pct": fg_pct, "fg2_pct": None, "fg3_pct": fg3_pct, "ft_pct": ft_pct,
        "fg3a_rate": None, "fta_rate": None, "efg_pct": efg, "ts_pct": ts,
        "usage": None, "ortg": None, "drtg": None, "bpm": None,
        "torvik_pid": _slug(name),
        "height_in": parse_height_to_inches(p.get("Height")),
        "weight_lb": None, "dunk_rate": None, "rim_rate": None,
        "source": "ncaa.com",
    }


def fetch_players(season: int) -> list[dict]:
    merged = _merge("d2")
    out = [r for r in (_build_record(p, season) for p in merged.values()) if r]
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
    ingest(max(seasons))   # ncaa.com only serves the current season


def _sanity_check(season: int) -> None:
    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM players WHERE division='D2' AND season=?", (season,)
        ).fetchone()["c"]
        srs = conn.execute(
            "SELECT COUNT(*) c FROM players WHERE division='D2' AND season=? AND class='Sr'", (season,)
        ).fetchone()["c"]
        print(f"[sanity] D2 {season}: {total} players ({srs} seniors)")
        top = conn.execute(
            """SELECT name, team, pts_pg, ts_pct FROM players
               WHERE division='D2' AND season=? AND pts_pg IS NOT NULL
               ORDER BY pts_pg DESC LIMIT 5""", (season,),
        ).fetchall()
        for r in top:
            print(f"   {r['name']:<22} {str(r['team']):<20} {r['pts_pg']} ppg  TS%={r['ts_pct']}")


def main():
    ap = argparse.ArgumentParser(description="NCAA Division II ingestion (ncaa.com)")
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--seasons", type=int, nargs="+")
    args = ap.parse_args()
    ingest_many(args.seasons) if args.seasons else ingest(args.season)


if __name__ == "__main__":
    main()
