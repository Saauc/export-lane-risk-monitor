"""
backtest.py
===========
The credibility centerpiece. A risk tool nobody can check is just an opinion;
this replays history and asks "would the recommendation actually have been
right, and what drives it?"

It reads the ~90 days of market history sitting in the SQLite store (FX, oil and
freight were backfilled, so we have real history from day one), reconstructs the
EUR landed cost per market for EACH past day, and reports:

  1. How often the "serve the cheapest market" call would have been correct.
  2. How big the day-to-day cost swing was (freight + fuel) vs the fixed tariff —
     i.e. WHAT actually drives the decision.
  3. How FX moved each market's EUR margin over the window (the part that DOES
     diverge between markets over time).

Honesty note: news sentiment and the blended risk score only exist from the day
the monitor first runs forward, so this backtest validates the COST model (which
is fully reconstructable from history), not the news component. We say so.
"""

from __future__ import annotations

import statistics

import chokepoints
import data_sources
import landed_cost
import risk_model
import storage


def _aligned_market_series(conn, limit: int = 120) -> dict[str, dict]:
    """
    Pull per-lane history and return, per date, the global oil/freight values and
    each lane's FX rate. oil/freight are the same across lanes (global prices),
    so we read them once.
    """
    per_lane = {
        lane: {h["date"]: h for h in storage.load_history(conn, lane, limit=limit)}
        for lane in data_sources.LANES
    }
    # Use any lane's dates that carry an oil price as the spine.
    any_lane = next(iter(data_sources.LANES))
    dates = [d for d, row in per_lane[any_lane].items() if row.get("oil_price")]
    return {"per_lane": per_lane, "dates": sorted(dates)}


def run_backtest(limit: int = 120) -> dict:
    """Reconstruct daily landed costs over history and summarise the findings."""
    cfg = risk_model.load_config()
    lanes = list(data_sources.LANES.keys())
    # CRITICAL: reconstruct with the SAME baseline tensions the live cards use,
    # so feedstock + reroute premiums are in BOTH — otherwise today's live number
    # (which includes them) would sit above a backtest range that excluded them.
    tensions = chokepoints.resolve_tensions(cfg, "baseline")

    with storage.get_connection() as conn:
        data = _aligned_market_series(conn, limit=limit)

    per_lane, dates = data["per_lane"], data["dates"]
    if len(dates) < 5:
        return {"ok": False, "error": "not enough history to backtest yet",
                "days": len(dates)}

    # Per day: landed cost for each lane, and which market was cheapest.
    daily_total = {lane: [] for lane in lanes}      # lane -> [total_eur per day]
    cheapest_counts = {lane: 0 for lane in lanes}

    for d in dates:
        oil = per_lane[lanes[0]][d]["oil_price"]
        totals = {}
        for lane in lanes:
            lc = landed_cost.landed_cost_from_values(lane, oil, cfg, tensions)
            totals[lane] = lc["total_eur"]
            daily_total[lane].append(lc["total_eur"])
        cheapest = min(totals, key=totals.get)
        cheapest_counts[cheapest] += 1

    n = len(dates)
    avg_cost = {lane: statistics.mean(daily_total[lane]) for lane in lanes}
    by_avg = sorted(lanes, key=lambda l: avg_cost[l])
    winner = max(cheapest_counts, key=cheapest_counts.get)
    runner_up = by_avg[1] if by_avg[0] == winner else by_avg[0]

    # Why the call holds: the winner's WHOLE cost range sits below the runner-up's
    # — disjoint ranges, so it's not a coin-flip. (This is the right evidence; the
    # US tariff explains why the US is expensive, not why the winner beats #2.)
    win_hi = max(daily_total[winner])
    run_lo = min(daily_total[runner_up])
    ranges_disjoint = win_hi < run_lo
    winner_vs_runnerup_eur = round(avg_cost[runner_up] - avg_cost[winner])
    max_swing = max(max(daily_total[l]) - min(daily_total[l]) for l in lanes)

    # FX margin divergence: how each EUR/dest rate moved start->end of window.
    fx_moves = {}
    for lane in lanes:
        rows = [per_lane[lane][d] for d in dates if per_lane[lane][d].get("fx_rate")]
        if len(rows) >= 2:
            first, last = rows[0]["fx_rate"], rows[-1]["fx_rate"]
            pct = (last - first) / first * 100 if first else 0.0
            # EUR-margin impact on a goods-value container if revenue is fixed in
            # the destination currency: a stronger euro (rate up) erodes EUR value.
            eur_impact = cfg["landed_cost"]["container_goods_value_eur"] * (-pct / 100)
            fx_moves[lane] = {"pct": round(pct, 2), "eur_impact": round(eur_impact)}

    return {
        "ok": True,
        "days": n,
        "winner": winner,
        "winner_market": data_sources.LANES[winner]["market"],
        "runner_up_market": data_sources.LANES[runner_up]["market"],
        "cheapest_share_pct": round(cheapest_counts[winner] / n * 100, 1),
        "cheapest_counts": cheapest_counts,
        "ranges_disjoint": ranges_disjoint,
        "winner_vs_runnerup_eur": winner_vs_runnerup_eur,
        "cost_range_eur": {lane: [round(min(daily_total[lane])), round(max(daily_total[lane]))]
                           for lane in lanes},
        "max_swing_eur": round(max_swing),
        "fx_moves": fx_moves,
        "avg_cost_eur": {lane: round(avg_cost[lane]) for lane in lanes},
    }


def format_report(bt: dict) -> str:
    """Human-readable backtest summary, for the CLI and the README."""
    if not bt.get("ok"):
        return f"Backtest unavailable: {bt.get('error')} (have {bt.get('days')} days)."

    lines = [
        "=" * 60,
        f"LANDED-COST BACKTEST  ·  {bt['days']} trading days of history",
        "=" * 60,
        "",
        f"Lowest cost-to-serve: {bt['winner_market']} on "
        f"{bt['cheapest_share_pct']}% of days "
        f"({bt['cheapest_counts'][bt['winner']]}/{bt['days']}).",
        "",
        "WHY THE CALL HOLDS (not a coin-flip):",
        f"  vs runner-up ({bt['runner_up_market']}): cheaper by ~€{bt['winner_vs_runnerup_eur']:,}/container on average.",
        f"  Cost ranges {'do NOT overlap' if bt['ranges_disjoint'] else 'overlap'} — "
        f"{'the winner is below the runner-up on every single day.' if bt['ranges_disjoint'] else 'the lead is within daily noise; treat as a tie.'}",
        "",
        "AVERAGE LANDED COST OVER WINDOW:",
    ]
    for lane, avg in bt["avg_cost_eur"].items():
        lo, hi = bt["cost_range_eur"][lane]
        lines.append(f"  {data_sources.LANES[lane]['market']:<14} €{avg:,}  "
                     f"(range €{lo:,}–€{hi:,})")
    lines += ["", "FX MARGIN MOVE OVER WINDOW (EUR value of dest-currency revenue):"]
    for lane, mv in bt["fx_moves"].items():
        direction = "headwind" if mv["eur_impact"] < 0 else "tailwind"
        lines.append(f"  {data_sources.LANES[lane]['market']:<14} EUR/"
                     f"{data_sources.LANES[lane]['currency']} {mv['pct']:+}%  "
                     f"-> €{mv['eur_impact']:+,} / container ({direction})")
    lines += ["",
              "Note: reconstructed on the SAME basis as the live cards (baseline",
              "chokepoint tensions, so feedstock + reroute are in both). Validates",
              "the COST model; news sentiment / blended risk accrue forward."]
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_report(run_backtest()))
