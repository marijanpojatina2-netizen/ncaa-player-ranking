"""Unit checks for percentile normalization and composite scoring."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.scoring import (  # noqa: E402
    composite_score,
    compute_percentile_table,
    _percentiles,
)


def test_percentiles_basic_ordering():
    vals = [10.0, 20.0, 30.0, 40.0]
    pcts = _percentiles(vals)
    assert pcts[0] < pcts[1] < pcts[2] < pcts[3]
    # midrank: smallest of 4 distinct -> 12.5, largest -> 87.5
    assert abs(pcts[0] - 12.5) < 1e-6
    assert abs(pcts[3] - 87.5) < 1e-6


def test_percentiles_handles_none():
    vals = [None, 5.0, None, 15.0]
    pcts = _percentiles(vals)
    assert pcts[0] is None and pcts[2] is None
    assert pcts[1] < pcts[3]


def test_percentiles_ties():
    vals = [5.0, 5.0, 5.0]
    pcts = _percentiles(vals)
    assert all(abs(p - 50.0) < 1e-6 for p in pcts)


def _mk(pid, division, pts, conf_strength=None):
    return {"id": pid, "division": division, "season": 2025,
            "pts_pg": pts, "conf_strength": conf_strength}


def test_compute_percentile_table_per_division():
    players = [
        _mk(1, "D1", 10), _mk(2, "D1", 20), _mk(3, "D1", 30),
        _mk(4, "D2", 5), _mk(5, "D2", 25),
    ]
    table = compute_percentile_table(players)
    # D1 top scorer (id 3) should beat D1 low scorer (id 1)
    assert table[3]["pts_pg"] > table[1]["pts_pg"]
    # D2 percentiles computed independently: id5 > id4
    assert table[5]["pts_pg"] > table[4]["pts_pg"]


def test_conf_strength_prescored():
    players = [_mk(1, "D1", 10, conf_strength=80.0), _mk(2, "D1", 20, conf_strength=20.0)]
    table = compute_percentile_table(players)
    # prescored: passes through as-is (value*100 already done upstream)
    assert table[1]["conf_strength"] == 80.0
    assert table[2]["conf_strength"] == 20.0


def test_inverted_metric_turnovers():
    players = [
        {"id": 1, "division": "D1", "season": 2025, "to_pct": 10.0},
        {"id": 2, "division": "D1", "season": 2025, "to_pct": 30.0},
    ]
    table = compute_percentile_table(players)
    # lower TO% is better -> player 1 should have HIGHER percentile
    assert table[1]["to_pct"] > table[2]["to_pct"]


def test_composite_weighted_average():
    pct_row = {"pts_pg": 80.0, "reb_pg": 40.0}
    weights = {"pts_pg": 50, "reb_pg": 50}
    score = composite_score(pct_row, weights)
    assert abs(score - 60.0) < 1e-6  # (80+40)/2


def test_composite_null_exclude_renormalizes():
    pct_row = {"pts_pg": 80.0, "reb_pg": None}
    weights = {"pts_pg": 50, "reb_pg": 50}
    score = composite_score(pct_row, weights, null_policy="exclude")
    assert abs(score - 80.0) < 1e-6  # reb excluded, only pts counts


def test_composite_null_median():
    pct_row = {"pts_pg": 80.0, "reb_pg": None}
    weights = {"pts_pg": 50, "reb_pg": 50}
    score = composite_score(pct_row, weights, null_policy="median")
    assert abs(score - 65.0) < 1e-6  # (80 + 50)/2


def test_composite_no_weighted_metrics_returns_none():
    assert composite_score({"pts_pg": 80.0}, {"pts_pg": 0}) is None


def test_athleticism_index_is_mean_of_component_percentiles():
    # Two players; the one higher on every athletic component must have a higher
    # athleticism index, and the index must be on the 0..100 scale.
    players = [
        {"id": 1, "division": "D1", "season": 2026, "blk_pct": 1.0, "stl_pct": 1.0,
         "orb_pct": 2.0, "dunk_rate": 0.1, "rim_rate": 20.0},
        {"id": 2, "division": "D1", "season": 2026, "blk_pct": 8.0, "stl_pct": 3.5,
         "orb_pct": 12.0, "dunk_rate": 2.0, "rim_rate": 60.0},
    ]
    table = compute_percentile_table(players)
    assert table[2]["athleticism"] > table[1]["athleticism"]
    assert 0 <= table[2]["athleticism"] <= 100


def test_athleticism_falls_back_when_components_missing():
    # Only blk%/stl% present (dunk/rim NULL) -> index still computed from what exists.
    players = [
        {"id": 1, "division": "D1", "season": 2026, "blk_pct": 1.0, "stl_pct": 1.0},
        {"id": 2, "division": "D1", "season": 2026, "blk_pct": 5.0, "stl_pct": 3.0},
    ]
    table = compute_percentile_table(players)
    assert table[1]["athleticism"] is not None
    assert table[2]["athleticism"] > table[1]["athleticism"]
