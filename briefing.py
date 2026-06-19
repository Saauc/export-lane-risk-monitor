"""
briefing.py
===========
Phase 3 — the daily text briefing.

A risk dashboard shows numbers; a briefing EXPLAINS them in plain English. For a
supply-chain / finance analyst this is half the job — you're paid to translate a
score into "so what". This module turns the scored lane results into a short,
readable summary a human could paste into a morning email.

It's pure string formatting over the risk_model output — no network, no math —
so it's trivial to read and to change the wording.
"""

from __future__ import annotations

from datetime import date

import landed_cost


def _driver_sentence(r: dict) -> str:
    """
    Identify which of the FOUR sub-scores is pushing this lane's risk the most
    and phrase it. This is what makes the briefing feel insightful rather than a
    data dump — it names the CAUSE, not just the level.
    """
    subs = r["subscores"]
    # Map each component to its 0-100 sub-score; the highest is the top driver.
    contributions = {
        "FX volatility": subs["fx_volatility"]["score"],
        "fuel cost": subs["fuel_cost"]["score"],
        "news sentiment": subs["news_sentiment"]["score"],
        "chokepoint exposure": subs["chokepoint"]["score"],
    }
    top = max(contributions, key=contributions.get)
    top_val = contributions[top]

    if top_val >= 66:
        intensity = "the dominant pressure"
    elif top_val >= 50:
        intensity = "the main driver"
    else:
        intensity = "the most notable factor (though all inputs are subdued)"
    return f"{top.capitalize()} is {intensity}."


def briefing_for_lane(r: dict) -> str:
    """One paragraph for a single lane."""
    sent = r["metrics"]["news_sentiment"]
    mood = "negative" if sent < -0.1 else "positive" if sent > 0.1 else "neutral"

    fx = r["metrics"]["fx_rate"]
    oil = r["metrics"]["oil_price"]
    container = r["metrics"]["container_freight_eur"]

    lc = r["landed"]
    choke = r["subscores"]["chokepoint"]
    corridor = ", ".join(choke["corridor"]) if choke["corridor"] else "none"
    reroute = lc["breakdown"].get("reroute", 0)
    reroute_txt = (f" Diverting via Cape of Good Hope (+€{reroute:,} reroute)."
                   if reroute else "")

    lines = [
        f"{r['market']} ({r['label']}) — RISK {r['risk_score']}/100 [{r['band']}]"
        f"  ·  cost-to-serve €{lc['total_eur']:,}/container",
        f"  {_driver_sentence(r)}",
        f"  News flow is {mood} (sentiment {sent}). EUR/{r['currency']} at {fx}; "
        f"container freight ≈€{container:,}; Brent ${oil}. "
        f"Tariff/duty €{lc['breakdown']['tariff']:,} ({int(lc['tariff_rate']*100)}%).",
        f"  Corridor chokepoints: {corridor} (exposure {choke['score']}/100)."
        f"{reroute_txt}",
    ]
    return "\n".join(lines)


def generate_briefing(results: dict, scenario_label: str | None = None) -> str:
    """
    Build the full multi-lane briefing string. `results` is the dict returned by
    risk_model.score_all_lanes(). Lanes are ordered most- to least-risky so the
    reader sees the worst lane first. `scenario_label` notes the active stress
    scenario when it isn't the baseline.
    """
    ordered = sorted(results.values(), key=lambda r: r["risk_score"], reverse=True)

    scen_line = (f"  ·  scenario: {scenario_label}"
                 if scenario_label and scenario_label.lower() != "baseline (today)"
                 else "")
    header = (
        f"TRADE LANE RISK BRIEFING — {date.today().isoformat()}{scen_line}\n"
        f"{'=' * 52}"
    )

    # Top-of-brief: lane cost spread (for pricing/margin), then the risk flag.
    # NOTE: this is NOT a "which country to sell to" recommender — the exporter
    # already sells into all these markets. It flags the cost-to-serve spread so
    # pricing, surcharges, FX hedging and shipment timing can react per lane.
    ranking = landed_cost.rank_markets(results)
    cheapest, priciest = ranking[0], ranking[-1]
    spread = priciest["total_eur"] - cheapest["total_eur"]
    worst = ordered[0]
    takeaway = (
        f"Cost-to-serve spread: {cheapest['market']} is currently leanest at "
        f"€{cheapest['total_eur']:,}/container; {priciest['market']} the heaviest "
        f"(+€{spread:,}) — watch margin/pricing there.\n"
        f"Highest risk today: {worst['market']} at {worst['risk_score']}/100 "
        f"({worst['band']}).\n"
    )

    body = "\n\n".join(briefing_for_lane(r) for r in ordered)

    footer = (
        "\n\n" + "-" * 52 + "\n"
        "Risk blends FX volatility (30%), fuel cost / Brent (20%), news "
        "sentiment (20%) and chokepoint exposure (30%). Higher = more risk."
    )

    return f"{header}\n\n{takeaway}\n{body}{footer}"


if __name__ == "__main__":
    # Smoke test: score live and print the briefing.
    import risk_model

    results = risk_model.score_all_lanes(persist=False)
    print(generate_briefing(results))
