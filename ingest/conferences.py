"""Build/refresh the `conferences` table.

D1 strength comes from Torvik team ratings aggregated per conference (see
torvik_d1.build_conferences which calls upsert_conferences here). strength_rating
is min-max normalized to 0..1 within (division, season).
"""
from __future__ import annotations

import argparse

from app.db import get_conn
from ingest.common import minmax_normalize, to_float, utcnow_iso


def upsert_conferences(rows: list[dict], division: str, season: int, source: str) -> int:
    """rows: [{conference, raw_rating, rank?}]. Normalizes raw_rating -> 0..1 and writes."""
    if not rows:
        return 0
    raws = [to_float(r.get("raw_rating")) for r in rows]
    norm = minmax_normalize(raws)
    # Rank by raw_rating desc if not provided.
    order = sorted(range(len(rows)), key=lambda i: (raws[i] if raws[i] is not None else -1e9), reverse=True)
    rank_by_idx = {idx: pos + 1 for pos, idx in enumerate(order)}

    now = utcnow_iso()
    written = 0
    with get_conn() as conn:
        for i, r in enumerate(rows):
            conf = (r.get("conference") or "").strip()
            if not conf:
                continue
            rank = r.get("rank") or rank_by_idx.get(i)
            conn.execute(
                """
                INSERT INTO conferences
                    (conference, division, season, strength_rating, rank, raw_rating, source, updated_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(conference, division, season) DO UPDATE SET
                    strength_rating=excluded.strength_rating,
                    rank=excluded.rank,
                    raw_rating=excluded.raw_rating,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (conf, division, season, norm[i], rank, raws[i], source, now),
            )
            written += 1
        conn.commit()
    return written


def main():
    ap = argparse.ArgumentParser(description="Build D1 conferences table (from Torvik)")
    ap.add_argument("--division", choices=["D1"], default="D1")
    ap.add_argument("--season", type=int, required=True)
    args = ap.parse_args()
    from ingest.torvik_d1 import build_conferences as build_d1
    build_d1(args.season)


if __name__ == "__main__":
    main()
