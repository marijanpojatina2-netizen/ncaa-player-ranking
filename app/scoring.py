"""Percentile normalization + weighted composite scoring.

Design
------
* Each numeric metric is converted to a PERCENTILE (0..100) computed WITHIN a
  (division, season) population. Percentiles are stable regardless of the user's
  row filters (we percentile against the full division population, not the
  filtered subset) so a player's standing reflects all peers.
* The composite score is a weighted average of the player's percentile on each
  metric the user gave a positive weight to.
* "Lower is better" metrics (turnovers, TO%, DRtg) are inverted: 100 - pct.
* Conference strength is just another weightable metric. Its underlying value is
  already normalized 0..1 within division, so we pre-score it as value*100
  instead of re-percentiling (flag: prescored).
* NULL handling (configurable): default "exclude" drops a metric from THAT
  player's weighted average and renormalizes the remaining weights. "median"
  treats a missing metric as the 50th percentile.

This build is Division I only. Percentiles are still grouped by (division,
season) so the code remains correct if other divisions are added later.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    higher_is_better: bool = True
    prescored: bool = False  # already on a 0..100 scale; skip percentiling
    derived: bool = False     # computed from other metrics, not a raw player column


# Box-stat inputs to the athleticism proxy index. All "higher = more athletic".
# Players with NULL inputs simply average over whatever is present (so the index
# still works when dunk/rim rates are unmapped and only blk/stl/ORB% exist).
ATHLETICISM_COMPONENTS = ["blk_pct", "stl_pct", "orb_pct", "dunk_rate", "rim_rate"]


# The full set of rankable metrics. `key` matches a column on players (except
# conf_strength which the API attaches from the conferences table).
METRICS: list[Metric] = [
    Metric("pts_pg", "Points / game"),
    Metric("reb_pg", "Rebounds / game"),
    Metric("oreb_pg", "Off. rebounds / game"),
    Metric("dreb_pg", "Def. rebounds / game"),
    Metric("orb_pct", "Offensive rebound %"),
    Metric("drb_pct", "Defensive rebound %"),
    Metric("ast_pg", "Assists / game"),
    Metric("ast_pct", "Assist %"),
    Metric("stl_pg", "Steals / game"),
    Metric("blk_pg", "Blocks / game"),
    Metric("stl_pct", "Steal %"),
    Metric("blk_pct", "Block %"),
    Metric("tov_pg", "Turnovers / game", higher_is_better=False),
    Metric("to_pct", "Turnover %", higher_is_better=False),
    Metric("fg_pct", "FG%"),
    Metric("fg2_pct", "2P%"),
    Metric("fg3_pct", "3P%"),
    Metric("ft_pct", "FT%"),
    Metric("fg3a_rate", "3PA rate"),
    Metric("fta_rate", "FT rate"),
    Metric("efg_pct", "eFG%"),
    Metric("ts_pct", "TS%"),
    Metric("usage", "Usage %"),
    Metric("ortg", "Offensive rating"),
    Metric("drtg", "Defensive rating", higher_is_better=False),
    Metric("bpm", "BPM / box +/-"),
    Metric("dunk_rate", "Dunks / game"),
    Metric("rim_rate", "Rim attempt rate"),
    Metric("athleticism", "Athleticism index (proxy)", prescored=True, derived=True),
    Metric("conf_strength", "Conference strength", prescored=True),
]

METRIC_BY_KEY = {m.key: m for m in METRICS}


def metric_registry() -> list[dict]:
    return [
        {"key": m.key, "label": m.label, "higher_is_better": m.higher_is_better}
        for m in METRICS
    ]


def _percentiles(values: list[Optional[float]]) -> list[Optional[float]]:
    """Midrank percentile (0..100) for non-null values; None stays None.

    pct(x) = 100 * (#{v < x} + 0.5 * #{v == x}) / N
    """
    present = [v for v in values if v is not None]
    n = len(present)
    if n == 0:
        return [None for _ in values]
    if n == 1:
        return [50.0 if v is not None else None for v in values]
    s = sorted(present)
    # For ties, precompute counts via binary search.
    import bisect
    out: list[Optional[float]] = []
    for v in values:
        if v is None:
            out.append(None)
            continue
        lo = bisect.bisect_left(s, v)
        hi = bisect.bisect_right(s, v)
        less = lo
        equal = hi - lo
        out.append(100.0 * (less + 0.5 * equal) / n)
    return out


def compute_percentile_table(players: Iterable[dict]) -> dict:
    """Return {player_id: {metric_key: percentile_or_None}} computed per
    (division, season) group. Inversion for lower-is-better metrics is applied
    here so downstream code always sees "higher percentile = better".

    `players` rows must contain `id`, `division`, `season`, and metric columns.
    For conf_strength the row should carry a `conf_strength` value in 0..100
    (prescored) — see API which derives it from strength_rating*100.
    """
    players = list(players)
    groups: dict[tuple, list[dict]] = {}
    for p in players:
        groups.setdefault((p["division"], p["season"]), []).append(p)

    result: dict = {}
    for _, group in groups.items():
        ids = [p["id"] for p in group]
        for m in METRICS:
            if m.derived:
                continue  # handled after raw metrics are percentiled
            vals = [_safe_num(p.get(m.key)) for p in group]
            if m.prescored:
                pcts = [v for v in vals]  # already 0..100
            else:
                pcts = _percentiles(vals)
                if not m.higher_is_better:
                    pcts = [None if v is None else 100.0 - v for v in pcts]
            for pid, pct in zip(ids, pcts):
                result.setdefault(pid, {})[m.key] = pct

        # Derived: athleticism index = mean of available component percentiles.
        for pid in ids:
            comps = [result[pid].get(k) for k in ATHLETICISM_COMPONENTS]
            present = [c for c in comps if c is not None]
            result[pid]["athleticism"] = round(sum(present) / len(present), 2) if present else None
    return result


def _safe_num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def composite_score(
    pct_row: dict,
    weights: dict[str, float],
    *,
    null_policy: str = "exclude",
) -> Optional[float]:
    """Weighted average of percentiles for metrics with weight>0.

    Returns a 0..100 score, or None if no weighted metric contributed.
    """
    num = 0.0
    den = 0.0
    for key, w in weights.items():
        if not w or w <= 0:
            continue
        if key not in METRIC_BY_KEY:
            continue
        pct = pct_row.get(key)
        if pct is None:
            if null_policy == "median":
                pct = 50.0
            else:  # exclude
                continue
        num += w * pct
        den += w
    if den == 0:
        return None
    return round(num / den, 2)


# Sensible starter preset (used as the default landing weights): balanced
# production + efficiency + conference strength.
DEFAULT_WEIGHTS = {
    "pts_pg": 60,
    "reb_pg": 30,
    "ast_pg": 30,
    "ts_pct": 40,
    "efg_pct": 20,
    "stl_pg": 15,
    "blk_pg": 15,
    "to_pct": 20,        # inverted internally
    "ortg": 25,
    "conf_strength": 50,
}
