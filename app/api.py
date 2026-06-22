"""FastAPI routes for the NCAA player ranking search engine."""
from __future__ import annotations

import csv
import io
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.db import get_conn
from app.scoring import (
    DEFAULT_WEIGHTS,
    METRIC_BY_KEY,
    METRICS,
    composite_score,
    compute_percentile_table,
    metric_registry,
)

router = APIRouter(prefix="/api")

# Columns we expose per player row in API output.
OUTPUT_COLUMNS = [
    "id", "name", "team", "conference", "division", "class", "position",
    "season", "gp", "min_pg", "min_pct", "pts_pg", "reb_pg", "oreb_pg",
    "dreb_pg", "orb_pct", "drb_pct", "ast_pg", "ast_pct", "stl_pg", "blk_pg",
    "tov_pg", "to_pct", "blk_pct", "stl_pct", "fg_pct", "fg2_pct", "fg3_pct",
    "ft_pct", "fg3a_rate", "fta_rate", "efg_pct", "ts_pct", "usage", "ortg",
    "drtg", "bpm", "torvik_pid", "height_in", "weight_lb", "dunk_rate",
    "rim_rate", "source", "updated_at",
]

# Weight presets for the UI buttons.
PRESETS = {
    "balanced": DEFAULT_WEIGHTS,
    "scoring_big": {"pts_pg": 80, "reb_pg": 50, "oreb_pg": 30, "ts_pct": 40,
                    "fg2_pct": 25, "fta_rate": 20, "usage": 30,
                    "conf_strength": 40},
    "three_and_d": {"fg3_pct": 70, "fg3a_rate": 40, "stl_pg": 50, "blk_pg": 25,
                    "stl_pct": 30, "ts_pct": 40, "to_pct": 25, "conf_strength": 40},
    "floor_general": {"ast_pg": 80, "ast_pct": 60, "to_pct": 50, "stl_pg": 30,
                      "pts_pg": 25, "ts_pct": 25, "conf_strength": 40},
    "rim_protector": {"blk_pg": 80, "blk_pct": 60, "dreb_pg": 40, "drb_pct": 40,
                      "drtg": 40, "reb_pg": 30, "conf_strength": 40},
    "rebounder": {"reb_pg": 80, "oreb_pg": 50, "dreb_pg": 50, "orb_pct": 50,
                  "drb_pct": 50, "conf_strength": 40},
    "efficiency": {"ts_pct": 80, "efg_pct": 60, "ortg": 60, "fg_pct": 30,
                   "ft_pct": 30, "to_pct": 40, "conf_strength": 40},
}


def _latest_season(conn) -> Optional[int]:
    row = conn.execute("SELECT MAX(season) s FROM players").fetchone()
    return row["s"] if row and row["s"] is not None else None


def _conf_strength_map(conn, season: int) -> dict:
    """{(conference, division): strength_rating(0..1)} for a season."""
    rows = conn.execute(
        "SELECT conference, division, strength_rating FROM conferences WHERE season=?",
        (season,),
    ).fetchall()
    return {(r["conference"], r["division"]): r["strength_rating"] for r in rows}


def _parse_weights(qp) -> dict[str, float]:
    """Read w_<metric> query params; fall back to DEFAULT_WEIGHTS if none given."""
    weights: dict[str, float] = {}
    for k, v in qp.multi_items():
        if k.startswith("w_"):
            key = k[2:]
            if key in METRIC_BY_KEY:
                try:
                    weights[key] = float(v)
                except ValueError:
                    pass
    return weights or dict(DEFAULT_WEIGHTS)


def _load_and_score(request: Request):
    """Shared loader used by /players and /export.csv. Returns (rows, meta).

    Division I only.
    """
    qp = request.query_params
    classes = qp.getlist("class")
    conferences = qp.getlist("conference")
    position = qp.get("position")
    min_gp = _f(qp.get("min_gp"))
    min_minutes = _f(qp.get("min_minutes"))
    min_conf_strength = _f(qp.get("min_conf_strength"))
    min_height = _f(qp.get("min_height_in"))
    max_height = _f(qp.get("max_height_in"))
    null_policy = qp.get("null_policy", "exclude")
    weights = _parse_weights(qp)

    with get_conn() as conn:
        season = _i(qp.get("season")) or _latest_season(conn)
        if season is None:
            return [], {"season": None, "total": 0}
        # Default class filter = Seniors unless explicitly overridden (incl. "all").
        if not classes:
            classes = ["Sr"]
        all_classes = "all" in [c.lower() for c in classes]

        pop = conn.execute(
            "SELECT * FROM players WHERE season=? AND division='D1'",
            (season,),
        ).fetchall()
        pop = [dict(r) for r in pop]
        cs_map = _conf_strength_map(conn, season)

    # Attach conference strength (0..100) for the prescored metric.
    for p in pop:
        sr = cs_map.get((p["conference"], p["division"]))
        p["conf_strength"] = (sr * 100.0) if sr is not None else None
        p["conf_strength_rating"] = sr

    pct_table = compute_percentile_table(pop)

    # Row filters (applied AFTER percentile computation so percentiles use full pop).
    rows = []
    for p in pop:
        if not all_classes and classes and p.get("class") not in classes:
            continue
        if conferences and p.get("conference") not in conferences:
            continue
        if position and (p.get("position") or "").lower().find(position.lower()) < 0:
            continue
        if min_gp is not None and (p.get("gp") or 0) < min_gp:
            continue
        if min_minutes is not None and (p.get("min_pg") or 0) < min_minutes:
            continue
        if min_conf_strength is not None:
            sr = p.get("conf_strength_rating")
            if sr is None or sr < min_conf_strength:
                continue
        h = p.get("height_in")
        if min_height is not None and (h is None or h < min_height):
            continue
        if max_height is not None and (h is None or h > max_height):
            continue
        pcts = pct_table.get(p["id"], {})
        score = composite_score(pcts, weights, null_policy=null_policy)
        out = {c: p.get(c) for c in OUTPUT_COLUMNS}
        out["composite_score"] = score
        out["conf_strength"] = p.get("conf_strength_rating")
        out["athleticism"] = pcts.get("athleticism")
        out["percentiles"] = {k: (round(v, 1) if v is not None else None)
                              for k, v in pcts.items()}
        rows.append(out)

    # Sort: composite desc by default; missing/None values always sort last
    # (regardless of direction). Present rows are sorted, then None rows appended.
    sort = qp.get("sort", "composite_score")
    reverse = qp.get("dir", "desc").lower() != "asc"

    def norm(v):
        return v.lower() if isinstance(v, str) else v

    present = [r for r in rows if r.get(sort) is not None]
    missing = [r for r in rows if r.get(sort) is None]
    try:
        present.sort(key=lambda r: norm(r.get(sort)), reverse=reverse)
    except TypeError:
        present.sort(key=lambda r: str(r.get(sort)), reverse=reverse)
    rows = present + missing

    meta = {"season": season, "total": len(rows), "weights": weights}
    return rows, meta


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _i(v):
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


@router.get("/meta")
def meta():
    with get_conn() as conn:
        seasons = [r["season"] for r in conn.execute(
            "SELECT DISTINCT season FROM players ORDER BY season DESC")]
        divisions = [r["division"] for r in conn.execute(
            "SELECT DISTINCT division FROM players ORDER BY division")]
        classes = [r["class"] for r in conn.execute(
            "SELECT DISTINCT class FROM players WHERE class IS NOT NULL ORDER BY class")]
        positions = [r["position"] for r in conn.execute(
            "SELECT DISTINCT position FROM players WHERE position IS NOT NULL ORDER BY position")]
        confs = [dict(r) for r in conn.execute(
            """SELECT conference, division, season, strength_rating, rank
               FROM conferences ORDER BY division, strength_rating DESC""")]
    return {
        "seasons": seasons,
        "divisions": divisions,
        "classes": classes,
        "positions": positions,
        "conferences": confs,
        "metrics": metric_registry(),
        "presets": PRESETS,
        "default_weights": DEFAULT_WEIGHTS,
    }


@router.get("/players")
def players(request: Request, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500)):
    rows, meta = _load_and_score(request)
    start = (page - 1) * page_size
    page_rows = rows[start:start + page_size]
    return {
        "season": meta["season"],
        "total": meta["total"],
        "page": page,
        "page_size": page_size,
        "weights": meta.get("weights"),
        "rows": page_rows,
    }


@router.get("/career")
def career(pid: Optional[str] = Query(None), name: Optional[str] = Query(None),
           team: Optional[str] = Query(None)):
    """Year-by-year history for one player, linked by Torvik player id (preferred)
    or by name (+optional team) as a fallback."""
    career_cols = ["season", "team", "conference", "class", "position", "gp",
                   "min_pg", "pts_pg", "reb_pg", "ast_pg", "stl_pg", "blk_pg",
                   "tov_pg", "fg_pct", "fg3_pct", "ft_pct", "ts_pct", "efg_pct",
                   "usage", "ortg", "bpm", "height_in", "weight_lb"]
    with get_conn() as conn:
        if pid:
            rows = conn.execute(
                "SELECT * FROM players WHERE torvik_pid=? ORDER BY season", (pid,)
            ).fetchall()
        elif name:
            q = "SELECT * FROM players WHERE name=?"
            params: list = [name]
            if team:
                q += " AND team=?"
                params.append(team)
            q += " ORDER BY season"
            rows = conn.execute(q, params).fetchall()
        else:
            raise HTTPException(status_code=400, detail="provide pid or name")
    seasons = [{c: dict(r).get(c) for c in career_cols} for r in rows]
    return {"name": (dict(rows[0])["name"] if rows else name),
            "pid": pid, "seasons": seasons}


@router.get("/export.csv")
def export_csv(request: Request):
    rows, meta = _load_and_score(request)
    cols = ["composite_score", "name", "team", "conference", "conf_strength",
            "class", "position", "height_in", "weight_lb", "athleticism",
            "season", "gp", "min_pg",
            "pts_pg", "reb_pg", "oreb_pg", "dreb_pg", "ast_pg", "stl_pg",
            "blk_pg", "tov_pg", "fg_pct", "fg3_pct", "ft_pct", "efg_pct",
            "ts_pct", "usage", "ortg", "drtg", "bpm", "source", "updated_at"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c) for c in cols])
    buf.seek(0)
    fname = f"ncaa_ranking_{meta['season']}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/refresh")
def refresh(season: int = Query(...), token: str = Query(...),
            refresh_cache: bool = Query(False)):
    expected = os.environ.get("REFRESH_TOKEN")
    if not expected or token != expected:
        raise HTTPException(status_code=401, detail="invalid or unset REFRESH_TOKEN")
    from ingest.torvik_d1 import ingest as ingest_d1
    ingest_d1(season, refresh=refresh_cache)
    return {"status": "ok", "division": "D1", "season": season}
