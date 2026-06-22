"""Division I ingestion from Bart Torvik (barttorvik.com).

Sources
-------
* Player-season advanced stats:  getadvstats.php?year=YYYY&csv=1
* Team ratings (for conf strength): trank.php?year=YYYY&csv=1

IMPORTANT — VERIFY EACH SEASON
------------------------------
Torvik does not publish a stable, documented API. The CSV endpoints return rows
WITHOUT a header line, so we map columns POSITIONALLY. The mapping below
(TORVIK_PLAYER_COLUMNS / TORVIK_TEAM_COLUMNS) reflects the observed layout but
*may shift between seasons or site updates*. Before trusting a fresh ingest:

    python -m ingest.torvik_d1 --season 2025 --inspect

...which prints the first data row with column indices so you can confirm /
correct the mapping. You can override the mapping without editing code by
dropping a JSON file at data/torvik_columns.json, e.g.:

    {"player": {"pts_pg": 60, "reb_pg": 58}, "team": {"conference": 2}}

Unknown / unavailable fields are stored as NULL (we never fabricate stats).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path
from typing import Optional

from app.db import get_conn, init_db
from ingest.common import (
    fetch, normalize_class, parse_height_to_inches, to_float, utcnow_iso,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLUMN_OVERRIDE = PROJECT_ROOT / "data" / "torvik_columns.json"

PLAYER_URL = "https://barttorvik.com/getadvstats.php"
TEAM_URL = "https://barttorvik.com/trank.php"

# --- Positional column maps (0-indexed). VERIFY with --inspect. --------------
# Confident early columns of getadvstats.php?csv=1. Per-game counting stats live
# in the tail of the row and are the most likely to drift -> verify those.
TORVIK_PLAYER_COLUMNS: dict[str, int] = {
    "name": 0,
    "team": 1,
    "conference": 2,
    "gp": 3,
    "min_pct": 4,
    "ortg": 5,
    "usage": 6,
    "efg_pct": 7,
    "ts_pct": 8,
    "orb_pct": 9,
    "drb_pct": 10,
    "ast_pct": 11,
    "to_pct": 12,
    "ft_pct": 15,
    "fg2_pct": 18,
    "fg3_pct": 21,
    "blk_pct": 22,
    "stl_pct": 23,
    "fta_rate": 24,   # ftr
    "class": 25,      # yr
    "height": 26,     # ht (e.g. "6-5") -> parsed to inches
    "torvik_pid": 32, # pid: stable player id used to link seasons (VERIFY)
    "position": 33,   # type (e.g. "Pure PG", "Wing G") -> stored as-is
}
# Tail per-game/box columns (HIGH drift risk). Left empty by default; fill in
# after --inspect, or via data/torvik_columns.json. When absent -> NULL.
TORVIK_PLAYER_TAIL: dict[str, Optional[int]] = {
    "pts_pg": None,
    "reb_pg": None,
    "oreb_pg": None,
    "dreb_pg": None,
    "ast_pg": None,
    "stl_pg": None,
    "blk_pg": None,
    "tov_pg": None,
    "min_pg": None,
    "drtg": None,
    "bpm": None,
    "fg3a_rate": None,
    "dunk_rate": None,   # dunks/game (athleticism proxy input)
    "rim_rate": None,    # rim attempt share (proxy input)
}

TORVIK_TEAM_COLUMNS: dict[str, int] = {
    "rank": 0,
    "team": 1,
    "conference": 2,
    "barthag": 18,   # adjusted win prob vs avg team; verify with --inspect
}


def _apply_override():
    if not COLUMN_OVERRIDE.exists():
        return
    try:
        data = json.loads(COLUMN_OVERRIDE.read_text())
    except Exception as exc:
        print(f"[torvik] could not parse {COLUMN_OVERRIDE}: {exc}")
        return
    TORVIK_PLAYER_COLUMNS.update(data.get("player", {}))
    TORVIK_PLAYER_TAIL.update(data.get("player_tail", {}))
    TORVIK_TEAM_COLUMNS.update(data.get("team", {}))
    print(f"[torvik] applied column overrides from {COLUMN_OVERRIDE.name}")


def _parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def _get(row: list[str], idx: Optional[int]):
    if idx is None or idx < 0 or idx >= len(row):
        return None
    return row[idx]


def inspect(season: int, refresh: bool, what: str = "player") -> None:
    url = PLAYER_URL if what == "player" else TEAM_URL
    text = fetch(url, {"year": season, "csv": 1}, refresh=refresh)
    rows = _parse_csv(text)
    print(f"[inspect] {url} season={season}: {len(rows)} rows")
    if rows:
        sample = rows[0]
        print(f"[inspect] first row has {len(sample)} columns:")
        for i, val in enumerate(sample):
            print(f"  [{i:>3}] {val!r}")


def fetch_players(season: int, refresh: bool) -> list[dict]:
    _apply_override()
    text = fetch(PLAYER_URL, {"year": season, "csv": 1}, refresh=refresh)
    rows = _parse_csv(text)
    out: list[dict] = []
    cols = {**TORVIK_PLAYER_COLUMNS}
    tail = {**TORVIK_PLAYER_TAIL}
    for row in rows:
        if not row or len(row) < 4:
            continue
        name = (_get(row, cols["name"]) or "").strip()
        if not name or name.lower() in ("player_name", "player", "name"):
            continue  # skip stray header
        rec = {
            "name": name,
            "team": (_get(row, cols["team"]) or "").strip() or None,
            "conference": (_get(row, cols["conference"]) or "").strip() or None,
            "division": "D1",
            "class": normalize_class(_get(row, cols.get("class"))),
            "position": (_get(row, cols.get("position")) or "").strip() or None,
            "torvik_pid": (_get(row, cols.get("torvik_pid")) or "").strip() or None,
            "height_in": parse_height_to_inches(_get(row, cols.get("height"))),
            "season": season,
            "gp": to_float(_get(row, cols.get("gp"))),
            "min_pct": to_float(_get(row, cols.get("min_pct"))),
            "ortg": to_float(_get(row, cols.get("ortg"))),
            "usage": to_float(_get(row, cols.get("usage"))),
            "efg_pct": to_float(_get(row, cols.get("efg_pct"))),
            "ts_pct": to_float(_get(row, cols.get("ts_pct"))),
            "orb_pct": to_float(_get(row, cols.get("orb_pct"))),
            "drb_pct": to_float(_get(row, cols.get("drb_pct"))),
            "ast_pct": to_float(_get(row, cols.get("ast_pct"))),
            "to_pct": to_float(_get(row, cols.get("to_pct"))),
            "ft_pct": to_float(_get(row, cols.get("ft_pct"))),
            "fg2_pct": to_float(_get(row, cols.get("fg2_pct"))),
            "fg3_pct": to_float(_get(row, cols.get("fg3_pct"))),
            "blk_pct": to_float(_get(row, cols.get("blk_pct"))),
            "stl_pct": to_float(_get(row, cols.get("stl_pct"))),
            "fta_rate": to_float(_get(row, cols.get("fta_rate"))),
            "source": "barttorvik",
        }
        # Tail (high drift) columns -> NULL unless mapped.
        for key, idx in tail.items():
            rec[key] = to_float(_get(row, idx)) if idx is not None else None
        out.append(rec)
    return out


def fetch_team_ratings(season: int, refresh: bool) -> list[dict]:
    _apply_override()
    text = fetch(TEAM_URL, {"year": season, "csv": 1}, refresh=refresh)
    rows = _parse_csv(text)
    cols = {**TORVIK_TEAM_COLUMNS}
    out: list[dict] = []
    for row in rows:
        if not row or len(row) < 3:
            continue
        team = (_get(row, cols["team"]) or "").strip()
        if not team or team.lower() == "team":
            continue
        out.append({
            "team": team,
            "conference": (_get(row, cols["conference"]) or "").strip() or None,
            "barthag": to_float(_get(row, cols.get("barthag"))),
        })
    return out


PLAYER_FIELDS = [
    "name", "team", "conference", "division", "class", "position", "season",
    "gp", "min_pg", "min_pct", "pts_pg", "reb_pg", "oreb_pg", "dreb_pg",
    "orb_pct", "drb_pct", "ast_pg", "ast_pct", "stl_pg", "blk_pg", "tov_pg",
    "to_pct", "blk_pct", "stl_pct", "fg_pct", "fg2_pct", "fg3_pct", "ft_pct",
    "fg3a_rate", "fta_rate", "efg_pct", "ts_pct", "usage", "ortg", "drtg",
    "bpm", "torvik_pid", "height_in", "weight_lb", "dunk_rate", "rim_rate",
    "source", "updated_at",
]


def upsert_players(records: list[dict]) -> int:
    if not records:
        return 0
    now = utcnow_iso()
    placeholders = ",".join("?" for _ in PLAYER_FIELDS)
    # Torvik does not provide weight; never overwrite the ESPN-sourced value.
    _no_update = ("name", "team", "season", "division", "weight_lb")
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
            values = [rec.get(f) for f in PLAYER_FIELDS]
            conn.execute(sql, values)
            written += 1
        conn.commit()
    return written


def build_conferences(season: int, refresh: bool = False) -> int:
    """Aggregate Torvik team barthag into per-conference average -> raw strength."""
    from ingest.conferences import upsert_conferences
    teams = fetch_team_ratings(season, refresh)
    agg: dict[str, list[float]] = {}
    for t in teams:
        conf = t.get("conference")
        bh = t.get("barthag")
        if conf and bh is not None:
            agg.setdefault(conf, []).append(bh)
    rows = [{"conference": c, "raw_rating": sum(v) / len(v)} for c, v in agg.items()]
    n = upsert_conferences(rows, division="D1", season=season, source="barttorvik_team_agg")
    print(f"[torvik] D1 season {season}: wrote {n} conference strength rows")
    return n


def ingest(season: int, refresh: bool = False) -> None:
    init_db()
    print(f"[torvik] ingesting D1 players for {season} (refresh={refresh}) ...")
    players = fetch_players(season, refresh)
    n = upsert_players(players)
    print(f"[torvik] wrote {n} D1 player rows")
    build_conferences(season, refresh)
    _sanity_check(season)


def ingest_many(seasons: list[int], refresh: bool = False) -> None:
    """Ingest several seasons in one run (for building multi-year career history)."""
    for s in seasons:
        ingest(s, refresh)


def _sanity_check(season: int) -> None:
    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM players WHERE division='D1' AND season=?", (season,)
        ).fetchone()["c"]
        seniors = conn.execute(
            "SELECT COUNT(*) c FROM players WHERE division='D1' AND season=? AND class='Sr'", (season,)
        ).fetchone()["c"]
        print(f"[sanity] D1 {season}: {total} players, {seniors} seniors")
        top = conn.execute(
            """SELECT name, team, conference, pts_pg FROM players
               WHERE division='D1' AND season=? AND class='Sr'
               ORDER BY pts_pg DESC NULLS LAST LIMIT 5""",
            (season,),
        ).fetchall()
        if top and top[0]["pts_pg"] is not None:
            print("[sanity] top senior scorers:")
            for r in top:
                print(f"   {r['name']:<24} {r['team']:<18} {r['conference']:<8} {r['pts_pg']} ppg")
        else:
            print("[sanity] pts_pg not populated (tail columns unmapped — run --inspect)")


def main():
    ap = argparse.ArgumentParser(description="Torvik D1 ingestion")
    ap.add_argument("--season", type=int, help="single season, e.g. 2026")
    ap.add_argument("--seasons", type=int, nargs="+",
                    help="multiple seasons for career history, e.g. --seasons 2023 2024 2025 2026")
    ap.add_argument("--refresh", action="store_true", help="bypass cache")
    ap.add_argument("--inspect", action="store_true", help="print indexed columns and exit")
    ap.add_argument("--inspect-what", choices=["player", "team"], default="player")
    args = ap.parse_args()
    if args.inspect:
        inspect(args.season or (args.seasons or [2026])[-1], args.refresh, args.inspect_what)
        return
    if args.seasons:
        ingest_many(args.seasons, args.refresh)
    elif args.season:
        ingest(args.season, args.refresh)
    else:
        ap.error("provide --season or --seasons")


if __name__ == "__main__":
    main()
