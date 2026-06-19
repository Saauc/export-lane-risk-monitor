"""
risk_model.py
=============
Phase 2 — the scoring engine.

This module turns the raw numbers from data_sources.py into a single 0-100
RISK SCORE per lane (higher = more risk), plus a transparent breakdown of how it
got there. It deliberately holds NO networking code — it asks data_sources for
data and focuses purely on the math, so the logic is easy to read and test.

THE CORE PROBLEM this file solves:
    We have three signals in totally different units —
        * FX volatility  (a % wobble)
        * shipping cost   (dollars)
        * news sentiment  (a mood)
    To combine them we must put them on ONE comparable scale. We do that with
    z-scores (distance from a baseline, measured in standard deviations) fed
    through a sigmoid that squashes any z into 0-100, where z=0 -> 50 (neutral).

WHY z-SCORES (and not fixed thresholds):
    MXN naturally swings more than EUR. A fixed "volatility above X = risky"
    rule would flag Mexico every single day and tell us nothing. A z-score asks
    instead: "is this lane unusual *relative to its own recent normal*?" — which
    is the question a risk analyst actually cares about. It also makes the
    "cost vs 90-day average" requirement literal: that average IS the baseline.

Everything tunable (weights, windows, the news lexicon, risk bands) lives in
config.json so this code stays declarative about WHAT, not magic about HOW MUCH.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from datetime import date
from pathlib import Path

import chokepoints
import data_sources
import landed_cost
import storage

CONFIG_PATH = Path(__file__).with_name("config.json")


def load_config(path: Path | str = CONFIG_PATH) -> dict:
    """Read config.json once and hand back the dict of knobs."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Small math helpers — the shared toolkit every sub-score uses.
# ---------------------------------------------------------------------------
def _sigmoid_0_100(z: float, k: float, clamp: float) -> float:
    """
    Squash a z-score into a 0-100 risk sub-score.

        score = 100 / (1 + e^(-k*z))

    Properties that make this a sensible mapping:
      * z = 0  (exactly average)        -> 50   (neutral risk)
      * z > 0  (worse than normal)      -> >50  (rising toward 100)
      * z < 0  (better than normal)     -> <50  (falling toward 0)
    We clamp z first so a near-flat baseline (tiny std) can't manufacture a
    z of 40 and peg the score at 100 on noise.
    """
    z = max(-clamp, min(clamp, z))
    return 100.0 / (1.0 + math.exp(-k * z))


def _zscore(value: float, sample: list[float]) -> float:
    """
    How many standard deviations `value` sits above/below the mean of `sample`.
    Returns 0.0 if we don't have enough data or the sample is perfectly flat —
    i.e. "no evidence of abnormality", which maps to neutral 50 downstream.
    """
    if len(sample) < 2:
        return 0.0
    mean = statistics.mean(sample)
    std = statistics.pstdev(sample)  # population std: we treat the window as the whole population
    if std == 0:
        return 0.0
    return (value - mean) / std


def _sorted_values(series: dict[str, float]) -> list[float]:
    """Take a {date: value} dict and return its values in chronological order."""
    return [series[d] for d in sorted(series)]


# ---------------------------------------------------------------------------
# Sub-score 1 — FX VOLATILITY (40%)
# ---------------------------------------------------------------------------
def fx_volatility_subscore(ccy_series: dict[str, float], cfg: dict) -> dict:
    """
    Score how turbulent a currency has been lately, relative to its own recent
    norm. Turbulent FX = uncertain margins on cross-border trade = higher risk.

    Steps:
      1. Convert the price series to DAILY LOG RETURNS  r_t = ln(P_t / P_t-1).
         Returns (not raw prices) are what 'volatility' is measured on.
      2. Take a ROLLING std-dev of those returns over `rolling_window` days.
         That rolling series is the lane's volatility through time.
      3. The latest rolling vol is the 'current' reading; z-score it against the
         whole distribution of rolling vols -> is today calm or stormy *for this
         currency*? Map that z through the sigmoid.

    Returns a dict with the sub-score plus the raw numbers for transparency.
    """
    window = cfg["fx_volatility"]["rolling_window"]
    k = cfg["scoring"]["sigmoid_k"]
    clamp = cfg["scoring"]["z_clamp"]

    prices = _sorted_values(ccy_series)
    if len(prices) < window + 2:
        # Not enough history to measure volatility meaningfully -> neutral 50.
        return {"score": 50.0, "current_vol": None, "note": "insufficient FX history"}

    # 1. daily log returns
    returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]

    # 2. rolling-window volatility (std of returns in each trailing window)
    rolling_vols = [
        statistics.pstdev(returns[i - window:i])
        for i in range(window, len(returns) + 1)
    ]

    current_vol = rolling_vols[-1]
    # 3. z-score the latest vol against the full set of rolling vols
    z = _zscore(current_vol, rolling_vols)
    score = _sigmoid_0_100(z, k, clamp)

    return {
        "score": round(score, 1),
        # annualised % vol is the human-readable version (252 trading days)
        "current_vol_annualized_pct": round(current_vol * math.sqrt(252) * 100, 2),
        "z": round(z, 2),
    }


# ---------------------------------------------------------------------------
# Sub-score 2 — SHIPPING COST (35%) = freight (primary) + oil (secondary)
# ---------------------------------------------------------------------------
def _cost_z(series: dict[str, float], lookback: int) -> tuple[float, float | None]:
    """
    Z-score the latest value of a price series against its trailing `lookback`
    window — literally 'current cost vs the recent average'. Returns (z, latest).
    A positive z means 'pricier than usual' -> upward pressure on risk.
    """
    values = _sorted_values(series)
    if len(values) < 5:
        return 0.0, (values[-1] if values else None)
    baseline = values[-lookback:]
    latest = baseline[-1]
    return _zscore(latest, baseline), latest


def fuel_cost_subscore(oil_series: dict, cfg: dict) -> dict:
    """
    Score fuel (bunker) cost pressure from Brent crude, z-scored vs its trailing
    average and mapped to 0-100.

    NOTE: oil is a GLOBAL price, so this sub-score is the same for every lane by
    design — it shifts all lanes together rather than discriminating between
    them. (We deliberately do NOT use a dry-bulk index like BDRY as a stand-in
    for container freight here; container indices such as Drewry WCI / Freightos
    FBX are the right instrument but aren't freely available via API. Lane
    differentiation comes from FX, news and chokepoint exposure instead.)
    """
    lookback = cfg["fuel_cost"]["lookback_days"]
    k = cfg["scoring"]["sigmoid_k"]
    clamp = cfg["scoring"]["z_clamp"]

    if not oil_series:
        return {"score": 50.0, "note": "no oil data"}

    oz, latest = _cost_z(oil_series, lookback)
    return {"score": round(_sigmoid_0_100(oz, k, clamp), 1),
            "z": round(oz, 2), "latest": latest}


# ---------------------------------------------------------------------------
# Sub-score 3 — NEWS SENTIMENT (25%)
# ---------------------------------------------------------------------------
def news_sentiment_subscore(headlines: list[dict], cfg: dict) -> dict:
    """
    Turn a batch of headlines into a risk sub-score using a TRANSPARENT keyword
    lexicon (defined in config.json, so it's auditable — no hidden ML box).

    Method:
      * For every headline, count keyword matches and take net = (pos - neg),
        so calm/good news is POSITIVE and risky news is NEGATIVE. (Getting this
        sign right matters: tariff/conflict headlines must push sentiment DOWN.)
      * Average net across headlines, divide by a scale so a typical 1-2 net
        words doesn't instantly peg tanh, then squash to a sentiment in [-1, 1].
      * Map sentiment to 0-100:  score = 50 - 50 * sentiment
            sentiment = -1 (all bad)   -> 100
            sentiment =  0 (neutral)   -> 50
            sentiment = +1 (all good)  -> 0

    NOTE / honesty: news is a SNAPSHOT — these sources only return current
    headlines, not a 30-day daily history (unlike FX/oil/freight). So this
    sub-score reflects 'today's mood'. That's exactly why storage.py persists it
    each day: so a real news-sentiment history accumulates for later validation.
    """
    neg_words = [w.lower() for w in cfg["news_lexicon"]["negative"]]
    pos_words = [w.lower() for w in cfg["news_lexicon"]["positive"]]

    if not headlines:
        return {"score": 50.0, "sentiment": 0.0, "headline_count": 0,
                "note": "no headlines"}

    scale = cfg["scoring"]["news_sentiment_scale"]

    def count_words(text: str, words: list[str]) -> int:
        # Word-boundary match so "war" doesn't fire inside "toward"/"forward"
        # and "ban" doesn't fire inside "urban". Substring matching (the old
        # approach) silently inflated the sentiment with false hits.
        return sum(len(re.findall(rf"\b{re.escape(w)}\b", text)) for w in words)

    nets = []
    for h in headlines:
        title = (h.get("title") or "").lower()
        neg = count_words(title, neg_words)
        pos = count_words(title, pos_words)
        nets.append(pos - neg)              # POSITIVE = calm/good, NEGATIVE = risky

    avg_net = statistics.mean(nets)
    sentiment = math.tanh(avg_net / scale)  # squash to [-1, 1]; negative = risky
    score = 50.0 - 50.0 * sentiment         # invert: bad news -> high score

    return {
        "score": round(score, 1),
        "sentiment": round(sentiment, 3),   # -1 risky .. +1 calm
        "headline_count": len(headlines),
    }


# ---------------------------------------------------------------------------
# Combine the three into one lane score
# ---------------------------------------------------------------------------
def _band_for(score: float, cfg: dict) -> dict:
    """Map a 0-100 score to its labelled risk band (Low/Moderate/Elevated/High)."""
    for band in cfg["risk_bands"]:
        if score <= band["max"]:
            return band
    return cfg["risk_bands"][-1]


def score_lane(lane_key: str, bundle: dict, cfg: dict,
               tensions: dict[str, int] | None = None) -> dict:
    """
    Produce the full risk result for ONE lane from a fetched `bundle`
    (data_sources.fetch_all() output) and the config, under the given chokepoint
    `tensions` (defaults to the baseline climate if not supplied).

    Final score = weighted sum of FOUR sub-scores (FX volatility, shipping cost,
    news sentiment, chokepoint exposure), using config weights.
    """
    currency = data_sources.LANES[lane_key]["currency"]
    weights = cfg["weights"]
    if tensions is None:
        tensions = chokepoints.resolve_tensions(cfg, "baseline")

    # Pull the relevant slices out of the bundle, tolerating dead sources.
    fx_series = bundle["fx"]["series"].get(currency, {}) if bundle["fx"]["ok"] else {}
    oil_series = bundle["oil"]["series"] if bundle["oil"]["ok"] else {}
    news = bundle["news"].get(lane_key, {})
    headlines = news.get("headlines", []) if news.get("ok") else []

    # Four independent sub-scores.
    fx_sub = fx_volatility_subscore(fx_series, cfg)
    fuel_sub = fuel_cost_subscore(oil_series, cfg)
    news_sub = news_sentiment_subscore(headlines, cfg)
    choke_sub = chokepoints.lane_chokepoint_subscore(lane_key, tensions)

    # Weighted blend into the headline 0-100 score.
    final = (
        weights["fx_volatility"] * fx_sub["score"]
        + weights["fuel_cost"] * fuel_sub["score"]
        + weights["news_sentiment"] * news_sub["score"]
        + weights["chokepoint"] * choke_sub["score"]
    )
    final = round(final, 1)
    band = _band_for(final, cfg)

    # Latest raw metrics, for the snapshot row + dashboard cards.
    latest_fx = _sorted_values(fx_series)[-1] if fx_series else None
    latest_oil = _sorted_values(oil_series)[-1] if oil_series else None

    # The money model: estimated EUR landed cost under the active tensions.
    landed = landed_cost.compute_landed_cost(
        lane_key, oil_series, fx_series, cfg, tensions
    )

    return {
        "lane": lane_key,
        "label": data_sources.LANES[lane_key]["label"],
        "market": data_sources.LANES[lane_key]["market"],
        "policy": data_sources.LANES[lane_key]["policy"],
        "currency": currency,
        "landed": landed,
        "risk_score": final,
        "band": band["label"],
        "color": band["color"],
        "subscores": {
            "fx_volatility": fx_sub,
            "fuel_cost": fuel_sub,
            "news_sentiment": news_sub,
            "chokepoint": choke_sub,
        },
        "metrics": {
            "fx_rate": latest_fx,
            "oil_price": latest_oil,
            "container_freight_eur": landed["breakdown"]["freight"],
            "news_sentiment": news_sub["sentiment"],
        },
    }


def score_all_lanes(bundle: dict | None = None, persist: bool = True,
                    scenario: str = "baseline") -> dict:
    """
    Score every lane under the given `scenario` (a key into config.scenarios that
    sets chokepoint tensions) and optionally persist today's snapshot.

    Pass your own `bundle` to score offline/with cached data; otherwise it
    fetches live. We request extra FX history (120 days) because volatility math
    needs a baseline longer than the 30-day window itself. We only persist the
    'baseline' scenario — hypothetical stress runs shouldn't pollute the history.
    """
    cfg = load_config()
    tensions = chokepoints.resolve_tensions(cfg, scenario)

    if bundle is None:
        bundle = {
            "fx": data_sources.fetch_fx_rates(days=120),  # long history for vol baseline
            "oil": data_sources.fetch_oil_prices(days=90),
            "news": {lane: data_sources.fetch_news(lane) for lane in data_sources.LANES},
        }

    results = {lane: score_lane(lane, bundle, cfg, tensions)
               for lane in data_sources.LANES}

    if persist and scenario == "baseline":
        today = date.today().isoformat()
        with storage.get_connection() as conn:
            # First make sure the market history is in the DB (idempotent upsert),
            # then write today's sentiment + final score onto today's rows.
            storage.backfill_market_history(conn, bundle)
            for lane, r in results.items():
                storage.save_snapshot(
                    conn,
                    snapshot_date=today,
                    lane=lane,
                    fx_rate=r["metrics"]["fx_rate"],
                    oil_price=r["metrics"]["oil_price"],
                    news_sentiment=r["metrics"]["news_sentiment"],
                    risk_score=r["risk_score"],
                    components=r["subscores"],
                )

    return results


if __name__ == "__main__":
    # Smoke test: score all lanes live and print a readable breakdown.
    print("=" * 64)
    print("Trade Lane Risk Monitor — Phase 2 risk model")
    print("=" * 64)

    results = score_all_lanes()
    for lane, r in results.items():
        print(f"\n{r['label']}  ({r['currency']})")
        print(f"   RISK SCORE: {r['risk_score']:>5}  [{r['band']}]")
        s = r["subscores"]
        print(f"   - FX volatility (30%): {s['fx_volatility']['score']:>5}"
              f"   (z={s['fx_volatility'].get('z')}, "
              f"ann.vol={s['fx_volatility'].get('current_vol_annualized_pct')}%)")
        print(f"   - Fuel cost    (20%): {s['fuel_cost']['score']:>5}"
              f"   (Brent z={s['fuel_cost'].get('z')})")
        print(f"   - News sentiment(20%): {s['news_sentiment']['score']:>5}"
              f"   (sentiment={s['news_sentiment']['sentiment']}, "
              f"n={s['news_sentiment']['headline_count']})")
        print(f"   - Chokepoint   (30%): {s['chokepoint']['score']:>5}"
              f"   (corridor={','.join(s['chokepoint'].get('corridor', [])) or '-'})")
        m = r["metrics"]
        print(f"   metrics: 1EUR={m['fx_rate']} {r['currency']}, "
              f"Brent=${m['oil_price']}, container≈€{m['container_freight_eur']}")

    print("\nSnapshots written to trade_lane.db")
