"""AI scouting tool: rank NCAA D1 + D2 seniors for two Croatian-league profiles.

Profiles (per scouting brief):
  * GUARD (1/2): primary scorer, efficient shooting, high steals.
  * BIG (4/5):   athletic, above-the-rim, and a passing big (assists).

Method
------
Percentile scoring within the relevant candidate pool (seniors, position + role
filtered), weights per profile below.

D1 (Bart Torvik data, complete): rate stats preferred (stl%, TS%, blk%, ORB%,
2P%) plus BPM as an overall-impact tiebreaker. Above-the-rim is proxied by
blk% + ORB% + 2P% (dunk/rim-attempt data isn't populated in the DB).

D2 (ncaa.com leaderboards, sparse): per-game box stats; missing metrics earn 0
so a player only gets credit for what his data proves. A D2 big appearing on
the assists leaderboard at all is treated as an elite passing signal.

STEAL INDEX (small-budget lens): fit score discounted by visibility — how
likely richer clubs/agents already track the player. For D2 that's national
scoring volume; for D1 it folds in conference tier (high-major seniors are
priced out; mid/low-major is the realistic market).

Usage:  python tools/scout.py                       # print all four lists
        python tools/scout.py --html web/scout.html # publishable report
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_D2 = ROOT / "web" / "data_d2.json"
DATA_D1 = ROOT / "web" / "data.json"

# --- D1 role buckets (Torvik position labels) + market tiers ----------------
D1_GUARD_POS = {"Pure PG", "Scoring PG", "Combo G", "Wing G"}
D1_BIG_POS = {"PF/C", "C", "Stretch 4", "Wing F"}
HIGH_MAJOR = {"B10", "B12", "SEC", "ACC", "BE"}
MID_MAJOR = {"Amer", "A10", "MWC", "WCC", "MVC", "CUSA", "SB", "MAC"}


def pct_rank(values: list[float], v: float) -> float:
    if not values:
        return 0.0
    below = sum(1 for x in values if x < v)
    equal = sum(1 for x in values if x == v)
    return (below + 0.5 * equal) / len(values)


def metric_pct(p: dict, pool: list[dict], key: str) -> float | None:
    if p.get(key) is None:
        return None
    vals = [q[key] for q in pool if q.get(key) is not None]
    return pct_rank(vals, p[key])


def ht(inch):
    if not inch:
        return "?"
    return f"{int(inch // 12)}'{int(inch % 12)}\""


def num(v, nd=1):
    return "—" if v is None else f"{v:.{nd}f}"


def steal_index(fit: float, vis: float) -> float:
    """Discount fit for the most visible players (top 40% of visibility)."""
    return round(fit * (1 - 0.25 * max(0.0, vis - 0.6) / 0.4), 1)


# =============================== D2 =========================================
def d2_pools(players: list[dict]):
    seniors = [p for p in players if p.get("class") == "Sr"]
    guards = [p for p in seniors if (p.get("position") or "") == "G"]
    bigs = [p for p in seniors
            if (p.get("position") or "") in ("F", "C")
            and (p.get("height_in") or 0) >= 78]
    return seniors, guards, bigs


def d2_shooting(p: dict, pool: list[dict]) -> float:
    for key in ("ts_pct", "efg_pct"):
        if p.get(key) is not None:
            return metric_pct(p, pool, key) or 0
    score = 0.0
    for key, w in (("fg3_pct", 0.6), ("ft_pct", 0.4)):
        if p.get(key) is not None:
            score += w * (metric_pct(p, pool, key) or 0)
    return score


def d2_visibility(p: dict, seniors: list[dict]) -> float:
    tot = (p.get("pts_pg") or 0) * (p.get("gp") or 0)
    vals = [(q.get("pts_pg") or 0) * (q.get("gp") or 0) for q in seniors]
    return pct_rank(vals, tot)


def d2_guard(p: dict, guards: list[dict], seniors: list[dict]) -> dict | None:
    if p.get("pts_pg") is None or p.get("stl_pg") is None:
        return None
    if all(p.get(k) is None for k in ("ts_pct", "efg_pct", "fg3_pct", "ft_pct")):
        return None
    parts = {
        "scoring":  0.32 * (metric_pct(p, guards, "pts_pg") or 0),
        "steals":   0.28 * (metric_pct(p, guards, "stl_pg") or 0),
        "shooting": 0.25 * d2_shooting(p, guards),
        "playmaking": 0.15 * (metric_pct(p, guards, "ast_pg") or 0),
    }
    fit = round(100 * sum(parts.values()), 1)
    vis = d2_visibility(p, seniors)
    return {"player": p, "fit": fit, "steal": steal_index(fit, vis), "vis": round(vis, 2),
            "parts": {k: round(v * 100, 1) for k, v in parts.items()}}


def d2_big(p: dict, bigs: list[dict], seniors: list[dict]) -> dict | None:
    if all(p.get(k) is None for k in ("blk_pg", "oreb_pg", "reb_pg")):
        return None
    parts = {
        "rim_blocks": 0.22 * (metric_pct(p, bigs, "blk_pg") or 0),
        "rim_oreb":   0.20 * (metric_pct(p, bigs, "oreb_pg") or 0),
        "finishing":  0.15 * (metric_pct(p, bigs, "fg_pct") or 0),
        "rebounding": 0.13 * (metric_pct(p, bigs, "reb_pg") or 0),
        "scoring":    0.10 * (metric_pct(p, bigs, "pts_pg") or 0),
    }
    ast = metric_pct(p, bigs, "ast_pg")
    parts["playmaking"] = 0.20 * (0.5 + 0.5 * ast) if ast is not None else 0.0
    fit = round(100 * sum(parts.values()), 1)
    vis = d2_visibility(p, seniors)
    return {"player": p, "fit": fit, "steal": steal_index(fit, vis), "vis": round(vis, 2),
            "parts": {k: round(v * 100, 1) for k, v in parts.items()}}


# =============================== D1 =========================================
def d1_pools(players: list[dict], season: int):
    seniors = [p for p in players
               if p.get("class") == "Sr" and p.get("season") == season
               and (p.get("gp") or 0) >= 15 and (p.get("min_pct") or 0) >= 40]
    guards = [p for p in seniors if (p.get("position") or "") in D1_GUARD_POS]
    bigs = [p for p in seniors
            if (p.get("position") or "") in D1_BIG_POS
            and (p.get("height_in") or 0) >= 79]           # 6'7"+ for a 4/5
    return seniors, guards, bigs


def d1_visibility(p: dict, seniors: list[dict]) -> float:
    conf = p.get("conference") or ""
    tier = 1.0 if conf in HIGH_MAJOR else (0.65 if conf in MID_MAJOR else 0.35)
    tot = (p.get("pts_pg") or 0) * (p.get("gp") or 0)
    vals = [(q.get("pts_pg") or 0) * (q.get("gp") or 0) for q in seniors]
    return 0.55 * tier + 0.45 * pct_rank(vals, tot)


def d1_guard(p: dict, guards: list[dict], seniors: list[dict]) -> dict:
    parts = {
        "scoring":  0.30 * (metric_pct(p, guards, "pts_pg") or 0),
        "steals":   0.28 * (metric_pct(p, guards, "stl_pct") or 0),
        "shooting": 0.25 * (metric_pct(p, guards, "ts_pct") or 0),
        "playmaking": 0.10 * (metric_pct(p, guards, "ast_pg") or 0),
        "impact":   0.07 * (metric_pct(p, guards, "bpm") or 0),
    }
    fit = round(100 * sum(parts.values()), 1)
    vis = d1_visibility(p, seniors)
    return {"player": p, "fit": fit, "steal": steal_index(fit, vis), "vis": round(vis, 2),
            "parts": {k: round(v * 100, 1) for k, v in parts.items()}}


def d1_big(p: dict, bigs: list[dict], seniors: list[dict]) -> dict:
    parts = {
        "playmaking": 0.25 * (metric_pct(p, bigs, "ast_pg") or 0),
        "rim_blocks": 0.20 * (metric_pct(p, bigs, "blk_pct") or 0),
        "rim_oreb":   0.18 * (metric_pct(p, bigs, "orb_pct") or 0),
        "finishing":  0.15 * (metric_pct(p, bigs, "fg2_pct") or 0),
        "rebounding": 0.07 * (metric_pct(p, bigs, "drb_pct") or 0),
        "scoring":    0.08 * (metric_pct(p, bigs, "pts_pg") or 0),
        "impact":     0.07 * (metric_pct(p, bigs, "bpm") or 0),
    }
    fit = round(100 * sum(parts.values()), 1)
    vis = d1_visibility(p, seniors)
    return {"player": p, "fit": fit, "steal": steal_index(fit, vis), "vis": round(vis, 2),
            "parts": {k: round(v * 100, 1) for k, v in parts.items()}}


# ===================== D1 "buy the dip" =====================================
def find_dips(players: list[dict], season: int) -> list[dict]:
    """Seniors whose first 3 seasons were far better than their senior year.

    The classic under-the-radar buy: a mid-major star transfers up (or gets
    hurt), loses his role as a senior, and his market price follows the bad
    senior line while the 3-year track record shows the real level. Criteria:
    peak prior-season BPM >= 3.0 with real minutes, then a senior collapse
    (BPM -2, or minutes% -25, or points -5). Ranked by peak BPM (what you buy).
    """
    by: dict[str, list[dict]] = {}
    for p in players:
        by.setdefault(p.get("torvik_pid"), []).append(p)
    out = []
    for s in players:
        if s.get("class") != "Sr" or s.get("season") != season or (s.get("gp") or 0) < 5:
            continue
        hist = [q for q in by.get(s["torvik_pid"], [])
                if q["season"] < season and (q.get("min_pct") or 0) >= 40
                and (q.get("gp") or 0) >= 15]
        if not hist:
            continue
        peak = max(hist, key=lambda q: q.get("bpm") or -99)
        pb, cb = peak.get("bpm"), s.get("bpm")
        if pb is None or cb is None or pb < 3.0:
            continue
        if (pb - cb >= 2.0
                or (peak.get("min_pct") or 0) - (s.get("min_pct") or 0) >= 25
                or (peak.get("pts_pg") or 0) - (s.get("pts_pg") or 0) >= 5):
            out.append({"now": s, "peak": peak})
    out.sort(key=lambda r: -(r["peak"].get("bpm") or 0))
    return out


def fmt_dip(r: dict) -> str:
    s, pk = r["now"], r["peak"]
    tr = "" if s["team"] == pk["team"] else f"  [{pk['team']} → {s['team']}]"
    return (f"{s['name']:<22} {(s.get('position') or '?'):<10} {ht(s.get('height_in')):>5}"
            f" peak'{pk['season'] % 100}: bpm={pk['bpm']:.1f} {num(pk.get('pts_pg'))}pts"
            f" ts={num(pk.get('ts_pct'))} | '{s['season'] % 100}: bpm={s['bpm']:.1f}"
            f" {num(s.get('pts_pg'))}pts min%={num(s.get('min_pct'), 0)}{tr}")


def dip_card(i: int, r: dict) -> str:
    s, pk = r["now"], r["peak"]
    tr = ("" if s["team"] == pk["team"]
          else f'<div class="row2">↪ transfer: {pk["team"]} → {s["team"]} (uloga nestala)</div>')
    peak_line = (f"PEAK '{pk['season'] % 100} ({pk['team']}): {num(pk.get('pts_pg'))} pts · "
                 f"TS {num(pk.get('ts_pct'))} · {num(pk.get('ast_pg'))} ast · "
                 f"stl% {num(pk.get('stl_pct'))} · blk% {num(pk.get('blk_pct'))} · BPM {num(pk.get('bpm'))}")
    now_line = (f"SADA '{s['season'] % 100}: {num(s.get('pts_pg'))} pts · min% {num(s.get('min_pct'), 0)} · "
                f"BPM {num(s.get('bpm'))}")
    return f"""<div class="card">
  <div class="row1"><span class="rank">#{i}</span>
    <span class="nm">{s['name']}</span>
    <span class="steal">{pk['bpm']:.1f}</span></div>
  <div class="row2">{s.get('position')} · {ht(s.get('height_in'))} · Sr · sada: {s['team']}</div>
  {tr}
  <div class="row3">{peak_line}</div>
  <div class="row3" style="color:#f87171">{now_line}</div>
</div>"""


# ============================ output ========================================
def fmt_row(r: dict, kind: str) -> str:
    p = r["player"]
    base = (f"{p['name']:<24} {(p.get('team') or ''):<20} {ht(p.get('height_in')):>5} "
            f"fit={r['fit']:>5} steal={r['steal']:>5} vis={r['vis']:.2f}")
    if kind == "G":
        base += (f" | pts={num(p.get('pts_pg'))} stl={num(p.get('stl_pg'))}/{num(p.get('stl_pct'))}% "
                 f"ast={num(p.get('ast_pg'))} ts={num(p.get('ts_pct'))}")
    else:
        base += (f" | pts={num(p.get('pts_pg'))} reb={num(p.get('reb_pg'))} "
                 f"blk={num(p.get('blk_pg'))}/{num(p.get('blk_pct'))}% "
                 f"ast={num(p.get('ast_pg'))} 2p={num(p.get('fg2_pct'), 3)}")
    return base


PART_LABELS = {
    "scoring": "Poeni", "steals": "Ukradene", "shooting": "Šut",
    "playmaking": "Asistencije", "rim_blocks": "Blokade", "rim_oreb": "Nap. skok",
    "finishing": "Finiširanje", "rebounding": "Skokovi", "impact": "BPM",
}


def card(i, r, kind, div):
    p = r["player"]
    conf = f" · {p['conference']}" if p.get("conference") else ""
    if kind == "G":
        stats = (f"{num(p.get('pts_pg'))} pts · {num(p.get('stl_pg'))} stl"
                 + (f" ({num(p.get('stl_pct'))}%)" if p.get('stl_pct') is not None else "")
                 + f" · {num(p.get('ast_pg'))} ast · TS {num(p.get('ts_pct'))}"
                 + f" · 3P {num(p.get('fg3_pct'), 3)}")
        if p.get("bpm") is not None:
            stats += f" · BPM {num(p.get('bpm'))}"
    else:
        stats = (f"{num(p.get('pts_pg'))} pts · {num(p.get('reb_pg'))} reb · "
                 f"{num(p.get('blk_pg'))} blk"
                 + (f" ({num(p.get('blk_pct'))}%)" if p.get('blk_pct') is not None else "")
                 + f" · {num(p.get('ast_pg'))} ast"
                 + (f" · 2P {num(p.get('fg2_pct'), 3)}" if p.get('fg2_pct') is not None else
                    f" · FG {num(p.get('fg_pct'), 3)}"))
        if p.get("bpm") is not None:
            stats += f" · BPM {num(p.get('bpm'))}"
    parts = " · ".join(f"{PART_LABELS.get(k, k)} {v:.0f}"
                       for k, v in sorted(r["parts"].items(), key=lambda kv: -kv[1]) if v > 0)
    radar = "🔥 ispod radara" if r["vis"] < 0.72 else "⚠️ na radaru"
    return f"""<div class="card">
  <div class="row1"><span class="rank">#{i}</span>
    <span class="nm">{p['name']}</span>
    <span class="steal">{r['steal']:.0f}</span></div>
  <div class="row2">{p.get('team')}{conf} · {ht(p.get('height_in'))} · Sr · {radar} (vis {r['vis']:.2f})</div>
  <div class="row3">{stats}</div>
  <div class="row4">fit {r['fit']:.0f} → {parts}</div>
</div>"""


def html_report(sections: list[tuple[str, list, str]], top: int, dips: list[dict]) -> str:
    body = "\n".join(
        f"<h2>{title}</h2>\n" + "\n".join(card(i, r, kind, title) for i, r in enumerate(scores[:top], 1))
        for title, scores, kind in sections)
    body += ("\n<h2>D1 · 💎 BUY THE DIP — zvijezde prve 3 sezone, loša senior godina</h2>\n"
             '<p class="sub">Mid-major zvijezda transferira na high-major, izgubi ulogu → cijena joj se '
             "formira po lošoj senior sezoni, a 3-godišnji track record pokazuje pravu razinu. "
             "Zelena brojka = peak BPM (što kupuješ). Obavezna provjera: razlog pada (uloga vs. ozljeda).</p>\n"
             + "\n".join(dip_card(i, r) for i, r in enumerate(dips, 1)))
    return f"""<!DOCTYPE html>
<html lang="hr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🎯 NCAA Scout — preporuke</title>
<style>
 body{{background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif;margin:0;padding:12px;max-width:760px;margin:auto}}
 h1{{font-size:1.15rem}} h2{{font-size:1rem;color:#34d399;margin:20px 0 8px}}
 .sub{{color:#94a3b8;font-size:.75rem;margin-bottom:10px}}
 .card{{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:10px;margin-bottom:8px}}
 .row1{{display:flex;align-items:center;gap:8px}}
 .rank{{color:#64748b;font-weight:700}} .nm{{font-weight:700;flex:1}}
 .steal{{background:#34d399;color:#0f172a;font-weight:800;border-radius:8px;padding:2px 8px}}
 .row2{{color:#94a3b8;font-size:.75rem;margin-top:2px}}
 .row3{{font-family:ui-monospace,monospace;font-size:.8rem;margin-top:6px}}
 .row4{{color:#64748b;font-size:.7rem;margin-top:4px}}
</style></head><body>
<h1>🎯 NCAA Scout — preporuke za hrvatsku ligu (mali budžet)</h1>
<p class="sub">STEAL = profil-fit (percentili među seniorima te divizije/pozicije) umanjen za
vidljivost: D1 uračunava jačinu konferencije (high-major = skupo), D2 nacionalni scoring rang.
D1 = Torvik napredne metrike (potpuno); D2 = ncaa.com ljestvice („—" = nije rangiran).
Sezona 2025-26, samo seniori.</p>
{body}
<p class="sub">Alat: tools/scout.py — težine i filtri lako se mijenjaju.
Za svakog kandidata prije kontakta: video + eligibility provjera.</p>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    ap.add_argument("--html", type=Path)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--season", type=int, default=2026)
    args = ap.parse_args()

    # D1
    d1 = json.loads(DATA_D1.read_text())
    s1, g1, b1 = d1_pools(d1["players"], args.season)
    print(f"D1 pool: {len(s1)} rotation seniors -> {len(g1)} guards, {len(b1)} bigs 6'7\"+")
    d1_g = sorted((d1_guard(p, g1, s1) for p in g1), key=lambda r: -r["steal"])
    d1_b = sorted((d1_big(p, b1, s1) for p in b1), key=lambda r: -r["steal"])

    # D2
    d2 = json.loads(DATA_D2.read_text())
    s2, g2, b2 = d2_pools(d2["players"])
    print(f"D2 pool: {len(s2)} seniors -> {len(g2)} guards, {len(b2)} bigs 6'6\"+\n")
    d2_g = sorted(filter(None, (d2_guard(p, g2, s2) for p in g2)), key=lambda r: -r["steal"])
    d2_b = sorted(filter(None, (d2_big(p, b2, s2) for p in b2)), key=lambda r: -r["steal"])

    sections = [
        ("D1 · GUARD 1/2 — scorer, šut, ukradene", d1_g, "G"),
        ("D1 · ATLETSKI 4/5 — above-the-rim + passing big", d1_b, "B"),
        ("D2 · GUARD 1/2 — scorer, šut, ukradene", d2_g, "G"),
        ("D2 · ATLETSKI 4/5 — above-the-rim + passing big", d2_b, "B"),
    ]
    for title, scores, kind in sections:
        print(f"=== {title} — top {args.top} by STEAL ===")
        for r in scores[:args.top]:
            print(fmt_row(r, kind))
        print()

    dips = find_dips(d1["players"], args.season)
    print(f"=== D1 · BUY THE DIP — peak prve 3 sezone >> senior ({len(dips)}) ===")
    for r in dips:
        print(fmt_dip(r))
    print()

    if args.json:
        args.json.write_text(json.dumps(
            {t: s[:max(args.top * 3, 50)] for t, s, _ in sections}, indent=1))
        print(f"wrote {args.json}")
    if args.html:
        args.html.write_text(html_report(sections, args.top, dips))
        print(f"wrote {args.html}")


if __name__ == "__main__":
    main()
