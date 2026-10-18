"""Xweather contract and safety tests: no .env reads and no live credentials."""
from datetime import datetime, timedelta, timezone
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
import database
import alerts
import strike_feed
import xweather_lightning as xweather

NOW = datetime(2026, 10, 17, 22, tzinfo=timezone.utc)


class XweatherLightning(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(database, 'DATABASE_PATH', Path(temporary.name) / 'test.db'))
        self.enterContext(patch.dict(os.environ, {'PLANIT_LIGHTNING_PROVIDER': 'xweather',
            'PLANIT_XWEATHER_API_KEY': 'TESTID_TESTSECRET', 'PLANIT_XWEATHER_CLIENT_ID': '',
            'PLANIT_XWEATHER_CLIENT_SECRET': '', 'PLANIT_LIGHTNING_API_KEY': '', 'PLANIT_BACKGROUND_REFRESH': '0'}))
        self.enterContext(patch('xweather_lightning.utcnow', return_value=NOW))
        self.enterContext(patch('strike_feed.utcnow', return_value=NOW))
        self.enterContext(patch('xweather_lightning._retry_until', None))
        database.initialize_database()
        self.site = database.insert_site('TEST Xweather location', 25.75, -80.7)

    def event(self, identifier='TEST-event', when=NOW, latitude=25.75, longitude=-80.7, pulse='cg'):
        return {'id': identifier, 'loc': {'lat': latitude, 'long': longitude},
            'ob': {'dateTimeISO': when.isoformat(), 'pulse': {'type': pulse}}}

    async def fetch(self, handler):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await strike_feed.fetch(self.site, client)

    def test_provider_selection_and_separate_credentials_do_not_reuse_twc_key(self):
        self.assertTrue(strike_feed.configured())
        self.assertEqual(strike_feed.provider_name(), 'xweather')
        with patch.dict(os.environ, {'PLANIT_XWEATHER_API_KEY': '', 'PLANIT_LIGHTNING_API_KEY': 'TEST_TWC_ONLY'}):
            self.assertFalse(strike_feed.configured())
        with patch.dict(os.environ, {'PLANIT_XWEATHER_API_KEY': '', 'PLANIT_XWEATHER_CLIENT_ID': 'ID',
                                    'PLANIT_XWEATHER_CLIENT_SECRET': 'SECRET'}):
            self.assertEqual(xweather.credentials(), ('ID', 'SECRET'))

    async def test_documented_query_and_empty_warning_do_not_manufacture_30_minute_coverage(self):
        def response(request):
            query = request.url.params
            self.assertEqual(request.url.host, 'data.api.xweather.com')
            self.assertEqual(query['radius'], '10miles')
            self.assertEqual(query['filter'], 'all')
            self.assertEqual(int(query['to'])-int(query['from']), 240)
            self.assertEqual(query['client_id'], 'TESTID')
            self.assertEqual(query['client_secret'], 'TESTSECRET')
            return httpx.Response(200, json={'success': True, 'response': [], 'error': {'code': 'warn_no_data'}})
        feed = await self.fetch(response)
        result = strike_feed.apply(self.site, feed, now=NOW)
        self.assertEqual(result['availability'], 'available')
        self.assertFalse(result['coverage_complete'])
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['provider_verification']['feed_access'], 'verified_current')
        self.assertEqual(result['provider_verification']['strike_detection'], 'not_yet_observed')

    async def test_real_adapter_records_cg_and_ic_coordinates_and_resets_only_for_new_strikes(self):
        feeds = []
        for identifier, at, kind in (('first', NOW, 'cg'), ('second', NOW+timedelta(minutes=2), 'IC')):
            with patch('xweather_lightning.utcnow', return_value=at):
                feed = await self.fetch(lambda req: httpx.Response(200, json={
                    'success': True, 'error': None, 'response': [self.event(identifier, at, pulse=kind)]}))
            feeds.append(feed)
            state = strike_feed.apply(self.site, feed, now=at)
            self.assertEqual(state['deadline'], (at+timedelta(minutes=30)).isoformat())
        deadline = state['deadline']
        repeated = strike_feed.apply(self.site, feeds[0], now=NOW+timedelta(minutes=2))
        self.assertEqual(repeated['deadline'], deadline)
        self.assertEqual(len(alerts.for_site(self.site['id'])), 1)
        database.initialize_database()
        self.assertEqual(strike_feed.state(self.site['id'], now=NOW+timedelta(minutes=2))['deadline'], deadline)

    def test_continuous_windows_survive_restart_but_a_long_outage_requires_new_warmup(self):
        def window(at):
            return {'provider': 'xweather', 'availability': 'available', 'events': [],
                    'coverage_start': (at-timedelta(minutes=4)).isoformat(), 'coverage_end': at.isoformat()}
        for minute in range(0, 29, 2):
            at = NOW+timedelta(minutes=minute)
            result = strike_feed.apply(self.site, window(at), now=at)
            if minute == 12:
                database.initialize_database()
        self.assertEqual(result['status'], 'clear')
        at = NOW+timedelta(minutes=40)
        result = strike_feed.apply(self.site, window(at), now=at)
        self.assertFalse(result['coverage_complete'])
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['coverage_start'], (at-timedelta(minutes=4)).isoformat())

    async def test_bounded_pagination_failure_still_stops_work_but_cannot_clear(self):
        calls = []
        def response(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(200, json={'success': True, 'error': None,
                    'response': [self.event(str(i)) for i in range(1000)]})
            return httpx.Response(503)
        feed = await self.fetch(response)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].url.params['from'], calls[1].url.params['from'])
        self.assertEqual(calls[1].url.params['skip'], '1000')
        self.assertEqual(feed['availability'], 'unavailable')
        result = strike_feed.apply(self.site, feed, now=NOW)
        self.assertEqual(result['status'], 'stop_work')
        self.assertFalse(result['coverage_complete'])
        self.assertEqual(result['availability'], 'unknown')

    async def test_http_and_api_failures_stay_unknown_without_secret_or_url_in_outputs(self):
        for response in (httpx.Response(403), httpx.Response(200, json={'success': False,
                'error': {'code': 'invalid_client', 'description': 'TESTSECRET'}, 'response': []}),
                httpx.Response(200, json={'success': True, 'error': None, 'response': [self.event(latitude=999)]})):
            feed = await self.fetch(lambda req: response)
            self.assertEqual(feed['availability'], 'unavailable')
            self.assertNotIn('TESTSECRET', json.dumps(feed))
            self.assertNotIn('client_secret', json.dumps(feed))

    async def test_rate_limit_honors_retry_after_without_more_requests(self):
        calls = []
        def response(request):
            calls.append(request)
            return httpx.Response(429, headers={'Retry-After': '120'})
        first = await self.fetch(response)
        second = await self.fetch(response)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first['reason'], 'provider_http_429')
        self.assertEqual(second['reason'], 'xweather_rate_limit_cooldown')

    async def test_httpx_info_logging_redacts_authenticated_query(self):
        output = io.StringIO()
        logger = logging.getLogger('httpx')
        handler = logging.StreamHandler(output)
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            await self.fetch(lambda req: httpx.Response(200, json={'success': True, 'response': [], 'error': None}))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        self.assertIn('xweather.com/lightning/closest', output.getvalue())
        self.assertNotIn('TESTSECRET', output.getvalue())
        self.assertNotIn('client_id', output.getvalue())

    def test_switching_provider_cannot_reuse_old_verified_coverage_for_clearance(self):
        with patch.dict(os.environ, {'PLANIT_LIGHTNING_PROVIDER': 'the_weather_company', 'PLANIT_LIGHTNING_API_KEY': 'TEST_ONLY'}):
            old = strike_feed.apply(self.site, {'provider': 'the_weather_company', 'availability': 'available', 'events': [],
                'coverage_start': (NOW-timedelta(minutes=55)).isoformat(), 'coverage_end': NOW.isoformat()}, now=NOW)
        self.assertEqual(old['status'], 'clear')
        switched = strike_feed.state(self.site['id'], now=NOW)
        self.assertEqual(switched['status'], 'unknown')
        self.assertEqual(switched['provider_verification']['feed_access'], 'not_verified')


if __name__ == '__main__':
    unittest.main()
