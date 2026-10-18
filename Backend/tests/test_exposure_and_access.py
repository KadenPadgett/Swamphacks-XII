"""Documented exposure and live-access workflow; only isolated mocked feed data."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
import alerts
import database
import hefs
import rivers
import strike_feed
from main import app

NOW = datetime(2026, 10, 17, 22, tzinfo=timezone.utc)


class ExposureAndAccess(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(database, 'DATABASE_PATH', Path(temporary.name) / 'test.db'))
        self.enterContext(patch.dict(os.environ, {'PLANIT_BACKGROUND_REFRESH': '0', 'PLANIT_WRITE_TOKEN': '',
                                                'PLANIT_LIGHTNING_API_KEY': '', 'PLANIT_ML_URL': ''}))
        self.enterContext(patch('strike_feed.utcnow', return_value=NOW))
        self.enterContext(patch('rivers.utcnow', return_value=NOW))
        self.client = self.enterContext(TestClient(app))
        self.site = database.insert_site('TEST crew site', 35.12, -90.08)
        self.sid = self.site['id']
        self.review = {'decision': 'confirmed', 'river_name': 'TEST river',
            'evidence_type': 'hydraulic_review', 'evidence': 'TEST ONLY documented connection for an isolated test site.',
            'source_urls': ['https://example.org/test-only-review'], 'reviewed_by': 'TEST reviewer'}

    def test_real_bankhead_connection_is_seeded_once_with_provenance_not_probability(self):
        site = next(site for site in database.list_sites() if site.get('eia_plant_id') == 2)
        result = self.client.get(f"/api/sites/{site['id']}/river/exposure").json()
        self.assertTrue(result['site_relevance_verified'])
        self.assertEqual(result['review']['gauge_id'], 'BLDA1')
        self.assertEqual(result['review']['evidence_type'], 'official_facility_connection')
        self.assertIn('https://api.water.noaa.gov/nwps/v1/gauges/BLDA1', result['review']['source_urls'])
        self.assertIsNone(result['site_inundation_probability'])
        database.initialize_database()
        repeated = rivers.exposure_for(site['id'])
        self.assertEqual(result['review']['id'], repeated['review']['id'])
        self.assertEqual(len(database.list_sites()), 62)  # 61 catalog/demo + this test's site
        self.assertEqual(hefs.saved(site['id'])['availability'], 'unavailable')

    def test_legacy_boolean_and_missing_review_cannot_confirm_exposure(self):
        rivers.store(self.sid, 'TEST1', {}, confirmed=True, association='manual')
        self.assertFalse(rivers.saved(self.sid)['exposure_confirmed'])
        self.assertFalse(rivers.exposure_for(self.sid)['site_relevance_verified'])
        response = self.client.put(f'/api/sites/{self.sid}/river', json={'gauge_id': 'TEST1', 'exposure_confirmed': True})
        self.assertEqual(response.status_code, 422)
        with self.assertRaises(ValueError):
            rivers.RiverAssociation(gauge_id='TEST1', exposure_confirmed=True,
                                    review={**self.review, 'source_urls': []})

    def test_review_is_bound_to_gauge_and_site_coordinates_and_withdrawal(self):
        rivers.store(self.sid, 'TEST1', {}, confirmed=True, association='manual')
        rivers.save_review(self.site, 'TEST1', rivers.ExposureReview(**self.review))
        self.assertTrue(rivers.exposure_for(self.sid)['site_relevance_verified'])
        with database.connect() as conn:
            conn.execute('UPDATE sites SET latitude=? WHERE id=?', (36.0, self.sid))
        self.assertEqual(rivers.exposure_for(self.sid)['status'], 'invalidated')
        with database.connect() as conn:
            conn.execute('UPDATE sites SET latitude=? WHERE id=?', (self.site['latitude'], self.sid))
        rivers.store(self.sid, 'OTHER', {}, confirmed=True, association='manual')
        self.assertEqual(rivers.exposure_for(self.sid)['status'], 'invalidated')
        rivers.store(self.sid, 'TEST1', {}, confirmed=False, association='manual')
        self.assertFalse(rivers.exposure_for(self.sid)['site_relevance_verified'])

    def test_startup_does_not_overwrite_manual_bankhead_withdrawal(self):
        site = next(site for site in database.list_sites() if site.get('eia_plant_id') == 2)
        rivers.store(site['id'], 'BLDA1', {}, confirmed=False, association='manual')
        database.initialize_database()
        self.assertFalse(rivers.saved(site['id'])['exposure_confirmed'])
        self.assertFalse(rivers.exposure_for(site['id'])['site_relevance_verified'])

    def test_hefs_and_site_responses_expose_unconfirmed_context_separately(self):
        rivers.store(self.sid, 'TEST1', {}, confirmed=True, association='manual')
        hefs.store_association(self.sid, {'gauge_id': 'TEST1', 'location_id': 'TEST1', 'parameter_id': 'SSTG'})
        result = self.client.get(f'/api/sites/{self.sid}/detail').json()
        self.assertFalse(result['river_exposure']['site_relevance_verified'])
        self.assertEqual(result['river_forecast']['applicability'], 'context_only')
        rivers.save_review(self.site, 'TEST1', rivers.ExposureReview(**self.review))
        verified = self.client.get(f'/api/sites/{self.sid}/hefs').json()
        self.assertEqual(verified['applicability'], 'verified_river_context')
        self.assertEqual(verified['availability'], 'unavailable')  # relevance is not forecast availability
        self.assertIsNone(verified['probability'])

    def test_missing_key_verification_stays_unknown_and_preserves_restart_hold(self):
        event = {'id': 'TEST ONLY', 'time': NOW.isoformat(), 'latitude': self.site['latitude'], 'longitude': self.site['longitude']}
        feed = {'provider': 'TEST ONLY', 'availability': 'available', 'events': [event],
                'coverage_start': (NOW-timedelta(minutes=55)).isoformat(), 'coverage_end': NOW.isoformat()}
        with patch.dict(os.environ, {'PLANIT_LIGHTNING_API_KEY': 'TEST_FIXTURE_ONLY'}):
            before = strike_feed.apply(self.site, feed, now=NOW)
        database.initialize_database()
        response = self.client.post(f'/api/sites/{self.sid}/lightning/verify')
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result['status'], 'stop_work')
        self.assertEqual(result['deadline'], before['deadline'])
        self.assertEqual(result['availability'], 'unknown')
        self.assertEqual(result['provider_verification']['feed_access'], 'not_configured')
        self.assertTrue(alerts.for_site(self.sid)[0]['stop_work'])
        after = strike_feed.state(self.sid, now=NOW+timedelta(minutes=31))
        self.assertEqual(after['status'], 'unknown')
        self.assertTrue(alerts.for_site(self.sid)[0]['stop_work'])

    def mock_provider(self, handler):
        @asynccontextmanager
        async def client():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
                yield transport
        return patch('strike_feed.http_client', client)

    def test_entitlement_failure_is_safe_and_does_not_claim_live_verification(self):
        secret = 'TEST_ONLY_DO_NOT_LEAK'
        with patch.dict(os.environ, {'PLANIT_LIGHTNING_API_KEY': secret}), \
             self.mock_provider(lambda request: httpx.Response(403)):
            result = self.client.post(f'/api/sites/{self.sid}/lightning/verify').json()
        self.assertEqual(result['availability'], 'unknown')
        self.assertEqual(result['provider_verification']['feed_access'], 'not_verified')
        self.assertEqual(result['reason'], 'provider_http_403')
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn('apiKey', json.dumps(result))

    def test_empty_success_verifies_feed_access_not_observed_strikes_and_key_removal_revokes_clearance(self):
        with patch.dict(os.environ, {'PLANIT_LIGHTNING_API_KEY': 'TEST_FIXTURE_ONLY'}), \
             self.mock_provider(lambda request: httpx.Response(200, json={'latitude': [], 'longitude': [], 'validTimeUtc': []})):
            result = self.client.post(f'/api/sites/{self.sid}/lightning/verify').json()
            self.assertEqual(result['status'], 'clear')
            self.assertEqual(result['provider_verification']['feed_access'], 'verified_current')
            self.assertEqual(result['provider_verification']['strike_detection'], 'not_yet_observed')
        read = self.client.get(f'/api/sites/{self.sid}/lightning/strikes').json()
        self.assertEqual(read['status'], 'unknown')
        self.assertFalse(read['coverage_complete'])
        self.assertEqual(read['provider_verification']['feed_access'], 'not_configured')


if __name__ == '__main__':
    unittest.main()
