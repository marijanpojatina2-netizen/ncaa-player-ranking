"""Shared ingestion helpers: cached + rate-limited HTTP client, normalization,
and small parsing utilities.

Scraping etiquette (REQUIRED by project brief):
  * Every raw response is cached to data/cache/ keyed by URL+params.
  * Cache is reused unless refresh=True (CLI --refresh).
  * Requests are rate-limited (>= MIN_DELAY seconds apart), send a real
    User-Agent, and retry with exponential backoff.
  * We do NOT bulk-scrape sports-reference.com.
"""
from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlencode

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "data" / "cache"

# Polite defaults.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 ncaa-scouting-tool/1.0 (personal scouting use)"
)
MIN_DELAY = 1.5          # seconds between live requests
MAX_RETRIES = 4
TIMEOUT = 30

# barttorvik.com intermittently blocks datacenter IPs (e.g. GitHub-hosted
# runners) with HTTP 403. When that happens we route ONLY barttorvik requests
# through a server-side "read" proxy that fetches the page from its own IP and
# returns the body. Configure via the FETCH_PROXIES env var: a space-separated
# list of templates tried in order. Use "{url}" for a URL-encoded target or
# "{rawurl}" for the raw target. Empty (default) -> always fetch directly.
PROXY_HOST = "barttorvik.com"
PROXY_TEMPLATES = [t for t in os.environ.get("FETCH_PROXIES", "").split() if t]

_last_request_ts = 0.0


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cache_key(url: str, params: Optional[dict]) -> Path:
    raw = url
    if params:
        raw += "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    # Keep a human hint in the filename.
    host = url.split("//")[-1].split("/")[0].replace(":", "_")
    return CACHE_DIR / f"{host}_{h}.cache"


def _build_target(url: str, params: Optional[dict]) -> str:
    """Fold query params into the URL (so it can be handed to a proxy)."""
    if not params:
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + urlencode(params)


def _proxied(template: str, target: str) -> str:
    if "{rawurl}" in template:
        return template.replace("{rawurl}", target)
    return template.replace("{url}", quote(target, safe=""))


def _looks_like_csv(text: str) -> bool:
    """Reject HTML/error/markdown bodies so a bad proxy falls through to the
    next candidate (and we never cache a CloudFront block page as 'data')."""
    head = text.lstrip()[:1500].lower()
    if head.startswith("<") or "<html" in head or "<!doctype" in head:
        return False
    if "request could not be satisfied" in head or "access denied" in head:
        return False
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    return first.count(",") >= 3


def fetch(
    url: str,
    params: Optional[dict] = None,
    *,
    refresh: bool = False,
    min_delay: float = MIN_DELAY,
    headers: Optional[dict] = None,
) -> str:
    """Fetch a URL with on-disk caching, rate-limiting and backoff.

    Returns the response body as text. Raises RuntimeError on persistent
    failure (after retries) when no cache is available. barttorvik requests are
    routed through FETCH_PROXIES (if configured) to dodge datacenter-IP blocks.
    """
    global _last_request_ts
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _cache_key(url, params)

    if cache_file.exists() and not refresh:
        return cache_file.read_text(encoding="utf-8", errors="replace")

    target = _build_target(url, params)
    use_proxy = bool(PROXY_TEMPLATES) and PROXY_HOST in url
    if use_proxy:
        candidates = [_proxied(t, target) for t in PROXY_TEMPLATES]
        timeout = max(TIMEOUT, 60)  # read proxies can be slow
    else:
        candidates = [target]
        timeout = TIMEOUT

    req_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if use_proxy:
        # Ask r.jina.ai for the raw page text instead of reformatted markdown
        # (harmless to the other proxies, which ignore it).
        req_headers["X-Return-Format"] = "text"
    if headers:
        req_headers.update(headers)

    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        for cand in candidates:
            # Rate-limit live requests.
            elapsed = time.time() - _last_request_ts
            if elapsed < min_delay:
                time.sleep(min_delay - elapsed)
            try:
                resp = requests.get(cand, headers=req_headers, timeout=timeout)
                _last_request_ts = time.time()
                resp.raise_for_status()
                text = resp.text
                if not text.strip():
                    raise RuntimeError("empty response body")
                if use_proxy and not _looks_like_csv(text):
                    raise RuntimeError("non-CSV/blocked response (proxy)")
                cache_file.write_text(text, encoding="utf-8")
                return text
            except Exception as exc:  # network error, HTTP error, timeout
                last_exc = exc
                _last_request_ts = time.time()
                print(f"  [fetch] {cand[:90]} failed (attempt {attempt + 1}/{MAX_RETRIES}): {exc}")
        wait = 2 ** (attempt + 1)  # 2,4,8,16
        time.sleep(wait)

    # Fall back to stale cache if we have any.
    if cache_file.exists():
        print(f"  [fetch] using stale cache for {url} after failures")
        return cache_file.read_text(encoding="utf-8", errors="replace")
    raise RuntimeError(f"Failed to fetch {url} after {MAX_RETRIES} attempts") from last_exc


# ---------------------------------------------------------------------------
# Parsing / normalization helpers
# ---------------------------------------------------------------------------
def to_float(value, default=None):
    """Best-effort float parse; returns default for blanks / '-' / non-numeric."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("%", "").replace(",", "")
    if s in ("", "-", "--", "N/A", "NA", "null", "None"):
        return default
    try:
        return float(s)
    except ValueError:
        return default


# Map various class/year spellings to canonical Fr/So/Jr/Sr.
_CLASS_MAP = {
    "fr": "Fr", "freshman": "Fr", "fresh": "Fr", "1": "Fr",
    "so": "So", "sophomore": "So", "soph": "So", "2": "So",
    "jr": "Jr", "junior": "Jr", "3": "Jr",
    "sr": "Sr", "senior": "Sr", "4": "Sr",
    "gr": "Sr", "grad": "Sr", "graduate": "Sr", "5": "Sr",  # treat grad as senior class for scouting
}


def normalize_class(value) -> Optional[str]:
    if value is None:
        return None
    key = str(value).strip().lower().rstrip(".")
    return _CLASS_MAP.get(key)


def parse_height_to_inches(value) -> Optional[float]:
    """Parse heights like '6-5', "6'5\"", '6 ft 5', or an inches int -> inches."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Already inches if plausible; ignore tiny/garbage values.
        return float(value) if value > 30 else None
    s = str(value).strip().lower().replace('"', "").replace("ft", "-").replace("'", "-")
    s = s.replace(" ", "")
    if not s or s in ("-", "--"):
        return None
    parts = [p for p in s.split("-") if p != ""]
    try:
        if len(parts) == 2:
            feet, inches = float(parts[0]), float(parts[1])
            return feet * 12 + inches
        if len(parts) == 1:
            v = float(parts[0])
            return v if v > 30 else v * 12  # bare number: treat <30 as feet
    except ValueError:
        return None
    return None


def minmax_normalize(values: list[float]) -> list[float]:
    """Min-max normalize to 0..1. Constant input -> all 0.5."""
    nums = [v for v in values if v is not None]
    if not nums:
        return [None for _ in values]
    lo, hi = min(nums), max(nums)
    if hi == lo:
        return [0.5 if v is not None else None for v in values]
    return [None if v is None else (v - lo) / (hi - lo) for v in values]
