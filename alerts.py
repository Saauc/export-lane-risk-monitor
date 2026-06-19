"""
alerts.py
=========
In-dashboard alert engine. Compares today's scored results against the
previous day's SQLite snapshot and flags lanes where something meaningful
changed — risk crossed a band boundary or landed cost moved by more than
the configured threshold.

Returns a list of alert dicts injected into the /api/state payload and
rendered as a notification strip in the UI. No email/push — alerts are
surfaced in the dashboard itself.
"""

from __future__ import annotations

_BAND_ORDER = {"Low": 0, "Moderate": 1, "Elevated": 2, "High": 3}


def check_alerts(results: dict, history: dict, cfg: dict) -> list[dict]:
    """
    Compare today's `results` (from score_all_lanes) against `history`
    (a dict keyed by lane_key of the most recent snapshot row from storage).

    Returns a list of alert dicts, each with:
        lane, market, type, msg, severity  ("warning" | "info" | "ok")
    Empty list = nothing to flag.
    """
    thresholds = cfg.get("alerts", {})
    cost_pct_threshold = float(thresholds.get("cost_change_pct", 3.0))
    watch_band = bool(thresholds.get("band_change", True))

    alerts = []

    for lane_key, r in results.items():
        prev = history.get(lane_key)
        if not prev:
            continue  # no history yet — can't compare

        market = r["market"]

        # --- Band change (risk worsened) ------------------------------------
        if watch_band:
            prev_band = prev.get("band")
            curr_band = r.get("band")
            if prev_band and curr_band and prev_band != curr_band:
                prev_ord = _BAND_ORDER.get(prev_band, 0)
                curr_ord = _BAND_ORDER.get(curr_band, 0)
                if curr_ord > prev_ord:
                    alerts.append({
                        "lane": lane_key,
                        "market": market,
                        "type": "band_up",
                        "msg": f"{market} risk rose to {curr_band} (was {prev_band})",
                        "severity": "warning",
                    })
                elif curr_ord < prev_ord:
                    alerts.append({
                        "lane": lane_key,
                        "market": market,
                        "type": "band_down",
                        "msg": f"{market} risk eased to {curr_band} (was {prev_band})",
                        "severity": "ok",
                    })

        # --- Landed cost move -----------------------------------------------
        prev_cost = prev.get("landed_cost")
        curr_cost = r["landed"]["total_eur"]
        if prev_cost and prev_cost > 0:
            change_pct = (curr_cost - prev_cost) / prev_cost * 100
            if abs(change_pct) >= cost_pct_threshold:
                sign = "+" if change_pct > 0 else ""
                direction = "up" if change_pct > 0 else "down"
                alerts.append({
                    "lane": lane_key,
                    "market": market,
                    "type": f"cost_{direction}",
                    "msg": (f"{market} cost-to-serve {sign}{change_pct:.1f}% vs yesterday "
                            f"(€{curr_cost:,})"),
                    "severity": "warning" if change_pct > 0 else "info",
                })

    # Most severe first, then alphabetical by market.
    severity_order = {"warning": 0, "info": 1, "ok": 2}
    alerts.sort(key=lambda a: (severity_order.get(a["severity"], 9), a["market"]))
    return alerts
