"""Read-time freshness and ranking; synthetic records stay in temporary SQLite."""

from copy import deepcopy
from datetime import timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import alerts
import briefings
import database
import freshness
import ml_risk
import risk
import site_responses
import strike_feed
from test_backend_flows import NOW, feeds


class FreshnessContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(database, "DATABASE_PATH", Path(temporary.name) / "sites.db"))
        self.enterContext(patch.dict(os.environ, {"PLANIT_ML_URL": "", "PLANIT_REFRESH_SECONDS": "300",
                                                "PLANIT_BACKGROUND_REFRESH": "0", "PLANIT_WRITE_TOKEN": ""}))
        database.initialize_database()
        self.site = database.insert_site("TEST freshness", 35.12, -90.08)
        self.sid = self.site["id"]
        briefings.set_timezone(self.sid, "America/Chicago")

    def assessment(self, site=None):
        with patch("risk.utcnow", return_value=NOW):
            snapshot = risk.assess(site or self.site, feeds())
        snapshot["ml_configuration_id"] = ml_risk.configuration_id()
        snapshot["ml_assessment"] = ml_risk.unavailable("trained_model_not_configured")
        return snapshot

    def predicted(self, snapshot, *, current_time=None, future_time=None):
        current = {"time": (current_time or NOW).isoformat(), "score": 7.0,
                   "construction_hazards": ["TEST heat"], "availability": "available"}
        future = {"time": (future_time or NOW + timedelta(hours=1)).isoformat(), "score": 8.0,
                  "construction_hazards": ["TEST wind"], "availability": "available"}
        snapshot["ml_assessment"] = {
            "assessment_type": "trained_ml", "availability": "available", "stale": False,
            "current": current, "current_score": current["score"], "hourly_scores": [future],
            "highest_upcoming": future, "model_version": "TEST-ONLY-v1", "model_id": "TEST-model",
            "input_generated_at": snapshot["generated_at"], "score_range": [0, 10],
        }
        return snapshot

    def test_incomplete_assessment_does_not_expire_before_normal_refresh(self):
        snapshot = self.assessment()
        snapshot["sources"]["wildlife"]["status"] = "unavailable"
        state = freshness.assessment(snapshot, NOW + timedelta(seconds=61))
        self.assertFalse(state["stale"])
        self.assertTrue(state["refresh_due"])
        self.assertEqual(state["refresh_after_seconds"], 60)
        self.assertGreater(state["max_age_seconds"], 300)
        with patch("risk.utcnow", return_value=NOW + timedelta(seconds=301)):
            self.assertTrue(risk.snapshot_is_fresh(snapshot, self.site))
        self.assertTrue(freshness.assessment(snapshot, NOW + timedelta(seconds=901))["stale"])

    def test_sources_preserve_observation_retrieval_and_cache_expiry_separately(self):
        snapshot = self.assessment()
        observed = (NOW - timedelta(minutes=10)).isoformat()
        retrieved = (NOW - timedelta(minutes=5)).isoformat()
        snapshot["current_risk"]["weather_observed_at"] = observed
        snapshot["sources"]["weather"] = {"status": "available", "retrieved_at": retrieved,
            "expires_at": (NOW - timedelta(seconds=1)).isoformat(), "cache_hit": True}
        state = freshness.assessment(snapshot, NOW)
        self.assertFalse(state["stale"])
        self.assertEqual(state["weather_observed_at"], observed)
        self.assertEqual(state["assessment_generated_at"], NOW.isoformat())
        self.assertEqual(state["sources"]["weather"]["retrieved_at"], retrieved)
        self.assertTrue(state["sources"]["weather"]["stale"])
        self.assertEqual(state["sources"]["weather"]["age_seconds"], 300)

    def test_expired_current_model_target_is_null_even_with_new_assessment(self):
        snapshot = self.predicted(self.assessment(), current_time=NOW - timedelta(minutes=6))
        original = deepcopy(snapshot)
        model = ml_risk.serialize_predictions(snapshot, NOW)
        self.assertIsNone(model["current_score"])
        self.assertEqual(model["current_availability"], "stale")
        self.assertEqual(model["current"]["original_score"], 7.0)
        self.assertEqual(model["hourly_scores"][0]["score"], 8.0)
        self.assertEqual(model["availability"], "partial")
        self.assertEqual(snapshot, original)

    def test_expired_hour_target_and_stale_assessment_retain_original_results(self):
        snapshot = self.predicted(self.assessment(), future_time=NOW - timedelta(hours=1))
        model = ml_risk.serialize_predictions(snapshot, NOW)
        self.assertIsNone(model["hourly_scores"][0]["score"])
        self.assertIsNone(model["highest_upcoming"])
        self.assertEqual(model["hourly_scores"][0]["original_score"], 8.0)
        snapshot = self.predicted(self.assessment())
        snapshot["generated_at"] = (NOW - timedelta(minutes=16)).isoformat()
        model = ml_risk.serialize_predictions(snapshot, NOW)
        self.assertEqual(model["availability"], "stale")
        self.assertIsNone(model["current_score"])
        self.assertIsNone(model["hourly_scores"][0]["score"])
        self.assertEqual(model["retained_predictions"]["current"]["time"], NOW.isoformat())
        self.assertEqual(model["retained_predictions"]["hourly_scores"][0]["score"], 8.0)
        # Serializing again cannot silently convert the original results to null.
        snapshot["ml_assessment"] = model
        again = ml_risk.serialize_predictions(snapshot, NOW + timedelta(minutes=1))
        self.assertEqual(again["retained_predictions"]["current"]["score"], 7.0)

    async def test_detail_map_and_briefing_reads_expire_scores_without_refresh(self):
        snapshot = self.predicted(self.assessment(), future_time=NOW + timedelta(hours=8))
        database.save_risk(self.sid, snapshot)
        early = await briefings.generate(self.sid, now=NOW, target_date="2026-08-13")
        self.assertEqual(early["briefing"]["ml_predictions"]["highest_upcoming"]["score"], 8.0)
        later = NOW + timedelta(minutes=16)
        with patch("ml_risk.utcnow", return_value=later), patch("freshness.utcnow", return_value=later), \
             patch("risk.utcnow", return_value=later):
            detail = site_responses.detail(self.site)
            row = site_responses.overview([self.site])["sites"][0]
        briefing = briefings.list_briefings(self.sid, now=later)[0]
        for model in (detail["ml_predictions"], row["ml_assessment"], briefing["ml_predictions"]):
            self.assertEqual(model["availability"], "stale")
            self.assertTrue(model["stale"])
            self.assertIsNone(model["highest_upcoming"])
        self.assertIsNone(detail["ml_predictions"]["current_score"])
        self.assertIsNone(row["current_score"])
        self.assertIsNone(row["upcoming_peak"]["ml_prediction"])
        self.assertEqual(database.load_risk(self.sid)["ml_assessment"]["current_score"], 7.0)

    def test_ranking_keeps_stop_work_and_known_hazards_before_unknown_before_low(self):
        sites = [database.insert_site("TEST " + label, 35.0, -90.0)
                 for label in ("complete low", "missing", "incomplete low", "watch", "stop work")]
        for index in (0, 2, 3, 4):
            snapshot = self.assessment(sites[index])
            overall = {"level": "watch" if index == 3 else "unknown" if index == 2 else "low",
                       "highest_known_level": "watch" if index == 3 else "low",
                       "coverage_complete": index not in (2, 3), "drivers": []}
            snapshot["overall"] = overall
            snapshot["current_risk"]["overall"] = overall
            database.save_risk(sites[index]["id"], snapshot)
        alerts.upsert(sites[4]["id"], "lightning_strike", "severe", "TEST stop work", {}, stop_work=True, now=NOW)
        with patch("freshness.utcnow", return_value=NOW), patch("ml_risk.utcnow", return_value=NOW), \
             patch("risk.utcnow", return_value=NOW):
            result = site_responses.overview(sites)
        self.assertEqual([row["site_id"] for row in result["sites"]],
                         [sites[index]["id"] for index in (4, 3, 1, 2, 0)])
        self.assertEqual([row["rank"] for row in result["sites"]], [1, 2, 3, 4, 5])
        incomplete = next(row for row in result["sites"] if row["site_id"] == sites[2]["id"])
        self.assertEqual(incomplete["rule_based_severity"], "unknown")
        self.assertEqual(incomplete["ranking_reason"], "assessment_uncertain")
        self.assertEqual(incomplete["coverage_status"], "incomplete")
        self.assertEqual(result["ranking"]["type"], "rule_based")
        self.assertFalse(result["ranking"]["ml_scores_used"])
        self.assertTrue(all(row["current_score"] is None for row in result["sites"]))

    def test_measured_counts_require_coverage_and_ignore_future_duplicate_and_distant_events(self):
        self.enterContext(patch.dict(os.environ, {'PLANIT_LIGHTNING_API_KEY': 'TEST_FIXTURE_ONLY'}))
        self.assertIsNone(strike_feed.measured_count(self.sid, NOW.isoformat(), now=NOW)["count"])
        events = [{"id": "TEST-" + str(index), "latitude": lat, "longitude": -90.08, "time": stamp.isoformat()}
                  for index, (lat, stamp) in enumerate([
                      (35.12, NOW - timedelta(minutes=5)), (35.12, NOW - timedelta(minutes=31)),
                      (40.0, NOW - timedelta(minutes=10)), (35.12, NOW + timedelta(minutes=1)),
                      (35.12, NOW - timedelta(minutes=30))])]
        feed = {"provider": "TEST-ONLY", "availability": "available", "events": events,
                "coverage_start": (NOW - timedelta(minutes=55)).isoformat(), "coverage_end": NOW.isoformat()}
        strike_feed.apply(self.site, feed, now=NOW)
        strike_feed.apply(self.site, feed, now=NOW)
        result = strike_feed.measured_count(self.sid, NOW.isoformat(), now=NOW)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["window_semantics"], "(start,end]")
        self.assertIsNone(strike_feed.measured_count(self.sid, (NOW + timedelta(hours=1)).isoformat(), now=NOW)["count"])
        self.assertIsNone(strike_feed.measured_count(self.sid, NOW.isoformat(), now=NOW + timedelta(minutes=2))["count"])
        feed.update(coverage_start=(NOW - timedelta(minutes=10)).isoformat())
        strike_feed.apply(self.site, feed, now=NOW)
        self.assertIsNone(strike_feed.measured_count(self.sid, NOW.isoformat(), now=NOW)["count"])

    def test_ml_context_count_is_current_only_and_request_hash_uses_captured_context(self):
        self.enterContext(patch.dict(os.environ, {'PLANIT_LIGHTNING_API_KEY': 'TEST_FIXTURE_ONLY'}))
        snapshot = self.assessment()
        feed = {"provider": "TEST-ONLY", "availability": "available", "events": [],
                "coverage_start": (NOW - timedelta(minutes=55)).isoformat(), "coverage_end": NOW.isoformat()}
        strike_feed.apply(self.site, feed, now=NOW)
        with patch("strike_feed.utcnow", return_value=NOW), patch("ml_risk.utcnow", return_value=NOW):
            snapshot["ml_context"] = ml_risk.capture_context(snapshot)
        request = ml_risk.request_for(snapshot)
        self.assertEqual(request["schema_version"], "site-risk-features-v1")
        self.assertEqual(len(request["samples"]), 49)
        self.assertEqual(request["samples"][0]["features"]["measured_lightning_strike_count"], 0)
        self.assertTrue(all(sample["features"]["measured_lightning_strike_count"] is None
                            for sample in request["samples"][1:]))
        self.assertIn("river", request["supplemental_context"])
        self.assertIn("hefs", request["supplemental_context"])
        self.assertIn("crew_reports", request["supplemental_context"])
        strike_feed.apply(self.site, {"availability": "unknown", "events": None}, now=NOW)
        self.assertEqual(ml_risk.request_for(snapshot)["request_id"], request["request_id"])

    def test_probability_conversion_is_explicit_and_model_scores_are_not_auto_scaled(self):
        self.assertEqual(ml_risk.probability_to_score(0.70), 7.0)
        for invalid in (True, -0.1, 1.1, float("nan"), None):
            with self.assertRaises(ValueError):
                ml_risk.probability_to_score(invalid)
        request = ml_risk.request_for(self.assessment())
        parsed = ml_risk.parse_prediction({"request_id": request["request_id"], "model_version": "TEST",
            "predictions": [{"time": request["samples"][0]["time"], "score": 0.7, "construction_hazards": []}]}, request)
        self.assertEqual(parsed["current_score"], 0.7)


if __name__ == "__main__":
    unittest.main()
