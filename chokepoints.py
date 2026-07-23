"""
chokepoints.py
==============
The "difficult climate" engine — what turns the map's chokepoint dots from
decoration into a working risk model.

Every maritime chokepoint carries a TENSION (0-100) reflecting how disrupted it
is right now (config.json -> chokepoints.points). A lane that sails through a
chokepoint inherits that tension as risk, and a badly disrupted chokepoint adds
a REROUTE cost premium (e.g. diverting Red Sea traffic around the Cape of Good
Hope). Hormuz is special: it's oil-linked, so its tension lifts fuel AND
feedstock cost on EVERY lane, because coatings are petroleum-derived.

STRESS SCENARIOS (config.json -> scenarios) let you override tensions to ask
"what if the Red Sea closes / Hormuz flares?" and watch every
lane's risk and cost react. That is the actual "analyse the risk of a difficult
climate" feature.

All functions are pure (no network), so they're trivial to test and to drive
from the dashboard's scenario selector.
"""

from __future__ import annotations

import data_sources


def resolve_tensions(cfg: dict, scenario_key: str = "baseline") -> dict[str, int]:
    """
    Return the live {chokepoint: tension} map for a given scenario: the baseline
    tensions with that scenario's overrides applied on top.
    """
    points = cfg["chokepoints"]["points"]
    tensions = {name: p["tension"] for name, p in points.items()}
    overrides = cfg.get("scenarios", {}).get(scenario_key, {}).get("overrides", {})
    tensions.update(overrides)
    return tensions


def resolve_custom_tensions(cfg: dict, severity: float) -> dict[str, int]:
    """
    Continuous alternative to a named scenario: interpolate every chokepoint's
    tension from baseline (severity=0) toward the combined worst case of ALL
    configured stress scenarios merged together (severity=100). Backs the
    dashboard's disruption-severity slider — one knob standing in for "how bad
    is the climate right now", rather than picking one canned scenario.
    """
    points = cfg["chokepoints"]["points"]
    baseline = {name: p["tension"] for name, p in points.items()}
    worst = dict(baseline)
    for key, scen in cfg.get("scenarios", {}).items():
        if key.startswith("_") or not isinstance(scen, dict):
            continue
        worst.update(scen.get("overrides", {}))
    frac = max(0.0, min(100.0, severity)) / 100.0
    return {name: round(baseline[name] + (worst.get(name, baseline[name]) - baseline[name]) * frac)
            for name in baseline}


def lane_chokepoint_subscore(lane_key: str, tensions: dict[str, int]) -> dict:
    """
    0-100 chokepoint-exposure sub-score for one lane, using a NOISY-OR over the
    chokepoints on the lane's natural corridor.

        exposure = (1 - PRODUCT(1 - tension_i/100)) * 100

    Reading tension/100 as a per-chokepoint disruption probability, this is the
    probability that AT LEAST ONE corridor chokepoint is disrupted. It is
    MONOTONIC: adding another chokepoint (even a calm one) can never lower the
    exposure — which fixes the old max+average blend, where adding low-tension
    Malacca made China look safer than Australia.

    This measures the lane's exposure to disruption whether it ends up transiting
    the chokepoint or diverting around it; the euro cost of diverting is a
    SEPARATE output (reroute_premium_eur), not a second count of the same risk.
    """
    names = data_sources.LANES[lane_key].get("chokepoints", [])
    if not names:
        return {"score": 0.0, "corridor": [], "max_tension": 0}
    prod = 1.0
    for n in names:
        prod *= (1.0 - tensions.get(n, 0) / 100.0)
    score = round((1.0 - prod) * 100, 1)
    return {"score": score, "corridor": names,
            "max_tension": max(tensions.get(n, 0) for n in names)}


def reroute_premium_eur(lane_key: str, tensions: dict[str, int], cfg: dict) -> tuple[int, list[str]]:
    """
    EUR cost of DIVERTING around the Cape of Good Hope when a corridor chokepoint
    is too disrupted to transit (tension >= reroute_threshold) — one premium per
    disrupted chokepoint avoided. Returns (premium, [chokepoints being avoided]).
    A non-zero premium means the lane is being drawn on the map as a Cape
    diversion rather than through the chokepoint.
    """
    cp = cfg["chokepoints"]
    threshold, premium = cp["reroute_threshold"], cp["reroute_premium_eur"]
    names = data_sources.LANES[lane_key].get("chokepoints", [])
    avoided = [n for n in names if tensions.get(n, 0) >= threshold]
    return premium * len(avoided), avoided


def hormuz_oil_multiplier(tensions: dict[str, int], cfg: dict) -> float:
    """
    Global fuel/feedstock cost multiplier driven by Hormuz tension. At tension 0
    it's 1.0 (no effect); at 100 with sensitivity 0.4 it's 1.4 (+40%). Applies to
    every lane because Hormuz gates the oil/gas that becomes coating feedstock.
    """
    t = tensions.get("Hormuz", 0)
    sens = cfg["chokepoints"]["hormuz_oil_sensitivity"]
    return 1.0 + sens * (t / 100.0)
