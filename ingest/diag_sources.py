"""One-off diagnostic v3: locate the player-rows data for a VALID individual D2
category (136 = Points Per Game) on ncaa.com, and test the henrygd API for it.
Concise output. Run via .github/workflows/d2-diag.yml.
"""
from __future__ import annotations

import re

import requests

UA = {"User-Agent": (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)}

NCAA = "https://www.ncaa.com/stats/basketball-men/d2/current/individual/136"
HENRY = "https://ncaa-api.henrygd.me/stats/basketball-men/d2/current/individual/136"


def main():
    # 1) henrygd for a VALID category
    try:
        r = requests.get(HENRY, headers=UA, timeout=(10, 60))
        print(f"HENRYGD /136: HTTP {r.status_code}, {len(r.text)} bytes")
        print("  head:", repr(r.text[:500]))
    except Exception as e:
        print("HENRYGD error:", e)

    # 2) ncaa.com embedded data
    r = requests.get(NCAA, headers=UA, timeout=(10, 60))
    b = r.text or ""
    print(f"\nNCAA /136: HTTP {r.status_code}, {len(b)} bytes")
    for m in ('"Rank"', "Rank", '"player"', '"School"', '"Cls"', "stats_player",
              "updated_at", '"data"', '"rows"', "so-stat", "tablesaw", "<table"):
        print(f'  marker {m!r}: {b.count(m)}')

    # full data.ncaa.com URLs (with path)
    apis = sorted(set(re.findall(r'https?://data\.ncaa\.com[^\s"\'<>\\]*', b)))
    print("  data.ncaa.com urls:", apis[:10])

    # application/json script blocks
    blocks = re.findall(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', b, re.S)
    print(f"  application/json blocks: {len(blocks)}")
    for i, blk in enumerate(blocks):
        print(f"  --- block {i}: {len(blk)} chars, head: {blk[:300]!r}")

    # window around first interesting stat marker
    for probe in ("tablesaw", "soial", "Rank", "player", "School"):
        j = b.find(probe)
        if j != -1:
            print(f"\n--- window {probe!r} @ {j} ---")
            print(b[j - 150:j + 700].replace("\n", " "))
            break


if __name__ == "__main__":
    main()
