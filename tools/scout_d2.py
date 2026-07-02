"""AI scouting tool: rank D2 seniors for two Croatian-league target profiles.

Profiles (per scouting brief):
  * GUARD (1/2): primary scorer, efficient shooting, high steals.
  * BIG (4/5):   athletic, above-the-rim, and a passing big (assists).

Method
------
Percentile scoring within the relevant candidate pool (D2 seniors, position
filtered), weights per profile below. Missing metrics contribute 0 and their
weight is NOT renormalized for "bonus" metrics, so a player only earns what his
data proves. Hard requirements keep out players whose key stats are unknown.

Athleticism has no direct free D2 stat, so "above the rim" is proxied by
blocks + offensive rebounds + FG% (rim finishing) — the standard box-score
athleticism triangle. Assists for a big are treated as a rare elite signal
(only ~13/187 D2 F/C rank on the assists leaderboard at all).

STEAL INDEX (small-budget lens): fit score discounted by national visibility
(scoring-volume percentile). Top national scorers are on every agent's list;
a player with elite profile fit but mid visibility is the under-the-radar buy.

Usage:  python tools/scout_d2.py            # prints reports
        python tools/scout_d2.py --json out.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "web" / "data_d2.json"


def pct_rank(values: list[float], v: float) -> float:
    """Percentile (0..1) of v within values (already filtered non-null)."""
    if not values:
        return 0.0
    below = sum(1 for x in values if x < v)
    equal = sum(1 for x in values if x == v)
    return (below + 0.5 * equal) / len(values)


def build_pools(players: list[dict]):
    seniors = [p for p in players if p.get("class") == "Sr"]
    guards = [p for p in seniors if (p.get("position") or "") == "G"]
    bigs = [p for p in seniors
            if (p.get("position") or "") in ("F", "C")
            and (p.get("height_in") or 0) >= 78]          # 6'6"+ for a 4/5
    return seniors, guards, bigs


def shooting_score(p: dict, pool: list[dict]) -> tuple[float, str]:
    """Best available efficiency signal, as pool percentile. Prefers TS%."""
    for key in ("ts_pct", "efg_pct"):
        if p.get(key) is not None:
            vals = [q[key] for q in pool if q.get(key) is not None]
            return pct_rank(vals, p[key]), key
    # fall back to 3P% + FT% blend (both are fractions)
    if p.get("fg3_pct") is not None or p.get("ft_pct") is not None:
        score, parts = 0.0, 0
        for key, w in (("fg3_pct", 0.6), ("ft_pct", 0.4)):
            if p.get(key) is not None:
                vals = [q[key] for q in pool if q.get(key) is not None]
                score += w * pct_rank(vals, p[key])
                parts += 1
        return (score if parts == 2 else score / (0.6 if p.get("fg3_pct") is not None else 0.4) * 1.0), "3p/ft"
    return 0.0, "none"


def metric_pct(p: dict, pool: list[dict], key: str) -> float | None:
    if p.get(key) is None:
        return None
    vals = [q[key] for q in pool if q.get(key) is not None]
    return pct_rank(vals, p[key])


def visibility(p: dict, seniors: list[dict]) -> float:
    """National-attention proxy: scoring-volume percentile among all seniors."""
    tot = (p.get("pts_pg") or 0) * (p.get("gp") or 0)
    vals = [(q.get("pts_pg") or 0) * (q.get("gp") or 0) for q in seniors]
    return pct_rank(vals, tot)


def score_guard(p: dict, guards: list[dict], seniors: list[dict]) -> dict | None:
    # Hard requirements: proven scorer AND ball-thief AND some shooting evidence
    # (the brief demands good shooting % — unknown efficiency can't top the list).
    if p.get("pts_pg") is None or p.get("stl_pg") is None:
        return None
    if all(p.get(k) is None for k in ("ts_pct", "efg_pct", "fg3_pct", "ft_pct")):
        return None
    sh, sh_src = shooting_score(p, guards)
    parts = {
        "scoring":  0.32 * (metric_pct(p, guards, "pts_pg") or 0),
        "steals":   0.28 * (metric_pct(p, guards, "stl_pg") or 0),
        "shooting": 0.25 * sh,
        "playmaking": 0.15 * (metric_pct(p, guards, "ast_pg") or 0),
    }
    fit = round(100 * sum(parts.values()), 1)
    vis = visibility(p, seniors)
    steal = round(fit * (1 - 0.25 * max(0.0, vis - 0.6) / 0.4), 1)  # only top-40% visibility discounted
    return {"player": p, "fit": fit, "steal": steal, "vis": round(vis, 2),
            "parts": {k: round(v * 100, 1) for k, v in parts.items()}, "shoot_src": sh_src}


def score_big(p: dict, bigs: list[dict], seniors: list[dict]) -> dict | None:
    # Hard requirement: at least two above-rim proxies known.
    proxies = [k for k in ("blk_pg", "oreb_pg", "reb_pg") if p.get(k) is not None]
    if len(proxies) < 1:
        return None
    parts = {
        "rim_blocks": 0.22 * (metric_pct(p, bigs, "blk_pg") or 0),
        "rim_oreb":   0.20 * (metric_pct(p, bigs, "oreb_pg") or 0),
        "finishing":  0.15 * (metric_pct(p, bigs, "fg_pct") or 0),
        "rebounding": 0.13 * (metric_pct(p, bigs, "reb_pg") or 0),
        "scoring":    0.10 * (metric_pct(p, bigs, "pts_pg") or 0),
    }
    # Passing-big bonus: ranking on the assists board at all is elite for a D2 big.
    ast = metric_pct(p, bigs, "ast_pg")
    parts["playmaking"] = 0.20 * (0.5 + 0.5 * ast) if ast is not None else 0.0
    fit = round(100 * sum(parts.values()), 1)
    vis = visibility(p, seniors)
    steal = round(fit * (1 - 0.25 * max(0.0, vis - 0.6) / 0.4), 1)
    return {"player": p, "fit": fit, "steal": steal, "vis": round(vis, 2),
            "parts": {k: round(v * 100, 1) for k, v in parts.items()}}


def ht(inch):
    if not inch:
        return "?"
    return f"{int(inch // 12)}'{int(inch % 12)}\""


def num(v, nd=1):
    return "—" if v is None else f"{v:.{nd}f}"


def fmt_row(r: dict, kind: str) -> str:
    p = r["player"]
    base = (f"{p['name']:<24} {p['team']:<22} {ht(p.get('height_in')):>5} "
            f"fit={r['fit']:>5} steal={r['steal']:>5} vis={r['vis']:.2f}")
    if kind == "G":
        base += (f" | pts={num(p.get('pts_pg'))} stl={num(p.get('stl_pg'))} "
                 f"ast={num(p.get('ast_pg'))} ts={num(p.get('ts_pct'))} 3p={num(p.get('fg3_pct'), 3)}")
    else:
        base += (f" | pts={num(p.get('pts_pg'))} reb={num(p.get('reb_pg'))} "
                 f"blk={num(p.get('blk_pg'))} oreb={num(p.get('oreb_pg'))} "
                 f"ast={num(p.get('ast_pg'))} fg={num(p.get('fg_pct'), 3)}")
    return base


PART_LABELS = {
    "scoring": "Poeni", "steals": "Ukradene", "shooting": "Šut",
    "playmaking": "Asistencije", "rim_blocks": "Blokade", "rim_oreb": "Nap. skok",
    "finishing": "FG% (rim)", "rebounding": "Skokovi",
}


def html_report(g_scores, b_scores, top: int) -> str:
    def rows(scores, kind):
        out = []
        for i, r in enumerate(scores[:top], 1):
            p = r["player"]
            if kind == "G":
                stats = (f"{num(p.get('pts_pg'))} pts · {num(p.get('stl_pg'))} stl · "
                         f"{num(p.get('ast_pg'))} ast · TS {num(p.get('ts_pct'))} · "
                         f"3P {num(p.get('fg3_pct'), 3)} · FT {num(p.get('ft_pct'), 3)}")
            else:
                stats = (f"{num(p.get('pts_pg'))} pts · {num(p.get('reb_pg'))} reb · "
                         f"{num(p.get('blk_pg'))} blk · {num(p.get('oreb_pg'))} oreb · "
                         f"{num(p.get('ast_pg'))} ast · FG {num(p.get('fg_pct'), 3)}")
            parts = " · ".join(f"{PART_LABELS.get(k, k)} {v:.0f}"
                               for k, v in sorted(r["parts"].items(), key=lambda kv: -kv[1]) if v > 0)
            radar = "🔥 ispod radara" if r["vis"] < 0.75 else "⚠️ na radaru (top scorer)"
            out.append(f"""<div class="card">
  <div class="row1"><span class="rank">#{i}</span>
    <span class="nm">{p['name']}</span>
    <span class="steal">{r['steal']:.0f}</span></div>
  <div class="row2">{p['team']} · {ht(p.get('height_in'))} · Sr · {radar} (vis {r['vis']:.2f})</div>
  <div class="row3">{stats}</div>
  <div class="row4">fit {r['fit']:.0f} → {parts}</div>
</div>""")
        return "\n".join(out)

    return f"""<!DOCTYPE html>
<html lang="hr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🎯 D2 Scout — preporuke</title>
<style>
 body{{background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif;margin:0;padding:12px}}
 h1{{font-size:1.15rem}} h2{{font-size:1rem;color:#34d399;margin:18px 0 8px}}
 .sub{{color:#94a3b8;font-size:.75rem;margin-bottom:10px}}
 .card{{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:10px;margin-bottom:8px}}
 .row1{{display:flex;align-items:center;gap:8px}}
 .rank{{color:#64748b;font-weight:700}} .nm{{font-weight:700;flex:1}}
 .steal{{background:#34d399;color:#0f172a;font-weight:800;border-radius:8px;padding:2px 8px}}
 .row2{{color:#94a3b8;font-size:.75rem;margin-top:2px}}
 .row3{{font-family:ui-monospace,monospace;font-size:.8rem;margin-top:6px}}
 .row4{{color:#64748b;font-size:.7rem;margin-top:4px}}
</style></head><body>
<h1>🎯 D2 Scout — preporuke za hrvatsku ligu (mali budžet)</h1>
<p class="sub">STEAL = profil-fit (percentili unutar D2 seniora) umanjen za nacionalnu vidljivost
(top skoreri su na radaru agenata i skupljih klubova). „—" = nije na toj NCAA ljestvici.
Izvor: ncaa.com službene ljestvice, sezona 2025-26.</p>
<h2>GUARD 1/2 — scorer, postotak šuta, ukradene lopte</h2>
{rows(g_scores, "G")}
<h2>ATLETSKI 4/5 — above-the-rim (blokade+nap. skok+FG%), passing big</h2>
{rows(b_scores, "B")}
<p class="sub">Alat: tools/scout_d2.py — težine se lako mijenjaju. Sljedeći korak za svakog
kandidata: video/eligibility provjera prije kontakta.</p>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, help="also write full ranked results as JSON")
    ap.add_argument("--html", type=Path, help="write a mobile-friendly HTML report")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    d = json.loads(DATA.read_text())
    seniors, guards, bigs = build_pools(d["players"])
    print(f"pool: {len(seniors)} seniors -> {len(guards)} guards, {len(bigs)} bigs 6'6\"+\n")

    g_scores = sorted(filter(None, (score_guard(p, guards, seniors) for p in guards)),
                      key=lambda r: -r["steal"])
    b_scores = sorted(filter(None, (score_big(p, bigs, seniors) for p in bigs)),
                      key=lambda r: -r["steal"])

    print(f"=== GUARD 1/2 (scorer + shooting + steals) — top {args.top} by STEAL index ===")
    for r in g_scores[:args.top]:
        print(fmt_row(r, "G"))
    print(f"\n=== ATHLETIC 4/5 (above-rim + passing big) — top {args.top} by STEAL index ===")
    for r in b_scores[:args.top]:
        print(fmt_row(r, "B"))

    if args.json:
        out = {"guards": [{**r, "player": r["player"]} for r in g_scores],
               "bigs": [{**r, "player": r["player"]} for r in b_scores]}
        args.json.write_text(json.dumps(out, indent=1))
        print(f"\nwrote {args.json}")
    if args.html:
        args.html.write_text(html_report(g_scores, b_scores, args.top))
        print(f"wrote {args.html}")


if __name__ == "__main__":
    main()
