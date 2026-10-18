"""Synthetic river conditions live only in isolated test databases."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

import alerts
import database
import risk
import rivers


NOW = datetime(2026, 10, 17, 20, tzinfo=timezone.utc)


class RiverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.enterContext(patch.object(database, "DATABASE_PATH", Path(folder.name) / "sites.db"))
        self.enterContext(patch("rivers.utcnow", return_value=NOW))
        database.initialize_database()
        self.site = database.insert_site("TEST river location", 35.12, -90.08)

    def gauge(self, **changes):
        value = {"lid": "TEST1", "name": "TEST gauge", "latitude": 35.12, "longitude": -90.08,
                 "status": {"observed": {"validTime": NOW.isoformat(), "primary": 46, "primaryUnit": "ft",
                                         "secondary": -999, "floodCategory": "major_flooding"}},
                 "flood": {"stageUnits": "ft", "flowUnits": "cfs", "categories": {"major": {"stage": 46}}}}
        value.update(changes)
        return value

    def assessment(self, rain=0, warnings=None):
        sources = {"alerts": {"status": "available", "data": {"alerts": warnings or []}},
                   "wildfires": {"status": "available", "data": {"events": []}},
                   "wildlife": {"status": "available", "data": {"total_results": 0}}}
        point = {"wind_gusts_10m": 5, "apparent_temperature": 80, "soil_moisture_3_to_9cm": 0.2,
                 "weather_code": 0}
        frame = risk.assess_frame(self.site, sources, point, NOW, rain, 0)
        return {"site": self.site, "generated_at": NOW.isoformat(),
                "current_risk": frame, "sources": {}}

    def context(self, confirmed=False):
        return {"availability": "available", "exposure_confirmed": confirmed,
                "exposure_verification": {"site_relevance_verified": confirmed},
                "gauge": {"id": "TEST1"}, "observation": self.gauge()["status"]["observed"],
                "source": {"status": "available", "retrieved_at": NOW.isoformat()}}

    async def test_unconfirmed_major_gauge_stays_context_and_cannot_create_stop_work(self):
        legacy = self.context(True)
        legacy.pop('exposure_verification')
        self.assertEqual(rivers.apply_context(self.assessment(), legacy)['indicators']['flooding']['level'], 'low')
        assessment = rivers.apply_context(self.assessment(), self.context())
        card = assessment["indicators"]["flooding"]
        self.assertEqual(card["level"], "low")
        self.assertEqual(card["evidence"]["nearby_river_conditions"]["indicative_river_level"], "severe")
        self.assertFalse(card["evidence"]["nearby_river_conditions"]["used_for_site_escalation"])
        alerts.evaluate(assessment)
        self.assertFalse(alerts.for_site(self.site["id"]))
        confirmed = rivers.apply_context(self.assessment(), self.context(True))
        alerts.evaluate(confirmed)
        flood = [row for row in alerts.for_site(self.site["id"]) if row["hazard"] == "flooding"][0]
        self.assertEqual(flood["severity"], "severe")
        self.assertTrue(flood["stop_work"])

    async def test_unconfirmed_gauge_preserves_independent_nws_warning_and_rain_rules(self):
        warning = {"event": "Flash Flood Warning", "severity": "Extreme", "onset": NOW.isoformat(),
                   "expires": (NOW + timedelta(hours=1)).isoformat()}
        for assessment in (self.assessment(warnings=[warning]), self.assessment(rain=4.1)):
            augmented = rivers.apply_context(assessment, self.context())
            self.assertEqual(augmented["indicators"]["flooding"]["level"], "severe")
            self.assertEqual(augmented["indicators"]["flooding"]["evidence"]["direct_site_rule_level"], "severe")
            alerts.evaluate(augmented)
            self.assertTrue(any(row["stop_work"] for row in alerts.for_site(self.site["id"])))

    async def test_shared_catalogue_coalesces_discovery_and_retains_manual_association(self):
        requests = []
        def respond(request):
            requests.append(request)
            if request.url.path.endswith("/gauges"):
                self.assertEqual(dict(request.url.params), {"srid": "EPSG_4326"})
                return httpx.Response(200, json={"gauges": [self.gauge()]})
            return httpx.Response(200, json=self.gauge())
        second = database.insert_site("TEST second site", 35.121, -90.081)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            first, _ = await asyncio.gather(rivers.context_for(self.site, client), rivers.context_for(second, client))
            rivers.store(self.site["id"], "TEST1", first, confirmed=True, association="manual")
            rivers.save_review(self.site, 'TEST1', {
                'decision': 'confirmed', 'river_name': 'TEST river', 'evidence_type': 'hydraulic_review',
                'evidence': 'TEST ONLY documented hydraulic connection for an isolated fixture.',
                'source_urls': ['https://example.org/test-only-review'], 'reviewed_by': 'TEST reviewer'})
            confirmed = await rivers.context_for(self.site, client)
        self.assertEqual(len(requests), 2)
        self.assertTrue(confirmed["exposure_confirmed"])
        self.assertEqual(confirmed["association"], "manual")
        self.assertIsNone(confirmed["observation"]["secondary"])
        self.assertEqual(confirmed["threshold_units"], {"stage": "ft", "flow": "cfs"})

    async def test_empty_catalogue_is_not_a_provider_failure(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"gauges": []}))) as client:
            context = await rivers.context_for(self.site, client)
        self.assertEqual(context["reason"], "no_gauge_within_10_km")
        self.assertEqual(context["source"]["http_status"], 200)

    async def test_provider_diagnostics_distinguish_http_timeout_schema_and_missing(self):
        cases = [("bad-schema", httpx.Response(200, json=[]), "invalid_schema", 200, 1),
                 ("missing", httpx.Response(404), "missing_data", 404, 1),
                 ("no-content", httpx.Response(204), "missing_data", 204, 1),
                 ("http", httpx.Response(400), "http_error", 400, 1),
                 ("server", httpx.Response(503), "http_error", 503, 2)]
        for label, response, error, code, attempts in cases:
            with self.subTest(label=label):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: response)) as client:
                    result = await rivers.fetch_json(client, rivers.BASE_URL + "/" + label)
                self.assertEqual(result["error"], error)
                self.assertEqual(result["http_status"], code)
                self.assertEqual(result["attempts"], attempts)
        def timeout(request):
            raise httpx.ReadTimeout("TEST_SECRET_MUST_NOT_LEAK", request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            result = await rivers.fetch_json(client, rivers.BASE_URL + "/timeout?api_key=TEST_SECRET_MUST_NOT_LEAK")
        self.assertEqual(result["error"], "timeout")
        self.assertEqual(result["attempts"], 2)
        self.assertIsNone(result["http_status"])
        self.assertNotIn("TEST_SECRET_MUST_NOT_LEAK", json.dumps(result))
        with database.connect() as conn:
            self.assertNotIn("TEST_SECRET_MUST_NOT_LEAK", repr([tuple(row) for row in conn.execute("SELECT * FROM provider_cache")]))

    async def test_429_cooldown_honors_retry_after_without_blocking_other_provider_family(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(429, headers={"Retry-After": "120"}) if "/nwps/" in request.url.path else httpx.Response(200, json={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            first = await rivers.fetch_json(client, rivers.BASE_URL + "/TEST1")
            second = await rivers.fetch_json(client, rivers.BASE_URL + "/TEST2")
            other = await rivers.fetch_json(client, "https://api.water.noaa.gov/hefs/v1/test")
        self.assertEqual(len(requests), 2)
        self.assertEqual(first["error"], "rate_limited")
        self.assertEqual(first["attempts"], 1)
        self.assertEqual(first["retry_at"], (NOW + timedelta(seconds=120)).isoformat())
        self.assertEqual(second["attempts"], 0)
        self.assertEqual(other["status"], "available")

    async def test_service_retry_after_is_honored_and_transient_failure_retries_once(self):
        requests = []
        def retry(request):
            requests.append(request)
            return httpx.Response(503) if len(requests) == 1 else httpx.Response(200, json=self.gauge())
        async with httpx.AsyncClient(transport=httpx.MockTransport(retry)) as client:
            recovered = await rivers.fetch_json(client, rivers.BASE_URL + "/TEST1", validator=rivers._validate_gauge)
        self.assertEqual(recovered["status"], "available")
        self.assertEqual(recovered["attempts"], 2)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(503, headers={"Retry-After": "60"}))) as client:
            result = await rivers.fetch_json(client, rivers.BASE_URL + "/TEST2")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["retry_at"], (NOW + timedelta(seconds=60)).isoformat())

    async def test_failed_refresh_retains_old_data_and_original_timestamps_but_cannot_escalate(self):
        rivers.store(self.site["id"], "TEST1", {}, confirmed=True, association="manual")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=self.gauge()))) as client:
            original = await rivers.context_for(self.site, client)
        with database.connect() as conn:
            conn.execute("UPDATE provider_cache SET expires_at=0")
        later = NOW + timedelta(minutes=6)
        with patch("rivers.utcnow", return_value=later):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(503))) as client:
                stale = await rivers.context_for(self.site, client)
        self.assertEqual(stale["observation"], original["observation"])
        self.assertEqual(stale["source"]["retrieved_at"], original["source"]["retrieved_at"])
        self.assertEqual(stale["source"]["expires_at"], original["source"]["expires_at"])
        self.assertEqual(stale["source"]["last_attempt_at"], later.isoformat())
        self.assertEqual(stale["availability"], "unavailable")
        self.assertTrue(stale["source"]["stale"])
        self.assertEqual(rivers.apply_context(self.assessment(), stale)["indicators"]["flooding"]["level"], "low")

    async def test_missing_and_stale_observations_are_distinct_and_failed_discovery_is_not_no_gauge(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"unexpected": []}))) as client:
            missing_catalogue = await rivers.context_for(self.site, client)
        self.assertEqual(missing_catalogue["reason"], "missing_data")
        rivers.store(self.site["id"], "TEST1", {}, confirmed=False, association="manual")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=self.gauge(status={})))) as client:
            missing = await rivers.context_for(self.site, client)
        self.assertEqual(missing["reason"], "missing_data")
        with database.connect() as conn:
            conn.execute("DELETE FROM provider_cache")
        observed = self.gauge()["status"]["observed"]
        observed["validTime"] = (NOW - timedelta(hours=4)).isoformat()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=self.gauge(status={"observed": observed})))) as client:
            stale = await rivers.context_for(self.site, client)
        self.assertEqual(stale["reason"], "stale_observation")
        self.assertEqual(stale["freshness"]["status"], "stale")

    async def test_validated_list_payload_is_available_for_hefs_adapter_boundary(self):
        def validate(data):
            if not isinstance(data, list):
                raise ValueError("Expected list")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=[{"id": "TEST"}]))) as client:
            result = await rivers.fetch_json(client, "https://api.water.noaa.gov/hefs/v1/test", validator=validate)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["data"], [{"id": "TEST"}])


if __name__ == "__main__":
    unittest.main()
