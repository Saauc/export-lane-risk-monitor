"""
data_sources.py
===============
Phase 1 of the Trade Lane Risk Monitor.

This module is the project's "data layer". Its only job is to GO OUT to the
internet, FETCH raw numbers/headlines from free public APIs, and hand them back
as clean Python dictionaries. It does NOT score risk — that is Phase 2's job
(risk_model.py). Keeping fetching and scoring separate means that if an API
changes or goes down, we only have to touch this one file.

Modeled on a Spanish specialty-coatings exporter. The exporter's costs are
in EUR; it sells into three separate North American markets, each in its own
currency. We monitor those three export lanes out of Barcelona:
    - Barcelona -> US (Miami)        revenue currency: USD
    - Barcelona -> Mexico (Veracruz) revenue currency: MXN
    - Barcelona -> Canada (Montreal) revenue currency: CAD

FX risk is therefore EUR vs each destination currency (EUR/USD, EUR/MXN,
EUR/CAD): when the destination currency weakens against the euro, the exporter's
EUR-denominated margin on that market shrinks.

Each external source lives in its own function with its own try/except, so one
dead API never takes down the whole program — the function just returns a
structured "error" payload and the others keep working.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import feedparser
import requests
from dotenv import load_dotenv

# Load variables from a local .env file into os.environ. If .env doesn't exist
# this silently does nothing, which is exactly what we want — the project must
# still run with zero configuration.
load_dotenv()

# A single shared timeout (seconds) so no request can hang the program forever.
REQUEST_TIMEOUT = 15

# The three export markets, each mapped to its revenue currency and the
# prevailing EU trade-policy regime for that market. Defined here once so the
# whole project (scoring, dashboard, briefing) shares one source of truth.
LANES = {
    "US": {"label": "US · Miami",       "currency": "USD", "market": "United States",
           "policy": "10% US tariff (Section 122, expires ~Jul 2026)",
           "chokepoints": ["Gibraltar"]},
    "MX": {"label": "Mexico · Veracruz", "currency": "MXN", "market": "Mexico",
           "policy": "EU–Mexico Global Agreement (modernised)",
           "chokepoints": ["Gibraltar"]},
    "CA": {"label": "Canada · Montreal", "currency": "CAD", "market": "Canada",
           "policy": "CETA (near tariff-free)",
           "chokepoints": ["Gibraltar"]},
    "UK": {"label": "UK · Felixstowe",   "currency": "GBP", "market": "United Kingdom",
           "policy": "EU–UK TCA (tariff-free, customs friction)",
           "chokepoints": ["Gibraltar"]},
    "BR": {"label": "Brazil · Santos",   "currency": "BRL", "market": "Brazil",
           "policy": "EU–Mercosur (pending ratification)",
           "chokepoints": ["Gibraltar"]},
    "AU": {"label": "Australia · Sydney", "currency": "AUD", "market": "Australia",
           "policy": "No EU FTA (MFN tariffs)",
           "chokepoints": ["Suez Canal", "Bab-el-Mandeb"]},
    "CN": {"label": "China · Shanghai",  "currency": "CNY", "market": "China",
           "policy": "EU–China MFN (trade tensions)",
           "chokepoints": ["Suez Canal", "Bab-el-Mandeb", "Malacca"]},
}


# ---------------------------------------------------------------------------
# 1. FX RATES  (frankfurter.app)
# ---------------------------------------------------------------------------
def fetch_fx_rates(days: int = 30) -> dict:
    """
    Fetch a daily time series of exchange rates for USD, MXN and CAD, all
    quoted against the euro (base=EUR), for roughly the last `days` calendar
    days — i.e. how many USD/MXN/CAD one euro buys.

    Why a *time series* and not just today's rate? Phase 2 scores "30-day FX
    volatility", so it needs a history of rates, not a single snapshot. Pulling
    the whole series here in one call is cheaper than 30 separate requests.

    API: https://www.frankfurter.app  (free, no API key, backed by the European
    Central Bank's published reference rates).

    The endpoint shape we use is the date-range form:
        https://api.frankfurter.app/2024-01-01..2024-01-31?base=USD&symbols=EUR,CNY,MXN

    `base=USD` means "1 USD = X of each symbol", i.e. USD is the denominator.
    The ECB only publishes on business days, so weekends/holidays are simply
    absent from the response — that's normal and Phase 2 handles the gaps.

    Returns a dict like:
        {
          "ok": True,
          "base": "USD",
          "series": {
              "EUR": {"2024-01-01": 0.91, "2024-01-02": 0.90, ...},
              "CNY": {...},
              "MXN": {...},
          }
        }
    or, on failure:
        {"ok": False, "error": "<message>", "series": {}}
    """
    end = date.today()
    # Pad the start a little so weekends/holidays still leave us ~`days` of
    # actual business-day data points.
    start = end - timedelta(days=days + 10)

    url = f"https://api.frankfurter.app/{start.isoformat()}..{end.isoformat()}"
    params = {"base": "EUR", "symbols": "USD,MXN,CAD,GBP,BRL,AUD,CNY"}

    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()  # turn any 4xx/5xx HTTP status into an exception
        data = resp.json()

        # frankfurter returns {"rates": {"2024-01-01": {"EUR": 0.91, ...}, ...}}
        # i.e. keyed by DATE first. We "pivot" it to be keyed by CURRENCY first,
        # because Phase 2 wants one clean per-currency time series at a time.
        series: dict[str, dict[str, float]] = {
            "USD": {}, "MXN": {}, "CAD": {}, "GBP": {}, "BRL": {}, "AUD": {}, "CNY": {},
        }
        for day_str, rates in data.get("rates", {}).items():
            for ccy, value in rates.items():
                series[ccy][day_str] = value

        return {"ok": True, "base": "EUR", "series": series}

    except Exception as exc:  # network error, bad JSON, timeout, etc.
        # We never raise — we return a structured failure the caller can detect
        # via the "ok" flag, so a single dead source can't crash the app.
        return {"ok": False, "error": f"FX fetch failed: {exc}", "series": {}}


# ---------------------------------------------------------------------------
# 2. OIL / BRENT CRUDE  (EIA primary, Stooq fallback)
# ---------------------------------------------------------------------------
def fetch_oil_prices(days: int = 90) -> dict:
    """
    Fetch a daily history of Brent crude spot prices (USD/barrel).

    Oil is our stand-in for *freight/fuel cost*: when crude spikes, shipping
    and trucking surcharges follow, which raises landed cost on every lane.
    Phase 2 compares the latest price to its 90-day average, so we ask for ~90
    days of history.

    Two-tier strategy:
      * PRIMARY  — EIA (U.S. Energy Information Administration) open-data API.
        High quality, but needs a free API key in EIA_API_KEY. Series ID
        RBRTE = Europe Brent Spot Price FOB, Daily.
      * FALLBACK — Yahoo Finance chart API (symbol BZ=F = Brent crude futures).
        No key required, so the project still works for someone who never signs
        up for EIA.

    Returns:
        {"ok": True, "source": "EIA"|"Stooq", "series": {"YYYY-MM-DD": price, ...}}
        {"ok": False, "error": "...", "series": {}}
    """
    eia_key = os.getenv("EIA_API_KEY")

    # --- Tier 1: EIA, only attempted if the user supplied a key -------------
    if eia_key:
        try:
            url = "https://api.eia.gov/v2/petroleum/pri/spt/data/"
            params = {
                "api_key": eia_key,
                "frequency": "daily",
                "data[0]": "value",
                "facets[series][]": "RBRTE",   # Brent, daily
                "sort[0][column]": "period",
                "sort[0][direction]": "desc",  # newest first
                "length": days,                # cap rows returned
            }
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            rows = resp.json()["response"]["data"]
            series = {r["period"]: float(r["value"]) for r in rows if r.get("value")}
            if series:
                return {"ok": True, "source": "EIA", "series": series}
            # If EIA returned nothing useful we deliberately fall through to Stooq.
        except Exception:
            # Swallow and fall back — we'd rather get *some* data from Stooq than
            # fail just because EIA had a hiccup.
            pass

    # --- Tier 2: Yahoo Finance chart API, always available, no key ----------
    try:
        # BZ=F is the Yahoo ticker for Brent crude futures. The "chart" endpoint
        # returns JSON with two parallel arrays: `timestamp` (Unix seconds) and
        # `close` prices. We zip them back together into our {date: price} shape.
        # `range=3mo` ~= 90 days; a browser-like User-Agent avoids Yahoo's bot
        # rejection.
        url = "https://query1.finance.yahoo.com/v8/finance/chart/BZ=F"
        params = {"range": "3mo", "interval": "1d"}
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()

        result = resp.json()["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]

        series: dict[str, float] = {}
        for ts, close in zip(timestamps, closes):
            # Yahoo inserts `null` closes for partial/holiday sessions — skip them.
            if close is not None:
                day_str = date.fromtimestamp(ts).isoformat()
                series[day_str] = round(float(close), 2)

        # Keep only the most recent `days` data points.
        if series:
            recent = dict(sorted(series.items())[-days:])
            return {"ok": True, "source": "YahooFinance", "series": recent}

        return {"ok": False, "error": "Yahoo returned no parseable rows", "series": {}}

    except Exception as exc:
        return {"ok": False, "error": f"Oil fetch failed (EIA+Yahoo): {exc}", "series": {}}


# ---------------------------------------------------------------------------
# NOTE on freight: an earlier version pulled BDRY (Breakwave Dry Bulk Shipping
# ETF) as a "freight" signal. That was WRONG for this project — BDRY tracks
# DRY-BULK freight (iron ore, coal, grain in bulk carriers), whereas coatings
# ship in CONTAINERS. The correct instrument is a container index (Drewry WCI,
# Freightos FBX, SCFI), none of which is freely available via API. So container
# freight is modelled as lane-specific configured estimates in config.json, and
# the live cost driver we DO fetch is Brent crude (the real bunker-fuel input).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 4. NEWS HEADLINES PER LANE  (NewsAPI primary, Google News RSS fallback)
# ---------------------------------------------------------------------------

# Search terms per lane. These get fed to whichever news source we use. Kept
# here so it's obvious what each lane is "listening" for.
LANE_NEWS_QUERIES = {
    "US": "Spain US trade tariff exports",
    "MX": "Spain Mexico trade exports",
    "CA": "Canada EU CETA trade exports",
    "UK": "Spain UK trade exports Brexit",
    "BR": "Spain Brazil Mercosur trade exports",
    "AU": "Spain Australia trade exports shipping",
    "CN": "Spain China trade exports tariff",
}


def fetch_news(lane_key: str, limit: int = 12) -> dict:
    """
    Fetch recent news headlines relevant to one lane.

    Phase 2 runs sentiment analysis over these headlines (negative, scary news
    -> higher risk). We only need the headline text + a little metadata, so we
    keep each item small.

    Two-tier strategy, same philosophy as oil:
      * PRIMARY  — NewsAPI.org, if NEWSAPI_KEY is set (free dev tier).
      * FALLBACK — Google News RSS feed, which needs no key at all.

    `lane_key` must be one of the keys in LANES ("EU_US", "CN_US", "MX_US").

    Returns:
        {"ok": True, "source": "NewsAPI"|"GoogleNewsRSS",
         "headlines": [{"title": ..., "published": ..., "url": ...}, ...]}
        {"ok": False, "error": "...", "headlines": []}
    """
    if lane_key not in LANE_NEWS_QUERIES:
        return {"ok": False, "error": f"Unknown lane '{lane_key}'", "headlines": []}

    query = LANE_NEWS_QUERIES[lane_key]
    newsapi_key = os.getenv("NEWSAPI_KEY")

    # --- Tier 1: NewsAPI ----------------------------------------------------
    if newsapi_key:
        try:
            url = "https://newsapi.org/v2/everything"
            params = {
                "q": query,
                "language": "en",
                "sortBy": "publishedAt",  # most recent first
                "pageSize": limit,
                "apiKey": newsapi_key,
            }
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            articles = resp.json().get("articles", [])
            headlines = [
                {
                    "title": a.get("title", ""),
                    "published": a.get("publishedAt", ""),
                    "url": a.get("url", ""),
                }
                for a in articles
                if a.get("title")
            ]
            if headlines:
                return {"ok": True, "source": "NewsAPI", "headlines": headlines}
        except Exception:
            pass  # fall through to RSS

    # --- Tier 2: Google News RSS -------------------------------------------
    try:
        # Google News exposes a search-as-RSS endpoint. We URL-encode the query
        # via requests' params builder, then let feedparser handle the XML.
        rss_url = "https://news.google.com/rss/search"
        # feedparser can take a full URL; build it with requests so the query is
        # safely escaped.
        prepared = requests.Request(
            "GET",
            rss_url,
            params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
        ).prepare()

        feed = feedparser.parse(prepared.url)
        headlines = [
            {
                "title": entry.get("title", ""),
                "published": entry.get("published", ""),
                "url": entry.get("link", ""),
            }
            for entry in feed.entries[:limit]
            if entry.get("title")
        ]
        if headlines:
            return {"ok": True, "source": "GoogleNewsRSS", "headlines": headlines}

        return {"ok": False, "error": "RSS feed returned no entries", "headlines": []}

    except Exception as exc:
        return {"ok": False, "error": f"News fetch failed: {exc}", "headlines": []}


# ---------------------------------------------------------------------------
# Convenience aggregator + manual smoke test
# ---------------------------------------------------------------------------
def fetch_all() -> dict:
    """
    Pull everything in one call. Phase 2/3 will use this as the single entry
    point so they don't have to know about individual sources.
    """
    return {
        "fx": fetch_fx_rates(),
        "oil": fetch_oil_prices(),
        "news": {lane: fetch_news(lane) for lane in LANES},
    }


if __name__ == "__main__":
    # Running `python data_sources.py` directly executes this block. It's a
    # human-readable smoke test that prints whether each source responded —
    # handy for confirming Phase 1 works before we build anything on top.
    print("=" * 60)
    print("Trade Lane Risk Monitor — Phase 1 data source smoke test")
    print("=" * 60)

    fx = fetch_fx_rates()
    print(f"\n[FX]  ok={fx['ok']}", "" if fx["ok"] else f"({fx.get('error')})")
    if fx["ok"]:
        for ccy, ser in fx["series"].items():
            if ser:
                latest_day = max(ser)
                print(f"      {ccy}/USD  latest {latest_day} = {ser[latest_day]:.4f}"
                      f"   ({len(ser)} days of history)")

    oil = fetch_oil_prices()
    print(f"\n[OIL] ok={oil['ok']}", "" if oil["ok"] else f"({oil.get('error')})")
    if oil["ok"]:
        latest_day = max(oil["series"])
        print(f"      source={oil['source']}  Brent latest {latest_day} = "
              f"${oil['series'][latest_day]:.2f}/bbl   "
              f"({len(oil['series'])} days of history)")

    print("\n[NEWS]")
    for lane, meta in LANES.items():
        news = fetch_news(lane)
        print(f"  {meta['label']:<12} ok={news['ok']}  source={news.get('source','-')}")
        if news["ok"]:
            for h in news["headlines"][:3]:
                print(f"      - {h['title'][:80]}")

    print("\nDone.")
