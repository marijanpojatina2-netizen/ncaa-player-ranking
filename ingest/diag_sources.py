"""One-off diagnostic: check how many players each candidate D2 individual
category returns (pages) and its columns, to find the broadest-coverage set.
Per-game leaderboards have a high qualification bar (~300); counting/total
leaderboards may list every player (thousands). Commits data/diag/cats2.json.
"""
from __future__ import annotations

import json
import os
import time

import requests

UA = {"User-Agent": "Mozilla/5.0"}
BASE = "https://ncaa-api.henrygd.me/stats/basketball-men/d2/current/individual"
OUT = "data/diag"

# candidate categories: totals/counting (likely no per-game minimum) + a few refs
CATS = {
    600: "Points(total)", 601: "Rebounds(total)", 605: "Assists(total)",
    608: "Blocks(total)", 615: "Steals(total)", 611: "FieldGoals(total)",
    850: "FreeThrows(total)", 851: "FTA(total)", 618: "FGA(total)",
    621: "3PM(total)", 624: "3PA(total)", 556: "DoubleDoubles",
    136: "PPG(ref)",
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
                "label": label, "http": r.status_code,
                "title": j.get("title"), "pages": j.get("pages"),
                "rows_page1": len(data),
                "columns": list(data[0].keys()) if data else [],
                "sample": data[0] if data else None,
            }
            print(f"{cid} {label}: pages={j.get('pages')} rows/pg={len(data)} cols={list(data[0].keys()) if data else []}")
        except Exception as e:
            summary[cid] = {"label": label, "error": str(e)}
            print(f"{cid} {label}: ERROR {e}")
        time.sleep(0.5)
    open(f"{OUT}/cats2.json", "w", encoding="utf-8").write(json.dumps(summary, indent=1))
    print(f"wrote {OUT}/cats2.json")


if __name__ == "__main__":
    main()
