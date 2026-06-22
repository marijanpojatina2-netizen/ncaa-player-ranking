"""Enrich players with position, height, and weight from ESPN's public roster API.

ESPN exposes an undocumented but stable roster endpoint per team:

    site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/teams
    site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/teams/{id}/roster

The team list gives every D1 team id; each roster gives athletes with
displayHeight, weight (lbs), and position. We match to existing `players` rows
by normalized full name (best-effort; team names differ between sources) and
fill height_in / weight_lb / position.

Coverage note: ESPN rosters reflect the CURRENT roster, so weight/height are
applied to all of a player's stored seasons (these are roughly static). This is
a one-time enrichment — run after the Torvik ingest.

VERIFY: ESPN endpoints are undocumented and can change. Use --inspect to see one
team's parsed roster.
"""
from __future__ import annotations

import argparse
import json
import re

from app.db import get_conn, init_db
from ingest.common import fetch, parse_height_to_inches, to_float, utcnow_iso

TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/teams"
ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/teams/{id}/roster"


def _norm_name(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[.’']", "", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    s = re.sub(r"[^a-z\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def list_team_ids(refresh: bool) -> list[str]:
    text = fetch(TEAMS_URL, {"limit": 1000}, refresh=refresh)
    data = json.loads(text)
    ids = []
    try:
        leagues = data["sports"][0]["leagues"][0]["teams"]
        for t in leagues:
            ids.append(str(t["team"]["id"]))
    except (KeyError, IndexError):
        pass
    return ids


def fetch_roster(team_id: str, refresh: bool) -> list[dict]:
    text = fetch(ROSTER_URL.format(id=team_id), refresh=refresh)
    data = json.loads(text)
    out = []
    for ath in data.get("athletes", []):
        # Some payloads nest athletes under position groups.
        items = ath.get("items") if isinstance(ath, dict) and "items" in ath else [ath]
        for a in items:
            name = a.get("fullName") or a.get("displayName")
            if not name:
                continue
            pos = (a.get("position") or {})
            out.append({
                "name": name,
                "height_in": parse_height_to_inches(a.get("displayHeight") or a.get("height")),
                "weight_lb": to_float(a.get("weight")),
                "position": pos.get("abbreviation") or pos.get("name"),
            })
    return out


def enrich(seasons: list[int] | None, refresh: bool = False) -> None:
    init_db()
    team_ids = list_team_ids(refresh)
    print(f"[espn] {len(team_ids)} teams")
    roster: dict[str, dict] = {}
    for i, tid in enumerate(team_ids, 1):
        try:
            for p in fetch_roster(tid, refresh):
                roster.setdefault(_norm_name(p["name"]), p)
        except Exception as exc:
            print(f"  [warn] team {tid} roster failed: {exc}")
        if i % 50 == 0:
            print(f"  ...{i}/{len(team_ids)} teams")
    print(f"[espn] collected {len(roster)} unique players")

    now = utcnow_iso()
    matched = 0
    with get_conn() as conn:
        where = "WHERE division='D1'"
        params: tuple = ()
        if seasons:
            where += f" AND season IN ({','.join('?' for _ in seasons)})"
            params = tuple(seasons)
        rows = conn.execute(f"SELECT id, name FROM players {where}", params).fetchall()
        for r in rows:
            info = roster.get(_norm_name(r["name"]))
            if not info:
                continue
            # weight always (ESPN-owned); height & position only if missing.
            conn.execute(
                """UPDATE players SET
                       weight_lb = COALESCE(?, weight_lb),
                       height_in = COALESCE(height_in, ?),
                       position  = COALESCE(position, ?),
                       updated_at = ?
                   WHERE id = ?""",
                (info["weight_lb"], info["height_in"], info["position"], now, r["id"]),
            )
            matched += 1
        conn.commit()
    print(f"[espn] enriched {matched}/{len(rows)} player rows with position/height/weight")


def inspect(refresh: bool) -> None:
    ids = list_team_ids(refresh)
    print(f"[inspect] {len(ids)} team ids; sample={ids[:5]}")
    if ids:
        sample = fetch_roster(ids[0], refresh)
        print(f"[inspect] team {ids[0]} roster ({len(sample)} players):")
        for p in sample[:8]:
            print(f"   {p}")


def main():
    ap = argparse.ArgumentParser(description="ESPN roster enrichment (position/height/weight)")
    ap.add_argument("--seasons", type=int, nargs="*", help="limit enrichment to these seasons")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--inspect", action="store_true")
    args = ap.parse_args()
    if args.inspect:
        inspect(args.refresh)
        return
    enrich(args.seasons, args.refresh)


if __name__ == "__main__":
    main()
