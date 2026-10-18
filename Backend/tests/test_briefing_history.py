"""Briefing delivery and historical pins using isolated synthetic data only."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

import briefings
import database
import ml_risk
import notifications
from main import app


EARLY = datetime(2026, 8, 12, 17, 0, tzinfo=timezone.utc)
DUE = EARLY + timedelta(hours=1)


class BriefingAndHistory(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enterContext(patch('main.initial_assessments.queue'))
        self.enterContext(patch.object(database, "DATABASE_PATH", Path(temp.name) / "isolated.sqlite"))
        self.enterContext(patch.dict(os.environ, {
            "PLANIT_BACKGROUND_REFRESH": "0", "PLANIT_ML_URL": "", "PLANIT_WRITE_TOKEN": "",
            "PLANIT_UPLOAD_DIR": str(Path(temp.name) / "photos"), "PLANIT_REFRESH_SECONDS": "300",
        }))
        self.client = self.enterContext(TestClient(app))
        self.site = self.client.post("/api/sites", json={"name": "TEST historical jobsite", "latitude": 25,
                                                      "longitude": -80, "timezone": "UTC"}).json()
        self.sid = self.site["id"]

    def snapshot(self, now=EARLY, level="low", model=False):
        times = [now.replace(hour=0) + timedelta(days=1, hours=i) for i in range(24)]
        rules = [{"time": value.isoformat(), "overall": {"highest_known_level": level,
                  "drivers": ["extreme_heat"] if level == "severe" else []}} for value in times]
        ml = ml_risk.unavailable("trained_model_not_configured")
        if model:
            ml.update(availability="partial", model_version="TEST_ONLY", input_generated_at=now.isoformat(),
                      hourly_scores=[{"time": times[0].isoformat(), "score": 7.0,
                                      "construction_hazards": ["TEST_ONLY heat"], "availability": "available"}])
        result = {"site": self.site, "generated_at": now.isoformat(), "hourly_predicted_risk": rules,
                  "overall": {"coverage_complete": False}, "ml_assessment": ml,
                  "sources": {"weather": {"status": "available", "retrieved_at": now.isoformat(),
                                           "expires_at": (now + timedelta(minutes=3)).isoformat()},
                              "wildlife": {"status": "unavailable", "reason": "TEST missing"}}}
        database.save_risk(self.sid, result)
        return result

    async def test_preview_does_not_consume_evening_delivery_or_freeze_forecasts(self):
        self.snapshot()
        preview = await briefings.generate(self.sid, now=EARLY, preview=True)
        self.assertTrue(preview["preview"])
        self.assertFalse(preview["briefing"]["delivery"]["consumes_scheduled_delivery"])
        self.assertIsNone(preview["briefing"]["delivery"]["delivered_at"])
        self.assertEqual(briefings.list_briefings(self.sid), [])
        self.assertEqual(notifications.events(self.sid), [])
        self.assertEqual((await briefings.tick(DUE - timedelta(seconds=1)))["generated"], 0)
        self.snapshot(now=DUE, level="severe")
        self.assertEqual((await briefings.tick(DUE))["generated"], 1)
        self.assertEqual((await briefings.tick(DUE + timedelta(minutes=1)))["generated"], 0)
        delivered = briefings.list_briefings(self.sid, now=DUE)[0]
        self.assertEqual(delivered["generated_at"], DUE.isoformat())
        self.assertEqual(delivered["rule_based_assessment"]["highest_known_level"], "severe")
        self.assertEqual(preview["briefing"]["rule_based_assessment"]["highest_known_level"], "low")
        self.assertEqual(len(notifications.events(self.sid)), 1)
        self.assertEqual(delivered["ml_predictions"]["availability"], "unavailable")

    async def test_preview_http_and_explicit_manual_delivery_without_ml(self):
        preview = self.client.post(f"/api/sites/{self.sid}/briefings/preview",
                                   json={"target_date": "2026-08-13"})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["delivery_mode"], "preview")
        self.assertEqual(notifications.events(self.sid), [])
        self.assertEqual(preview.json()["briefing"]["ml_predictions"]["availability"], "unavailable")
        delivered = await briefings.generate(self.sid, now=EARLY)
        self.assertEqual(delivered["delivery_mode"], "actual_delivery")
        self.assertTrue(delivered["briefing"]["delivery"]["consumes_scheduled_delivery"])
        self.assertEqual((await briefings.tick(DUE))["generated"], 0)
        self.assertEqual(len(notifications.events(self.sid)), 1)
        later_preview = await briefings.generate(self.sid, now=DUE, preview=True)
        self.assertEqual(later_preview["briefing"]["generated_at"], DUE.isoformat())
        self.assertNotIn("id", later_preview["briefing"])

    async def test_saved_briefing_rechecks_ml_and_source_age_without_changing_history(self):
        self.snapshot(model=True)
        original = (await briefings.generate(self.sid, now=EARLY))["briefing"]
        self.assertEqual(original["ml_predictions"]["hourly_scores"][0]["score"], 7.0)
        stale = briefings.list_briefings(self.sid, now=EARLY + timedelta(minutes=20))[0]
        prediction = stale["ml_predictions"]["hourly_scores"][0]
        self.assertIsNone(prediction["score"])
        self.assertEqual(prediction["original_score"], 7.0)
        self.assertEqual(prediction["availability"], "stale")
        self.assertEqual(stale["ml_predictions"]["availability"], "stale")
        self.assertIsNone(stale["ml_predictions"]["highest_upcoming"])
        self.assertTrue(stale["freshness"]["assessment_stale"])
        self.assertTrue(stale["environmental_forecasts"]["sources"]["weather"]["stale"])
        self.assertEqual(stale["freshness"]["source_timestamps"]["weather"]["retrieved_at"], EARLY.isoformat())
        self.assertEqual(stale["generated_at"], original["generated_at"])
        self.assertEqual(stale["delivery"], original["delivery"])
        with database.connect() as connection:
            saved = json.loads(connection.execute("SELECT payload FROM briefings WHERE site_id=?", (self.sid,)).fetchone()[0])
        self.assertEqual(saved["ml_predictions"]["hourly_scores"][0]["score"], 7.0)
        self.assertTrue(any("stale" in text for text in stale["missing_information"]))

    async def test_expired_hour_target_is_not_a_briefing_prediction_even_with_fresh_assessment(self):
        snapshot = self.snapshot(model=True)
        target = datetime.fromisoformat(snapshot["ml_assessment"]["hourly_scores"][0]["time"])
        now = target + timedelta(hours=1)
        snapshot["generated_at"] = (now - timedelta(minutes=1)).isoformat()
        database.save_risk(self.sid, snapshot)
        result = (await briefings.generate(self.sid, now=now, target_date=target.date(), preview=True))["briefing"]
        self.assertFalse(result["freshness"]["assessment_stale"])
        prediction = result["ml_predictions"]["hourly_scores"][0]
        self.assertEqual(prediction["time"], target.isoformat())
        self.assertIsNone(prediction["score"])
        self.assertEqual(prediction["original_score"], 7.0)
        self.assertEqual(prediction["availability"], "stale")
        self.assertIsNone(result["ml_predictions"]["highest_upcoming"])

    def report(self, site_id=None, latitude=25, longitude=-80, photo_reference=None):
        body = {"hazard_type": "wildlife", "description": "TEST historical gator observation",
                "latitude": latitude, "longitude": longitude, "photo_reference": photo_reference}
        response = self.client.post(f"/api/sites/{site_id or self.sid}/reports", json=body)
        self.assertEqual(response.status_code, 201)
        return response.json()

    async def test_nearby_history_crosses_sites_and_preserves_resolved_pin_photo_and_provenance(self):
        data = BytesIO()
        Image.new("RGB", (4, 4), "green").save(data, "PNG")
        photo = self.client.post(f"/api/sites/{self.sid}/photos", content=data.getvalue(),
                                 headers={"Content-Type": "image/png"}).json()
        first = self.report(photo_reference=photo["photo_reference"])
        self.client.post(f"/api/sites/{self.sid}/reports/{first['id']}/resolve")
        other = self.client.post("/api/sites", json={"name": "TEST future nearby project", "latitude": 25.01,
                                                   "longitude": -80}).json()
        second = self.report(site_id=other["id"], latitude=25.01)
        self.report(latitude=26)
        query = {"latitude": 25, "longitude": -80, "radius_km": 5, "limit": 10}
        response = self.client.get("/api/reports/nearby", params=query)
        self.assertEqual(response.status_code, 200)
        rows = response.json()["reports"]
        self.assertEqual([row["id"] for row in rows], [first["id"], second["id"]])
        historical = rows[0]
        self.assertEqual(historical["status"], "resolved")
        self.assertIsNotNone(historical["resolved_at"])
        self.assertEqual(historical["original_site"]["id"], self.sid)
        self.assertEqual(historical["provenance"]["report_time"], first["report_time"])
        self.assertEqual(historical["photo_url"], photo["photo_url"])
        self.assertEqual(historical["latitude"], 25)
        self.assertEqual(historical["distance_km"], 0)
        self.assertNotIn("severity", historical)
        self.assertNotIn("probability", historical)
        filtered = self.client.get("/api/reports/nearby", params={**query, "status": "active", "limit": 1}).json()
        self.assertEqual([row["id"] for row in filtered["reports"]], [second["id"]])

    async def test_nearby_validates_coordinates_radius_limit_and_handles_antimeridian(self):
        good = {"latitude": 25, "longitude": -80, "radius_km": 5, "limit": 10}
        for invalid in ({"latitude": 91}, {"longitude": -181}, {"radius_km": 0}, {"radius_km": 101},
                        {"radius_km": "nan"}, {"longitude": "inf"}, {"limit": 0}, {"limit": 101}, {"status": "any"}):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.client.get("/api/reports/nearby", params={**good, **invalid}).status_code, 422)
        report = self.report(latitude=0, longitude=-179.99)
        nearby = self.client.get("/api/reports/nearby", params={"latitude": 0, "longitude": 179.99,
                                                              "radius_km": 5}).json()["reports"]
        self.assertEqual([row["id"] for row in nearby], [report["id"]])
        self.assertLess(nearby[0]["distance_km"], 3)


if __name__ == "__main__":
    unittest.main()
