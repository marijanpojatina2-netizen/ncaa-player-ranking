"""One-off diagnostic v4: dump the raw ncaa.com D2 stats page (category 136 =
Points Per Game) to a file so it can be committed and analyzed locally (the
sandbox can't reach ncaa.com). Also dump henrygd's response. Run via
.github/workflows/d2-diag.yml, which commits data/diag/ back to the repo.
"""
from __future__ import annotations

import os

import requests

UA = {"User-Agent": (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)}

NCAA = "https://www.ncaa.com/stats/basketball-men/d2/current/individual/136"
HENRY = "https://ncaa-api.henrygd.me/stats/basketball-men/d2/current/individual/136"
OUT = "data/diag"


def main():
    os.makedirs(OUT, exist_ok=True)
    r = requests.get(NCAA, headers=UA, timeout=(10, 60))
    open(f"{OUT}/ncaa_136.html", "w", encoding="utf-8").write(r.text)
    print(f"ncaa /136: HTTP {r.status_code}, {len(r.text)} bytes -> {OUT}/ncaa_136.html")

    try:
        r2 = requests.get(HENRY, headers=UA, timeout=(10, 60))
        open(f"{OUT}/henry_136.json", "w", encoding="utf-8").write(r2.text)
        print(f"henrygd /136: HTTP {r2.status_code}, {len(r2.text)} bytes -> {OUT}/henry_136.json")
    except Exception as e:
        print("henrygd error:", e)


if __name__ == "__main__":
    main()
