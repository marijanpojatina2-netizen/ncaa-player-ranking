"""One-off diagnostic v5: via the henrygd API (which returns clean JSON for
ncaa.com stat pages), dump a compact summary — title, page count, column keys,
and one sample row — for every D2 individual category we want to merge. Commits
data/diag/cats.json for local analysis.
"""
from __future__ import annotations

import json
import os
import time

import requests

UA = {"User-Agent": "Mozilla/5.0"}
BASE = "https://ncaa-api.henrygd.me/stats/basketball-men/d2/current/individual"
OUT = "data/diag"

# id -> our label, the categories we plan to merge
CATS = {
    136: "PPG", 137: "RPG", 140: "APG", 139: "SPG", 138: "BPG",
    628: "MPG", 141: "FG%", 142: "FT%", 143: "3P%",
    856: "OREB", 858: "DREB", 473: "A/TO", 144: "3PG",
}


def main():
    os.makedirs(OUT, exist_ok=True)
    summary = {}
    for cid, label in CATS.items():
        try:
            r = requests.get(f"{BASE}/{cid}", headers=UA, timeout=(10, 60))
            j = r.json()
            data = j.get("data", [])
            summary[cid] = {
                "label": label,
                "http": r.status_code,
                "title": j.get("title"),
                "pages": j.get("pages"),
                "columns": list(data[0].keys()) if data else [],
                "sample": data[0] if data else None,
            }
            print(f"{cid} {label}: HTTP {r.status_code}, pages={j.get('pages')}, "
                  f"cols={list(data[0].keys()) if data else []}")
        except Exception as e:
            summary[cid] = {"label": label, "error": str(e)}
            print(f"{cid} {label}: ERROR {e}")
        time.sleep(0.5)
    open(f"{OUT}/cats.json", "w", encoding="utf-8").write(json.dumps(summary, indent=1))
    print(f"\nwrote {OUT}/cats.json")


if __name__ == "__main__":
    main()
