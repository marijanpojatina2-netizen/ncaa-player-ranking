"""Seed the database with CLEARLY-LABELED SYNTHETIC demo data.

This exists ONLY so the scoring/API/frontend can be exercised end-to-end without
live network access to Torvik. Every row is tagged source='DEMO_SYNTHETIC' and
the player names are obviously fake ("D1 Demo Player 7"). These are NOT real
statistics and must never be presented as such.

Usage:
    python -m ingest.seed_demo --season 2025
    python -m ingest.seed_demo --season 2025 --clear   # wipe demo rows first
"""
from __future__ import annotations

import argparse
import random

from app.db import get_conn, init_db
from ingest.conferences import upsert_conferences
from ingest.common import utcnow_iso

D1_CONFS = ["Big Ten", "SEC", "Big 12", "ACC", "Big East", "Mountain West",
            "WCC", "American", "Atlantic 10", "MVC", "Sun Belt", "MAC",
            "Southern", "Patriot", "MEAC"]
CLASSES = ["Fr", "So", "Jr", "Sr"]
POSITIONS = ["Pure PG", "Combo G", "Wing G", "Wing F", "Stretch 4", "C"]

PLAYER_FIELDS = [
    "name", "team", "conference", "division", "class", "position", "season",
    "gp", "min_pg", "min_pct", "pts_pg", "reb_pg", "oreb_pg", "dreb_pg",
    "orb_pct", "drb_pct", "ast_pg", "ast_pct", "stl_pg", "blk_pg", "tov_pg",
    "to_pct", "blk_pct", "stl_pct", "fg_pct", "fg2_pct", "fg3_pct", "ft_pct",
    "fg3a_rate", "fta_rate", "efg_pct", "ts_pct", "usage", "ortg", "drtg",
    "bpm", "torvik_pid", "height_in", "weight_lb", "dunk_rate", "rim_rate",
    "source", "updated_at",
]
_CLASS_ORDER = ["Fr", "So", "Jr", "Sr"]


def _player_identity(i: int, rng: random.Random) -> dict:
    """Static attributes that persist across a player's seasons."""
    pos = rng.choice(POSITIONS)
    big = pos in ("Stretch 4", "C", "Wing F")
    height = rng.randint(78, 84) if big else rng.randint(72, 79)
    return {
        "pid": f"demo{i}",
        "name": f"D1 Demo Player {i}",
        "team": f"D1 Demo U {i % 40}",
        "conference": rng.choice(D1_CONFS),
        "position": pos,
        "big": big,
        "guard": pos in ("Pure PG", "Combo G", "Wing G"),
        "height_in": float(height),
        "weight_lb": float(height * rng.uniform(2.5, 3.0)),
        "skill": rng.uniform(0.3, 1.0),  # latent ability, drives stats
    }


def _season_row(ident: dict, season: int, klass: str, year_idx: int, rng: random.Random) -> dict:
    """One season for a player; stats scale with skill and improve a bit by year."""
    big, guard = ident["big"], ident["guard"]
    growth = 1.0 + 0.08 * year_idx                  # gentle year-over-year growth
    s = ident["skill"] * growth
    pts = round(rng.uniform(3, 22) * s, 1)
    reb = round((rng.uniform(6, 11) if big else rng.uniform(2, 5)) * s, 1)
    oreb = round(reb * rng.uniform(0.25, 0.4), 1)
    return {
        "name": ident["name"], "team": ident["team"], "conference": ident["conference"],
        "division": "D1", "class": klass, "position": ident["position"],
        "torvik_pid": ident["pid"], "height_in": ident["height_in"],
        "weight_lb": round(ident["weight_lb"], 0), "season": season,
        "gp": rng.randint(20, 34), "min_pg": round(min(38, rng.uniform(10, 30) * growth), 1),
        "pts_pg": pts, "reb_pg": reb, "oreb_pg": oreb, "dreb_pg": round(reb - oreb, 1),
        "ast_pg": round((rng.uniform(2, 6) if guard else rng.uniform(0.3, 2.0)) * s, 1),
        "stl_pg": round(rng.uniform(0.3, 2.0) * s, 1),
        "blk_pg": round((rng.uniform(0.7, 2.3) if big else rng.uniform(0.0, 0.5)) * s, 1),
        "tov_pg": round(rng.uniform(0.8, 3.0), 1),
        "fg_pct": round(rng.uniform(40, 58), 1), "fg2_pct": round(rng.uniform(45, 62), 1),
        "fg3_pct": round(rng.uniform(28, 42), 1), "ft_pct": round(rng.uniform(60, 90), 1),
        "min_pct": round(rng.uniform(30, 90), 1),
        "orb_pct": round((rng.uniform(6, 14) if big else rng.uniform(1, 5)), 1),
        "drb_pct": round(rng.uniform(8, 28), 1), "ast_pct": round(rng.uniform(5, 35), 1),
        "to_pct": round(rng.uniform(8, 24), 1),
        "blk_pct": round((rng.uniform(3, 9) if big else rng.uniform(0.2, 2)), 1),
        "stl_pct": round(rng.uniform(0.5, 4), 1), "fg3a_rate": round(rng.uniform(10, 60), 1),
        "fta_rate": round(rng.uniform(15, 60), 1), "efg_pct": round(rng.uniform(45, 62), 1),
        "ts_pct": round(rng.uniform(48, 65), 1), "usage": round(rng.uniform(12, 32), 1),
        "ortg": round(rng.uniform(92, 124), 1), "drtg": round(rng.uniform(92, 112), 1),
        "bpm": round(rng.uniform(-4, 11) * s, 1),
        "dunk_rate": round((rng.uniform(0.3, 2.5) if big else rng.uniform(0, 0.6)) * s, 2),
        "rim_rate": round((rng.uniform(35, 65) if big else rng.uniform(10, 40)), 1),
        "source": "DEMO_SYNTHETIC",
    }


def seed(last_season: int, n_players: int = 350, seasons_back: int = 4,
         clear: bool = False, seed_val: int = 42) -> None:
    init_db()
    rng = random.Random(seed_val)
    if clear:
        with get_conn() as conn:
            conn.execute("DELETE FROM players WHERE source='DEMO_SYNTHETIC'")
            conn.commit()

    seasons = list(range(last_season - seasons_back + 1, last_season + 1))
    # Build multi-season careers so seniors have real year-by-year history.
    records: list[dict] = []
    for i in range(n_players):
        ident = _player_identity(i, rng)
        # Class in the most recent season (mix of classes, weighted toward upper).
        cur_idx = rng.choices([0, 1, 2, 3], weights=[2, 2, 3, 4])[0]  # Fr/So/Jr/Sr
        length = cur_idx + 1  # years of history ending in last_season
        for yi in range(length):
            season = last_season - (length - 1 - yi)
            if season < seasons[0]:
                continue
            klass = _CLASS_ORDER[yi]  # Fr -> ... -> current class
            records.append(_season_row(ident, season, klass, yi, rng))

    now = utcnow_iso()
    placeholders = ",".join("?" for _ in PLAYER_FIELDS)
    update_cols = ",".join(f"{c}=excluded.{c}" for c in PLAYER_FIELDS
                           if c not in ("name", "team", "season", "division"))
    sql = (f"INSERT INTO players ({','.join(PLAYER_FIELDS)}) VALUES ({placeholders}) "
           f"ON CONFLICT(name, team, season, division) DO UPDATE SET {update_cols}")
    with get_conn() as conn:
        for rec in records:
            conn.execute(sql, [rec.get(f) if f != "updated_at" else now for f in PLAYER_FIELDS])
        conn.commit()

    # Conference strength per season: synthetic raw ratings for D1.
    for season in seasons:
        d1_rows = [{"conference": c, "raw_rating": rng.uniform(0.3, 0.95)} for c in D1_CONFS]
        upsert_conferences(d1_rows, "D1", season, "DEMO_SYNTHETIC")

    print(f"[seed] inserted {len(records)} synthetic player-seasons "
          f"({n_players} careers) across seasons {seasons}")
    print("[seed] NOTE: source='DEMO_SYNTHETIC' — these are NOT real stats.")


def main():
    ap = argparse.ArgumentParser(description="Seed synthetic demo data")
    ap.add_argument("--season", type=int, required=True, help="most recent season")
    ap.add_argument("--seasons-back", type=int, default=4)
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--n-players", type=int, default=350)
    args = ap.parse_args()
    seed(args.season, args.n_players, args.seasons_back, clear=args.clear)


if __name__ == "__main__":
    main()
