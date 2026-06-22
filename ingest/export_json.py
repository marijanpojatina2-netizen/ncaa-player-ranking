"""Export the concluded database to a portable JSON file.

Produces web/data.json containing every D1 player-season plus conference
strengths and the metric/preset metadata. This is what a static (server-less)
viewer would load to do percentile + composite scoring entirely in the browser,
and it doubles as a human-portable snapshot of the scraped database.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.db import get_conn
from app.scoring import DEFAULT_WEIGHTS, metric_registry

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT_ROOT / "web" / "data.json"


def export(out_path: Path = DEFAULT_OUT) -> int:
    with get_conn() as conn:
        players = [dict(r) for r in conn.execute(
            "SELECT * FROM players WHERE division='D1' ORDER BY season DESC, name")]
        confs = [dict(r) for r in conn.execute(
            "SELECT conference, division, season, strength_rating, rank FROM conferences")]
    payload = {
        "generated_from": "barttorvik + espn rosters",
        "players": players,
        "conferences": confs,
        "metrics": metric_registry(),
        "default_weights": DEFAULT_WEIGHTS,
        "n_players": len(players),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"[export] wrote {len(players)} player-seasons to {out_path} "
          f"({out_path.stat().st_size // 1024} KB)")
    return len(players)


def main():
    ap = argparse.ArgumentParser(description="Export concluded DB to JSON")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    export(args.out)


if __name__ == "__main__":
    main()
