"""
app.py
======
Phase 3 — the web server.

A small Flask app that ties everything together:
    data_sources -> risk_model -> storage  (the pipeline)
    +  this file  -> a dark-theme dashboard + a JSON API

Two routes:
    GET /            -> renders the dashboard HTML (templates/index.html)
    GET /api/state   -> returns all the data the page needs as JSON
    POST /api/refresh-> re-runs the live pipeline, then returns fresh state

We CACHE the scored state in memory so loading the page doesn't hammer the news
APIs on every refresh. The first request computes it; an explicit refresh button
(or the /api/refresh route) recomputes on demand.
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template, request

import alerts
import backtest
import briefing
import chokepoints
import data_sources
import landed_cost
import risk_model
import storage

app = Flask(__name__)
# Re-read templates from disk on each request so edits show on a simple reload,
# without turning on full debug mode (which would block the port at startup).
app.config["TEMPLATES_AUTO_RELOAD"] = True


def active_branding() -> dict:
    """Resolve the branding profile selected by config.json -> branding.mode."""
    cfg = risk_model.load_config()
    branding = cfg.get("branding", {})
    mode = branding.get("mode", "generic")
    return branding.get("profiles", {}).get(mode, {
        "title": "Trade Lane Monitor", "subtitle": "", "cargo": "goods"})

# Caches: the live data bundle is fetched once and reused across scenarios; the
# computed state is cached per scenario so flipping scenarios is instant.
_bundle_cache: dict | None = None
_state_cache: dict[str, dict] = {}


def _scenarios_payload(cfg: dict, active: str, tensions: dict[str, int]) -> dict:
    """List of selectable scenarios + the resolved chokepoint tensions for the
    active one (so the map can colour the chokepoint markers). `tensions` is
    passed in rather than re-resolved here, since the caller already computed
    it (and "custom" isn't a lookup key in config.scenarios at all)."""
    scen = cfg.get("scenarios", {})
    points = cfg["chokepoints"]["points"]
    # skip the "_comment" doc key — only real scenario entries have a label.
    options = [{"key": k, "label": v["label"]}
               for k, v in scen.items() if not k.startswith("_")]
    options.append({"key": "custom", "label": "Custom disruption"})
    return {
        "active": active,
        "reroute_threshold": cfg["chokepoints"]["reroute_threshold"],
        "options": options,
        "chokepoints": [
            {"name": name, "lon": p["lon"], "lat": p["lat"], "tension": tensions[name]}
            for name, p in points.items()
        ],
    }


def _build_state(refresh: bool = False, scenario: str = "baseline",
                 severity: int | None = None) -> dict:
    """
    Compute (or return cached) the dashboard payload for a given stress scenario.
    The live data bundle is fetched once and shared across scenarios. When
    `scenario` is "custom", `severity` (0-100) drives a continuous blend between
    baseline and worst-case tensions instead of a named config scenario.
    """
    global _bundle_cache, _state_cache
    cache_key = scenario if scenario != "custom" else f"custom:{severity}"
    if not refresh and cache_key in _state_cache:
        return _state_cache[cache_key]

    cfg = risk_model.load_config()

    # Fetch the live bundle once (or on refresh); reuse it for every scenario.
    if _bundle_cache is None or refresh:
        _bundle_cache = {
            "fx": data_sources.fetch_fx_rates(days=120),
            "oil": data_sources.fetch_oil_prices(days=90),
            "news": {lane: data_sources.fetch_news(lane) for lane in data_sources.LANES},
        }
        _state_cache = {}  # bundle changed -> drop stale per-scenario states

    if scenario == "custom":
        tensions = chokepoints.resolve_custom_tensions(cfg, severity)
        scenario_label = f"Custom disruption ({severity}%)"
    else:
        tensions = chokepoints.resolve_tensions(cfg, scenario)
        scenario_label = cfg.get("scenarios", {}).get(scenario, {}).get("label")

    # Score under this scenario; only the baseline persists to the DB.
    results = risk_model.score_all_lanes(
        bundle=_bundle_cache, persist=(scenario == "baseline"),
        scenario=scenario, tensions=tensions)

    # 30-day trend series + yesterday's snapshot (for alerts) from SQLite.
    trends = {}
    prev_snapshots = {}
    with storage.get_connection() as conn:
        for lane in data_sources.LANES:
            history = storage.load_history(conn, lane, limit=30)
            trends[lane] = {
                "dates": [h["date"] for h in history],
                "fx_rate": [h["fx_rate"] for h in history],
                "oil_price": [h["oil_price"] for h in history],
                "risk_score": [h["risk_score"] for h in history],
            }
            # Yesterday = second-most-recent row (index 1), most recent is today.
            if len(history) >= 2:
                prev_snapshots[lane] = history[-2]

    # Rank lanes by cost-to-serve. Framed as a margin/pricing monitor across the
    # exporter's EXISTING lanes — not a "which country to sell to" recommender.
    ranking = landed_cost.rank_markets(results)
    cheapest, priciest = ranking[0], ranking[-1]
    recommendation = {
        "market": cheapest["market"],
        "lane": cheapest["lane"],
        "total_eur": cheapest["total_eur"],
        "spread_eur": priciest["total_eur"] - cheapest["total_eur"],
        "priciest_market": priciest["market"],
        "priciest_total_eur": priciest["total_eur"],
    }

    # Alerts: only meaningful on the baseline (stress scenarios don't have history).
    lane_alerts = alerts.check_alerts(results, prev_snapshots, cfg) if scenario == "baseline" else []

    _state_cache[cache_key] = {
        "results": results,
        "trends": trends,
        "ranking": ranking,
        "recommendation": recommendation,
        "alerts": lane_alerts,
        "backtest": backtest.run_backtest(),
        "briefing": briefing.generate_briefing(results, scenario_label=scenario_label),
        "scenarios": _scenarios_payload(cfg, scenario, tensions),
        "lane_order": list(data_sources.LANES.keys()),
        "severity": severity if scenario == "custom" else None,
        # Exposed so the dashboard's weight-reweighting sliders can recompute
        # risk_score/band client-side from the already-shipped per-lane
        # subscores, without a round trip per drag.
        "weights": cfg["weights"],
        "risk_bands": cfg["risk_bands"],
    }
    return _state_cache[cache_key]


def _scenario_arg(default: str = "baseline") -> str:
    """Read & validate the ?scenario= query param against config. "custom" is
    always allowed — it's the slider mode and isn't a config.scenarios key."""
    cfg = risk_model.load_config()
    requested = request.args.get("scenario", default)
    if requested == "custom" or requested in cfg.get("scenarios", {}):
        return requested
    return default


def _severity_arg(default: int = 50) -> int:
    """Read & clamp the ?severity= query param (0-100) for custom scenario mode."""
    try:
        value = int(request.args.get("severity", default))
    except (TypeError, ValueError):
        value = default
    return max(0, min(100, value))


@app.route("/")
def index():
    """Render the dashboard shell; the page fetches /api/state for its data."""
    return render_template("index.html", branding=active_branding())


@app.route("/api/state")
def api_state():
    """Return cached (or first-computed) state for the requested scenario."""
    scenario = _scenario_arg()
    severity = _severity_arg() if scenario == "custom" else None
    return jsonify(_build_state(refresh=False, scenario=scenario, severity=severity))


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Force a fresh live pipeline run and return the new state."""
    scenario = _scenario_arg()
    severity = _severity_arg() if scenario == "custom" else None
    return jsonify(_build_state(refresh=True, scenario=scenario, severity=severity))


if __name__ == "__main__":
    # NOTE: we deliberately do NOT fetch data here. Binding the port must happen
    # immediately so the server is reachable; the cache is filled lazily on the
    # first /api/state request instead (which is why that first call is slower).
    print("Serving on http://127.0.0.1:5000 (data loads on first request)")
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
