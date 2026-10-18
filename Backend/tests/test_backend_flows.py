"""Synthetic hazards and model output are confined to these temporary databases."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image
from fastapi.testclient import TestClient
import alerts
import briefings
import database
import ml_risk
import notifications
import refresh_jobs
import risk
import rivers
import strike_feed
from main import app

NOW = datetime(2026, 8, 12, 22, 0, tzinfo=timezone.utc)


def feeds(heat=80):
    start = NOW.replace(hour=0)
    history_times = [(start - timedelta(days=2) + timedelta(hours=i)).isoformat() for i in range(72)]
    future_times = [(NOW + timedelta(hours=i)).isoformat() for i in range(1, 49)]
    weather = {'timezone': 'America/New_York', 'utc_offset_seconds': 0,
        'current': {'time': NOW.isoformat(), 'weather_code': 0, 'apparent_temperature': heat,
                    'wind_gusts_10m': 5, 'soil_moisture_3_to_9cm': 0.2},
        'recent_hourly': {'time': history_times, 'precipitation': [0] * len(history_times)},
        'hourly': {'time': future_times, 'precipitation': [0] * 48, 'apparent_temperature': [heat] * 48,
                   'weather_code': [0] * 48, 'wind_gusts_10m': [5] * 48, 'soil_moisture_3_to_9cm': [0.2] * 48}}
    return {name: {'status': 'available', 'retrieved_at': NOW.isoformat(), 'data': data}
            for name, data in [('weather', weather), ('alerts', {'alerts': []}),
                               ('wildfires', {'events': []}), ('wildlife', {'total_results': 0})]}


class BackendFlows(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        # These tests drive their own deterministic refresh after creating a site.
        self.enterContext(patch('main.initial_assessments.queue'))
        self.enterContext(patch.object(database, 'DATABASE_PATH', Path(temp.name) / 'sites.db'))
        self.enterContext(patch.dict(os.environ, {'PLANIT_ML_URL': '', 'PLANIT_LIGHTNING_API_KEY': '',
                                                'PLANIT_BACKGROUND_REFRESH': '0', 'PLANIT_WRITE_TOKEN': '',
                                                'PLANIT_UPLOAD_DIR': str(Path(temp.name) / 'photos')}))
        self.client = self.enterContext(TestClient(app))
        self.site = self.client.post('/api/sites', json={'name': 'TEST river site', 'latitude': 35.12,
                                                        'longitude': -90.08, 'timezone': 'America/Chicago'}).json()
        self.sid = self.site['id']

    def assessment(self, heat=80):
        with patch('risk.utcnow', return_value=NOW):
            assessment = risk.assess(database.find_site(self.sid), feeds(heat))
        assessment['ml_assessment'] = ml_risk.unavailable('trained_model_not_configured')
        assessment['ml_configuration_id'] = ml_risk.configuration_id()
        return assessment

    async def test_complete_flow_without_ml_create_refresh_alert_photo_report_briefing(self):
        with patch('risk_sources.RiskSources.for_site', new=AsyncMock(return_value=feeds(115))), \
             patch('risk.utcnow', return_value=NOW), \
             patch('rivers.augment_assessment', side_effect=lambda site, assessment, client: assessment):
            outcome = await refresh_jobs.run('full', sites=[database.find_site(self.sid)], trigger='background')
        self.assertEqual(outcome['count'], 1)
        self.assertTrue(alerts.for_site(self.sid))
        image = BytesIO()
        Image.new('RGB', (10, 10), 'green').save(image, 'PNG')
        photo = self.client.post(f'/api/sites/{self.sid}/photos', content=image.getvalue(), headers={'Content-Type': 'image/png'})
        self.assertEqual(photo.status_code, 201)
        report = self.client.post(f'/api/sites/{self.sid}/reports', json={
            'hazard_type': 'wildlife', 'description': 'TEST gator near access road',
            'latitude': 35.1201, 'longitude': -90.0801, 'photo_reference': photo.json()['photo_reference']})
        self.assertEqual(report.status_code, 201)
        self.assertEqual(self.client.get(photo.json()['photo_url']).status_code, 200)
        # A separate browser/client sees the event scoped to the affected site.
        with TestClient(app) as viewer:
            received = viewer.get(f'/api/sites/{self.sid}/notifications').json()['notifications']
            self.assertIn('crew_report_created', [event['kind'] for event in received])
            self.assertEqual(viewer.get('/api/sites/1/notifications').json()['notifications'], [])
        detail = self.client.get(f'/api/sites/{self.sid}/detail').json()
        self.assertIsNone(detail['ml_predictions']['current_score'])
        self.assertEqual(detail['ml_predictions']['availability'], 'unavailable')
        self.assertTrue(detail['required_safety_actions'])
        self.assertEqual(len(detail['active_crew_reports']), 1)
        briefing = await briefings.generate(self.sid, now=NOW)
        self.assertTrue(briefing['created'])
        self.assertEqual(briefing['briefing']['ml_predictions']['availability'], 'unavailable')
        self.assertTrue(all(row['score'] is None for row in briefing['briefing']['ml_predictions']['hourly_scores']))
        self.assertTrue(briefing['briefing']['missing_information'])
        self.assertEqual(len(briefing['briefing']['unresolved_crew_reports']), 1)
        self.assertIn('briefing', [event['kind'] for event in notifications.events(self.sid)])

    async def test_alert_episode_deduplicates_worsens_and_retains_when_unknown(self):
        assessment = self.assessment(102)
        alerts.evaluate(assessment)
        alerts.evaluate(assessment)
        self.assertEqual(len(alerts.for_site(self.sid)), 1)
        self.assertEqual(len(notifications.events(self.sid)), 1)
        alerts.evaluate(self.assessment(115))
        self.assertEqual(len(alerts.for_site(self.sid)), 1)
        self.assertEqual(len(notifications.events(self.sid)), 2)
        unknown = self.assessment(80)
        unknown['current_risk']['indicators']['extreme_heat'].update(level='unknown', coverage='unavailable')
        alerts.evaluate(unknown)
        self.assertEqual(len(alerts.for_site(self.sid)), 1)
        alerts.evaluate(self.assessment(80))
        self.assertEqual(alerts.for_site(self.sid), [])
        self.assertEqual(alerts.for_site(self.sid, 'resolved')[0]['status'], 'resolved')

    def strike(self, identifier, instant, lat=None, lon=None):
        return {'id': identifier, 'time': instant.isoformat(), 'latitude': lat or self.site['latitude'],
                'longitude': lon or self.site['longitude']}

    def feed(self, events, now):
        return {'provider': 'isolated_test_fixture', 'availability': 'available', 'events': events,
                'coverage_start': (now-timedelta(minutes=55)).isoformat(), 'coverage_end': now.isoformat()}

    async def test_strike_radius_duplicates_reset_restart_and_missing_coverage(self):
        self.enterContext(patch.dict(os.environ, {'PLANIT_LIGHTNING_API_KEY': 'TEST_FIXTURE_ONLY'}))
        first = self.strike('first', NOW)
        one = strike_feed.apply(self.site, self.feed([first], NOW), now=NOW)
        self.assertEqual(one['status'], 'stop_work')
        self.assertEqual(one['countdown_remaining_seconds'], 1800)
        self.assertEqual(len(notifications.events(self.sid)), 1)
        later = NOW+timedelta(minutes=10)
        duplicate = strike_feed.apply(self.site, self.feed([first, self.strike('older', NOW-timedelta(minutes=1))], later), now=later)
        self.assertEqual(duplicate['deadline'], one['deadline'])
        self.assertEqual(len(notifications.events(self.sid)), 1)
        reset = strike_feed.apply(self.site, self.feed([self.strike('new', later)], later), now=later)
        self.assertEqual(reset['deadline'], (later+timedelta(minutes=30)).isoformat())
        # Reinitializing simulates a backend restart: state is in SQLite, not memory.
        database.initialize_database()
        self.assertEqual(strike_feed.state(self.sid, now=later)['deadline'], reset['deadline'])
        after = later+timedelta(minutes=31)
        missing = strike_feed.apply(self.site, {'availability': 'unknown', 'reason': 'feed_missing', 'events': None}, now=after)
        self.assertEqual(missing['status'], 'unknown')
        self.assertTrue(alerts.for_site(self.sid)[0]['stop_work'])
        cleared = strike_feed.apply(self.site, self.feed([], after), now=after)
        self.assertEqual(cleared['status'], 'clear')
        self.assertEqual(alerts.for_site(self.sid), [])
        far = strike_feed.apply(self.site, self.feed([self.strike('far', after, lat=40)], after), now=after)
        self.assertEqual(far['deadline'], reset['deadline'])

    async def test_missing_strike_adapter_and_provider_failure_stay_unknown(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
            absent = await strike_feed.fetch(self.site, client)
            self.assertIsNone(absent['events'])
            with patch.dict(os.environ, {'PLANIT_LIGHTNING_API_KEY': 'TEST_NOT_A_REAL_KEY'}):
                failed = await strike_feed.fetch(self.site, client)
        self.assertEqual(failed['availability'], 'unavailable')
        self.assertNotIn('TEST_NOT_A_REAL_KEY', json.dumps(failed))
        with self.assertRaises(ValueError):
            strike_feed.map_twc({'latitude': [999], 'longitude': [-90], 'validTimeUtc': [NOW.isoformat()]}, NOW)

    async def test_sse_is_site_scoped_and_resumable(self):
        first = notifications.publish(self.sid, 'test_event', {'test_only': True}, 'test:1')
        notifications.publish(1, 'test_event', {'wrong_site': True}, 'test:other')
        second = notifications.publish(self.sid, 'test_event', {'test_only': True}, 'test:2')
        request = type('Request', (), {'is_disconnected': AsyncMock(return_value=False)})()
        stream = notifications.event_stream(self.sid, first, request)
        event = await anext(stream)
        await stream.aclose()
        self.assertIn(f'id: {second}', event)
        self.assertNotIn('wrong_site', event)

    async def test_reports_validate_photo_site_and_preserve_resolved_pin(self):
        invalid = self.client.post(f'/api/sites/{self.sid}/reports', json={'hazard_type': 'wildlife',
            'description': 'test', 'latitude': 91, 'longitude': 0})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(self.client.post(f'/api/sites/{self.sid}/photos', content=b'not-image',
                                         headers={'Content-Type': 'image/png'}).status_code, 422)
        created = self.client.post(f'/api/sites/{self.sid}/reports', json={'hazard_type': 'wildlife',
            'description': 'TEST gator', 'latitude': 35.1, 'longitude': -90.1}).json()
        resolved = self.client.post(f'/api/sites/{self.sid}/reports/{created["id"]}/resolve')
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.json()['latitude'], 35.1)
        self.assertEqual(self.client.get(f'/api/sites/{self.sid}/reports?status=active').json()['reports'], [])
        self.assertEqual(len(self.client.get(f'/api/sites/{self.sid}/reports').json()['reports']), 1)

    async def test_briefing_6pm_local_once_dst_and_missing_timezone(self):
        briefings.set_timezone(self.sid, 'America/New_York')
        early = datetime(2026, 10, 31, 21, 59, tzinfo=timezone.utc)
        due = early + timedelta(minutes=1)
        self.assertEqual((await briefings.tick(early))['generated'], 0)
        self.assertEqual((await briefings.tick(due))['generated'], 1)
        self.assertEqual((await briefings.tick(due+timedelta(minutes=1)))['generated'], 0)
        saved = briefings.list_briefings(self.sid)[0]
        self.assertEqual(saved['target_date'], '2026-11-01')
        self.assertEqual(saved['period']['hours'], 25)
        self.assertEqual(saved['ml_predictions']['availability'], 'unavailable')
        self.assertEqual(len([e for e in notifications.events(self.sid) if e['kind']=='briefing']), 1)
        spring = await briefings.generate(self.sid, now=datetime(2026,3,7,23,tzinfo=timezone.utc))
        self.assertEqual(spring['briefing']['period']['hours'], 23)
        with self.assertRaises(ValueError):
            await briefings.generate(1, now=due)

    async def test_river_site_heavy_rain_warning_has_context_and_evidence(self):
        assessment = self.assessment()
        assessment['current_risk']['indicators']['flooding']['evidence']['preceding_24h_precipitation_in'] = 1.5
        context = {'availability': 'available', 'exposure_confirmed': True,
                   'exposure_verification': {'site_relevance_verified': True},
                   'gauge': {'id': 'MEMT1', 'name': 'Mississippi River at Memphis', 'distance_km': 0.5},
                   'observation': {'validTime': NOW.isoformat(), 'primary': 29, 'primaryUnit': 'ft', 'floodCategory': 'action'},
                   'thresholds': {'action': {'stage': 28}}, 'source': {'retrieved_at': NOW.isoformat()}}
        augmented = rivers.apply_context(assessment, context)
        alerts.evaluate(augmented)
        flood = [row for row in alerts.for_site(self.sid) if row['hazard']=='flooding'][0]
        self.assertEqual(flood['severity'], 'high')
        self.assertEqual(flood['evidence']['river_context']['gauge']['id'], 'MEMT1')
        self.assertTrue(flood['evidence']['river_warning_reasons'])
        self.assertIsNone(augmented['ml_assessment']['current_score'])

    async def test_map_order_missing_model_is_rule_based_and_pending_not_zero(self):
        database.save_risk(self.sid, self.assessment(115))
        alerts.evaluate(self.assessment(115))
        response = self.client.get('/api/risks').json()
        self.assertEqual(response['ranking']['type'], 'rule_based')
        self.assertFalse(response['ranking']['ml_scores_used'])
        self.assertEqual(response['sites'][0]['site_id'], self.sid)
        self.assertTrue(all(row['current_score'] is None for row in response['sites']))
        self.assertTrue(any(row['status']=='pending' for row in response['sites']))

    async def test_next_day_forecast_filters_hours_and_retains_highest_periods(self):
        snapshot = self.assessment(105)
        database.save_risk(self.sid, snapshot)
        briefing = (await briefings.generate(self.sid, now=NOW))['briefing']
        start = datetime.fromisoformat(briefing['period']['start_utc'])
        end = datetime.fromisoformat(briefing['period']['end_utc'])
        hours = briefing['rule_based_assessment']['hourly']
        self.assertEqual(len(hours), 24)
        self.assertTrue(all(start <= datetime.fromisoformat(row['time']) < end for row in hours))
        self.assertEqual(briefing['rule_based_assessment']['highest_known_level'], 'high')
        self.assertTrue(briefing['rule_based_assessment']['highest_risk_periods'])
        self.assertEqual(len(briefing['environmental_forecasts']['hourly']), 24)

    async def test_real_river_adapter_discovers_gauge_and_marks_old_observation_stale(self):
        requests = []
        def respond(request):
            requests.append(request)
            if request.url.path.endswith('/gauges'):
                self.assertEqual(request.url.params['srid'], 'EPSG_4326')
                return httpx.Response(200, json={'gauges': [{'lid': 'TEST1', 'latitude': 35.12, 'longitude': -90.08}]})
            return httpx.Response(200, json={'lid': 'TEST1', 'name': 'TEST river gauge',
                'latitude': 35.12, 'longitude': -90.08, 'status': {'observed': {
                    'validTime': (NOW-timedelta(days=2)).isoformat(), 'primary': 29, 'secondary': -999,
                    'floodCategory': 'major_flooding'}}, 'flood': {'categories': {'minor': {'stage': -9999}}}})
        with patch('rivers.utcnow', return_value=NOW):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                context = await rivers.context_for(database.find_site(self.sid), client)
        self.assertEqual(len(requests), 2)
        self.assertFalse(context['exposure_confirmed'])
        self.assertEqual(context['availability'], 'unavailable')
        self.assertIsNone(context['observation']['secondary'])
        assessment = rivers.apply_context(self.assessment(), context)
        self.assertNotEqual(assessment['indicators']['flooding']['level'], 'severe')

    async def test_cors_development_and_optional_write_protection(self):
        preflight = self.client.options('/api/sites', headers={'Origin': 'http://localhost:5173',
            'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'Authorization,Content-Type'})
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(preflight.headers['access-control-allow-origin'], 'http://localhost:5173')
        with patch.dict(os.environ, {'PLANIT_WRITE_TOKEN': 'isolated-test-token'}):
            self.assertEqual(self.client.post('/api/sites', json={'name':'test','latitude':0,'longitude':0}).status_code, 401)
            self.assertEqual(self.client.post('/api/sites', json={'name':'test','latitude':0,'longitude':0},
                headers={'Authorization': 'Bearer isolated-test-token'}).status_code, 201)

    async def test_saved_detail_and_refreshed_site_response_have_null_model_fields(self):
        snapshot = self.assessment()
        with patch('risk.get_site_risk', new=AsyncMock(return_value=snapshot)):
            response = self.client.get(f'/api/sites/{self.sid}/risk')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['ml_predictions']['current_score'])
        self.assertIn('rule_based_assessment', response.json())
        self.assertIn('required_safety_actions', response.json())
        self.assertEqual(self.client.get(f'/api/sites/{self.sid}').json()['timezone'], 'America/Chicago')

    async def test_model_adapter_routes_accept_only_matching_real_contract(self):
        snapshot = self.assessment()
        database.save_risk(self.sid, snapshot)
        request = ml_risk.request_for(snapshot)
        # Explicit TEST-only sample output. No production route generates samples.
        payload = {'request_id': request['request_id'], 'model_version': 'TEST_FIXTURE_ONLY',
                   'predictions': [{'time': request['samples'][0]['time'], 'score': 7.5, 'construction_hazards': ['TEST heat']}]}
        # The fixture assessment/target is NOW, not the wall clock on the test
        # runner. Serialization must still expire a delayed submission on reads.
        with patch('ml_risk.utcnow', return_value=NOW):
            response = self.client.post(f'/api/sites/{self.sid}/ml-output', json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['current_score'], 7.5)
        with patch('ml_risk.utcnow', return_value=NOW + timedelta(minutes=6)):
            expired = self.client.post(f'/api/sites/{self.sid}/ml-output', json=payload)
        self.assertEqual(expired.status_code, 200)
        self.assertIsNone(expired.json()['current_score'])
        self.assertEqual(expired.json()['current']['availability'], 'stale')
        self.assertEqual(expired.json()['retained_predictions']['current_score'], 7.5)
        payload['request_id'] = 'wrong-input'
        self.assertEqual(self.client.post(f'/api/sites/{self.sid}/ml-output', json=payload).status_code, 409)
        payload['predictions'][0]['score'] = 11
        self.assertEqual(self.client.post(f'/api/sites/{self.sid}/ml-output', json=payload).status_code, 422)

    async def test_invalid_site_coordinates_and_timezone_rejected(self):
        for fields in ({'latitude': 91, 'longitude': 0}, {'latitude': 0, 'longitude': -181},
                       {'latitude': 0, 'longitude': 0, 'timezone': 'not/a_zone'}):
            self.assertEqual(self.client.post('/api/sites', json={'name': 'bad', **fields}).status_code, 422)


if __name__ == '__main__':
    unittest.main()
