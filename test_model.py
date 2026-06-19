"""
test_model.py
=============
Unit tests for the pure logic — scoring math, landed cost, and the news lexicon.
These hit NO network (they pass synthetic data straight into the functions), so
they run in milliseconds and can't flake on a dead API.

Run with:  python -m unittest -v   (or just: python test_model.py)
"""

import unittest

import chokepoints
import landed_cost
import risk_model


def _series(values):
    """Turn a list of numbers into a {date: value} series the model expects."""
    return {f"2026-01-{i + 1:02d}": v for i, v in enumerate(values)}


class TestScoringMath(unittest.TestCase):
    def setUp(self):
        self.cfg = risk_model.load_config()

    def test_sigmoid_midpoint_is_50(self):
        # z = 0 (perfectly average) must map to neutral 50.
        self.assertAlmostEqual(risk_model._sigmoid_0_100(0, k=0.9, clamp=3), 50.0)

    def test_sigmoid_is_monotonic(self):
        # Worse-than-normal (z>0) must score higher than better-than-normal.
        low = risk_model._sigmoid_0_100(-1, k=0.9, clamp=3)
        high = risk_model._sigmoid_0_100(1, k=0.9, clamp=3)
        self.assertLess(low, 50)
        self.assertGreater(high, 50)

    def test_zscore_flat_sample_is_zero(self):
        # A perfectly flat history => no abnormality => z = 0 (not a crash).
        self.assertEqual(risk_model._zscore(5, [5, 5, 5, 5]), 0.0)

    def test_negative_news_scores_high_risk(self):
        # Headlines full of risk words must push the sub-score ABOVE 50.
        bad = [{"title": "New tariff and sanctions spark trade war and strike"}]
        out = risk_model.news_sentiment_subscore(bad, self.cfg)
        self.assertGreater(out["score"], 50)
        self.assertLess(out["sentiment"], 0)

    def test_positive_news_scores_low_risk(self):
        good = [{"title": "Trade deal and agreement ease tensions, recovery boost"}]
        out = risk_model.news_sentiment_subscore(good, self.cfg)
        self.assertLess(out["score"], 50)

    def test_word_boundary_no_false_positive(self):
        # "toward" must NOT trigger the "war" keyword (the bug we fixed).
        neutral = [{"title": "Shipments move toward urban hubs in forward planning"}]
        out = risk_model.news_sentiment_subscore(neutral, self.cfg)
        self.assertEqual(out["sentiment"], 0.0)  # no keyword hits at all


class TestLandedCost(unittest.TestCase):
    def setUp(self):
        self.cfg = risk_model.load_config()

    def test_us_tariff_adds_expected_euros(self):
        # US tariff term must equal goods_value * tariff_rate exactly.
        lc = self.cfg["landed_cost"]
        out = landed_cost.landed_cost_from_values("US", 80, self.cfg)
        expected = lc["container_goods_value_eur"] * lc["lanes"]["US"]["tariff_rate"]
        self.assertEqual(out["breakdown"]["tariff"], round(expected))

    def test_canada_cheaper_than_us(self):
        # At the same oil price, Canada (no tariff) must beat the US.
        us = landed_cost.landed_cost_from_values("US", 80, self.cfg)
        ca = landed_cost.landed_cost_from_values("CA", 80, self.cfg)
        self.assertLess(ca["total_eur"], us["total_eur"])

    def test_bunker_scales_with_oil(self):
        # Higher Brent must raise the bunker line (the live cost driver).
        base = landed_cost.landed_cost_from_values("US", 80, self.cfg)
        high = landed_cost.landed_cost_from_values("US", 120, self.cfg)
        self.assertGreater(high["breakdown"]["bunker"], base["breakdown"]["bunker"])

    def test_ranking_orders_cheapest_first(self):
        # rank_markets must return ascending total cost.
        results = {
            lane: {"label": lane, "market": lane,
                   "landed": landed_cost.landed_cost_from_values(lane, 80, self.cfg)}
            for lane in ("US", "MX", "CA")
        }
        ranked = landed_cost.rank_markets(results)
        totals = [r["total_eur"] for r in ranked]
        self.assertEqual(totals, sorted(totals))


class TestChokepoints(unittest.TestCase):
    def setUp(self):
        self.cfg = risk_model.load_config()

    def test_scenario_overrides_tensions(self):
        # The red_sea_closure scenario must raise Bab-el-Mandeb above baseline.
        base = chokepoints.resolve_tensions(self.cfg, "baseline")
        stress = chokepoints.resolve_tensions(self.cfg, "red_sea_closure")
        self.assertGreater(stress["Bab-el-Mandeb"], base["Bab-el-Mandeb"])

    def test_red_sea_lane_riskier_than_atlantic(self):
        # Australia (Red Sea) must carry more chokepoint risk than the US (Gibraltar).
        tens = chokepoints.resolve_tensions(self.cfg, "baseline")
        au = chokepoints.lane_chokepoint_subscore("AU", tens)
        us = chokepoints.lane_chokepoint_subscore("US", tens)
        self.assertGreater(au["score"], us["score"])

    def test_exposure_is_monotonic(self):
        # China adds Malacca to Australia's corridor, so its exposure must be
        # >= Australia's (adding a risk source can't reduce total risk).
        tens = chokepoints.resolve_tensions(self.cfg, "baseline")
        au = chokepoints.lane_chokepoint_subscore("AU", tens)
        cn = chokepoints.lane_chokepoint_subscore("CN", tens)
        self.assertGreaterEqual(cn["score"], au["score"])

    def test_reroute_premium_triggers_when_hot(self):
        # A disrupted chokepoint on the route must add a reroute premium.
        tens = chokepoints.resolve_tensions(self.cfg, "red_sea_closure")
        premium, hot = chokepoints.reroute_premium_eur("AU", tens, self.cfg)
        self.assertGreater(premium, 0)
        self.assertIn("Bab-el-Mandeb", hot)

    def test_hormuz_lifts_cost_globally(self):
        # A Hormuz crisis must raise landed cost even on a lane that never goes
        # near Hormuz (the feedstock/fuel channel), via the oil multiplier.
        calm = chokepoints.resolve_tensions(self.cfg, "baseline")
        crisis = chokepoints.resolve_tensions(self.cfg, "hormuz_crisis")
        us_calm = landed_cost.landed_cost_from_values("US", 80, self.cfg, calm)
        us_crisis = landed_cost.landed_cost_from_values("US", 80, self.cfg, crisis)
        self.assertGreater(us_crisis["total_eur"], us_calm["total_eur"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
