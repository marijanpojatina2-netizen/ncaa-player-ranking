"""One-off diagnostic v2: ncaa.com is directly reachable from the runner. Find
WHERE the stats data lives (embedded JSON / data API) and extract the real D2
men's basketball stat-category IDs. Run via .github/workflows/d2-diag.yml.
"""
from __future__ import annotations

import re

import requests

UA = {"User-Agent": (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)}

PAGE = "https://www.ncaa.com/stats/basketball-men/d2/current/individual/147"


def main():
    r = requests.get(PAGE, headers=UA, timeout=(10, 60))
    b = r.text or ""
    print(f"PAGE {PAGE}\nHTTP {r.status_code}, {len(b)} bytes\n")

    markers = ["__NEXT_DATA__", "application/json", "application/ld+json",
               "data.ncaa.com", "casablanca", "<table", "<tbody", "window.__",
               "stats_player", "Rank", "/json/", "RPG", "PPG"]
    print("MARKERS:")
    for m in markers:
        print(f"  {m!r}: {b.count(m)}")

    # Stat-category dropdown options: /stats/basketball-men/d2/current/(individual|team)/<id>
    cats = re.findall(
        r'/stats/basketball-men/d2/current/(individual|team)/(\d+)"[^>]*>([^<]{1,40})',
        b)
    seen = set()
    print(f"\nCATEGORY LINKS ({len(cats)} raw):")
    for kind, cid, label in cats:
        key = (kind, cid)
        if key in seen:
            continue
        seen.add(key)
        print(f"  {kind}/{cid}  {label.strip()!r}")

    # If Next.js, dump a slice of __NEXT_DATA__
    i = b.find("__NEXT_DATA__")
    if i != -1:
        print("\n__NEXT_DATA__ slice:")
        print(b[i:i + 1200])

    # Any obvious data API URL referenced
    apis = sorted(set(re.findall(r'https?://[a-z0-9.\-]*ncaa\.com[^\s"\'<>]{0,80}', b)))
    print(f"\nNCAA URLs referenced ({len(apis)}):")
    for u in apis[:40]:
        print("  " + u)

    # Window around first occurrence of a stat word, to see how rows are encoded
    for probe in ("Rank", "PPG", "<tbody", "json"):
        j = b.find(probe)
        if j != -1:
            print(f"\n--- window around {probe!r} @ {j} ---")
            print(b[j - 200:j + 600].replace("\n", " "))
            break


if __name__ == "__main__":
    main()
