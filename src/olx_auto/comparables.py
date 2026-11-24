"""OLX comparables (spec §7): asking prices of similar adverts from the public olx.bg search.

The partner API can't search other people's adverts, so this reads the search page a browser gets.
The page embeds its results as JSON (`window.__PRERENDERED_STATE__`), which is parsed instead of
the HTML. Scraping is fragile, so it's isolated here: a changed page raises ComparablesError, the
agent gets that as a tool error and prices without comparables. Standard library only.

robots.txt allows path-style searches (`/q-<words>/`) but not `/api/` or `?q=` (spike-notes.md).
"""
import json
import os
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

BASE = "https://www.olx.bg"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
STATE_RE = re.compile(r'window\.__PRERENDERED_STATE__\s*=\s*("(?:[^"\\]|\\.)*")')
CODE_RE = re.compile(r"^[a-z0-9_]+$")
PATHS_MAX_AGE = 7 * 24 * 3600  # category id → URL path cache; every search page refreshes it
CONDITION_ATTRIBUTES = ("state", "condition")


class ComparablesError(Exception):
    pass


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html",
                                               "Accept-Language": "bg,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise ComparablesError(f"olx.bg search answered HTTP {e.code}") from None
    except OSError as e:
        raise ComparablesError(f"olx.bg search failed: {e}") from None


def parse_state(html: str) -> dict:
    m = STATE_RE.search(html)
    if not m:
        raise ComparablesError("olx.bg search page changed: no embedded results")
    try:
        return json.loads(json.loads(m.group(1)))
    except ValueError:
        raise ComparablesError("olx.bg search page changed: unreadable embedded results") from None


def search_url(query: str, category_path: str = "", filters: dict | None = None) -> str:
    """`filters` is {attribute code: value code or [codes]}, as in a draft's attributes."""
    words = re.findall(r"\w+", query.lower())
    if not words:
        raise ComparablesError("empty search query")
    slug = "-".join(urllib.parse.quote(w) for w in words)
    url = f"{BASE}/{category_path or 'ads'}/q-{slug}/"
    params = []
    for code, value in (filters or {}).items():
        if not CODE_RE.match(str(code)):
            raise ComparablesError(f"bad filter code {code!r}")
        for i, v in enumerate(value if isinstance(value, list) else [value]):
            params.append((f"search[filter_enum_{code}][{i}]", str(v)))
    return url + ("?" + urllib.parse.urlencode(params) if params else "")


def _item(ad: dict) -> dict:
    price = (ad.get("price") or {}).get("regularPrice") or {}
    condition = next((p.get("normalizedValue") for p in ad.get("params") or []
                      if p.get("key") in CONDITION_ATTRIBUTES), None)
    url = ad.get("url") or ""
    return {
        "title": ad.get("title") or "",
        "price": price.get("value") if price.get("currencyCode") == "EUR" else None,
        "negotiable": bool(price.get("negotiable")),
        "condition": condition if isinstance(condition, str) else None,
        "url": url if url.startswith(BASE + "/") else None,  # only ever link to olx.bg
        "promoted": bool(ad.get("isPromoted")),
        "business": bool(ad.get("isBusiness")),
        "city": (ad.get("location") or {}).get("cityName"),
        "created": (ad.get("createdTime") or "")[:10],
    }


def summary(items: list[dict]) -> dict:
    prices = sorted(i["price"] for i in items if i["price"])
    if not prices:
        return {"n": 0}
    low, _, high = statistics.quantiles(prices, n=4, method="inclusive") if len(prices) > 1 else prices * 3
    return {"n": len(prices), "median": round(statistics.median(prices), 2), "low": round(low, 2),
            "high": round(high, 2), "min": prices[0], "max": prices[-1]}


class Search:
    """Searches olx.bg. `cache_file` keeps the category id → URL path map between runs."""

    def __init__(self, cache_file: Path, get: Callable[[str], str] = fetch):
        self.cache_file, self.get = cache_file, get

    def _save_paths(self, state: dict) -> dict[str, str]:
        cats = (state.get("categories") or {}).get("list") or {}
        paths = {str(c["id"]): c["path"] for c in cats.values() if c.get("id") and c.get("path")}
        if paths:
            tmp = self.cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(paths))
            os.replace(tmp, self.cache_file)
        return paths

    def category_path(self, category_id: int) -> str:
        paths = {}
        if self.cache_file.exists() and time.time() - self.cache_file.stat().st_mtime < PATHS_MAX_AGE:
            paths = json.loads(self.cache_file.read_text())
        if str(category_id) not in paths:
            paths = self._save_paths(parse_state(self.get(f"{BASE}/ads/")))
        if str(category_id) not in paths:
            raise ComparablesError(f"olx.bg has no search page for category {category_id}")
        return paths[str(category_id)]

    def search(self, query: str, category_id: int | None = None, filters: dict | None = None) -> dict:
        """One results page (about 40 adverts), newest promoted ones included."""
        url = search_url(query, self.category_path(category_id) if category_id else "", filters)
        state = parse_state(self.get(url))
        self._save_paths(state)
        try:
            listing = state["listing"]["listing"]
            items = [_item(ad) for ad in listing["ads"]]
        except (KeyError, TypeError):
            raise ComparablesError("olx.bg search page changed: no adverts in the results") from None
        return {"query": query, "category_id": category_id, "filters": filters or {}, "url": url,
                "total": listing.get("totalElements"), "summary": summary(items), "items": items,
                "at": datetime.now(UTC).isoformat(timespec="seconds")}
