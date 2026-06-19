"""
landed_cost.py
==============
The "money model" — the piece that turns an abstract 0-100 risk score into a
number a decision-maker actually cares about: roughly how many EUROS it costs to
land one container of coatings in each market, and therefore which market is
currently cheapest to serve.

A risk score says "the US looks stressed." A landed cost says "the US container
costs €4,000 more than Canada's, almost all of it tariff." The second sentence
is the one that wins an interview, because it's concrete and it drives a
decision.

WHAT IT IS (and isn't):
    It's a transparent, directional ESTIMATE built from configurable placeholders
    (config.json -> landed_cost), not a freight quote. Every input is visible and
    every figure is tunable. That honesty is deliberate — see the README.

THE COMPONENTS (all in EUR, for one representative container):
    goods value        fixed cargo value (FOB)
  + container freight  lane-specific configured estimate (Drewry/Freightos in prod)
  + bunker surcharge   base fuel cost per lane, SCALED by live Brent crude
  + duty / tariff      goods value x the market's tariff rate (US 10%, CETA ~0%)
  + feedstock surcharge rises with Hormuz tension (oil-derived input cost)
  + Cape reroute       added when a corridor chokepoint is too disrupted to transit
  + handling/docs/ins.  fixed origin+destination overheads
  = total landed cost

We also surface an FX MARGIN signal: the exporter's costs are in EUR but it sells
in the destination currency, so if that currency weakened vs the euro over the
last 30 days, the EUR value of its revenue shrank — a margin headwind that is
separate from the landed cost itself.
"""

from __future__ import annotations

import chokepoints


def _latest(series: dict[str, float]) -> float | None:
    """Most recent value of a {date: value} series, or None if empty."""
    if not series:
        return None
    return series[max(series)]


def _fx_30d_change_pct(fx_series: dict[str, float]) -> float | None:
    """
    Percent change in the EUR/destination FX rate over ~30 data points.

    The series is destination-currency-per-EUR (e.g. USD per EUR). If that rises,
    the euro strengthened, so revenue earned in the destination currency converts
    to FEWER euros -> a margin HEADWIND. We return a signed % where POSITIVE means
    headwind (euro stronger), negative means tailwind.
    """
    if not fx_series or len(fx_series) < 2:
        return None
    values = [fx_series[d] for d in sorted(fx_series)]
    window = values[-30:] if len(values) >= 30 else values
    first, last = window[0], window[-1]
    if first == 0:
        return None
    return round((last - first) / first * 100, 2)


def landed_cost_from_values(
    lane_key: str,
    oil_price: float | None,
    cfg: dict,
    tensions: dict[str, int] | None = None,
) -> dict:
    """
    Core cost calculation for ONE lane from a SCALAR oil price, so it can be
    evaluated for any single day (today or a historical date in the backtest).

    Container freight is a lane-specific configured estimate (a real container
    index like Drewry WCI / Freightos FBX would go here in production — see
    config). The live element of cost is the bunker surcharge, which scales with
    Brent. `tensions` drives the stress effects:
      * bunker is multiplied by the Hormuz oil multiplier,
      * a feedstock surcharge appears as Hormuz tension lifts petroleum input cost,
      * a reroute (Cape diversion) premium is added per disrupted corridor chokepoint.
    Pass tensions=None for a pure market-only cost.
    """
    lc = cfg["landed_cost"]
    lane = lc["lanes"][lane_key]
    tensions = tensions or {}

    goods = lc["container_goods_value_eur"]
    handling = lc["handling_docs_insurance_eur"]
    base_oil = lc["baseline_oil"]

    # Lane-specific container freight estimate (NOT a dry-bulk index).
    freight = lane["container_freight_eur"]

    # Bunker surcharge scales with Brent vs its baseline, then with Hormuz tension.
    bunker = lane["base_bunker_eur"]
    if oil_price and base_oil:
        bunker = lane["base_bunker_eur"] * (oil_price / base_oil)
    hormuz_mult = chokepoints.hormuz_oil_multiplier(tensions, cfg)
    bunker *= hormuz_mult

    # Feedstock surcharge: Hormuz tension raises the petroleum input cost embedded
    # in the goods themselves (coatings are oil-derived).
    feedstock = goods * cfg["chokepoints"]["feedstock_oil_share"] * (hormuz_mult - 1.0)

    # Reroute premium: diverting around any badly disrupted chokepoint en route.
    reroute, hot = chokepoints.reroute_premium_eur(lane_key, tensions, cfg)

    # Duty: goods value x the market's tariff rate — trade policy as real money.
    tariff = goods * lane["tariff_rate"]
    total = goods + freight + bunker + tariff + handling + feedstock + reroute

    # Margin: sell_price_eur is the EUR-equivalent invoice price for this lane.
    # Gross margin = revenue - total landed cost. Configured per lane so the
    # commercial team can tune it independently of the cost model.
    sell_price = lane.get("sell_price_eur")
    if sell_price:
        margin_eur = round(sell_price - total)
        margin_pct = round(margin_eur / sell_price * 100, 1)
    else:
        margin_eur = margin_pct = None

    return {
        "total_eur": round(total),
        "breakdown": {
            "goods": round(goods),
            "freight": round(freight),
            "bunker": round(bunker),
            "tariff": round(tariff),
            "handling": round(handling),
            "feedstock": round(feedstock),
            "reroute": round(reroute),
        },
        "tariff_rate": lane["tariff_rate"],
        "reroute_chokepoints": hot,
        "cost_to_serve_eur": round(total - goods),
        "sell_price_eur": sell_price,
        "margin_eur": margin_eur,
        "margin_pct": margin_pct,
    }


def compute_landed_cost(
    lane_key: str,
    oil_series: dict,
    fx_series: dict,
    cfg: dict,
    tensions: dict[str, int] | None = None,
) -> dict:
    """
    Today's landed cost for one lane (uses the latest oil price) under the given
    chokepoint tensions, plus the FX margin signal. Thin wrapper over
    landed_cost_from_values().
    """
    out = landed_cost_from_values(lane_key, _latest(oil_series), cfg, tensions)
    out["fx_margin_headwind_pct"] = _fx_30d_change_pct(fx_series)
    return out


def rank_markets(results: dict) -> list[dict]:
    """
    Given the per-lane results (each carrying a 'landed' dict), return markets
    ordered cheapest-to-serve first. This is the decision the whole tool exists
    to support: where is it least costly to do business right now?
    """
    rows = [
        {
            "lane": lane,
            "label": r["label"],
            "market": r["market"],
            "total_eur": r["landed"]["total_eur"],
            "cost_to_serve_eur": r["landed"]["cost_to_serve_eur"],
            "tariff_rate": r["landed"]["tariff_rate"],
        }
        for lane, r in results.items()
    ]
    return sorted(rows, key=lambda x: x["total_eur"])
