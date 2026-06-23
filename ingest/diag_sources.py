"""One-off diagnostic: from a GitHub runner, probe candidate NCAA data sources
and report which are reachable and return *parseable* content. Lets us pick a
reliable D2 fetch path without blind trial-and-error. Prints status + key markers.
Run via .github/workflows/d2-diag.yml. Not part of the app.
"""
from __future__ import annotations

from urllib.parse import quote

import requests

UA = {"User-Agent": (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)}

CANDIDATES = [
    # label, url, extra headers, marker to look for in body
    ("direct stats.ncaa.org/teams/history",
     "https://stats.ncaa.org/teams/history", {}, "org_id_select"),
    ("direct ncaa.com d2 scoring leaderboard",
     "https://www.ncaa.com/stats/basketball-men/d2/current/individual/147", {}, "<table"),
    ("henrygd hosted api d2 individual/147",
     "https://ncaa-api.henrygd.me/stats/basketball-men/d2/current/individual/147", {}, "\"data\""),
    ("jina html of stats.ncaa.org/teams/history",
     "https://r.jina.ai/https://stats.ncaa.org/teams/history",
     {"X-Return-Format": "html"}, "org_id_select"),
    ("jina html of ncaa.com d2 scoring",
     "https://r.jina.ai/https://www.ncaa.com/stats/basketball-men/d2/current/individual/147",
     {"X-Return-Format": "html"}, "<table"),
    ("allorigins raw stats.ncaa.org/teams/history",
     "https://api.allorigins.win/raw?url=" + quote("https://stats.ncaa.org/teams/history", safe=""),
     {}, "org_id_select"),
]


def main():
    for label, url, extra, marker in CANDIDATES:
        h = {**UA, **extra}
        try:
            r = requests.get(url, headers=h, timeout=(10, 60))
            body = r.text or ""
            has = marker in body
            low = body.lower()
            blocked = any(s in low for s in (
                "access denied", "request could not be satisfied", "are you a robot",
                "captcha", "cloudflare", "incapsula", "forbidden", "just a moment"))
            print(f"\n=== {label}")
            print(f"    HTTP {r.status_code}, {len(body)} bytes, marker[{marker!r}]={has}, blockish={blocked}")
            print(f"    head: {body[:240].replace(chr(10),' ')!r}")
        except Exception as exc:
            print(f"\n=== {label}\n    ERROR {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
