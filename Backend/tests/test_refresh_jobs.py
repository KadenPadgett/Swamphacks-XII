import asyncio
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

import database
import lightning
import ml_risk
import risk
from main import app, lifespan
import refresh_jobs
from refresh_locks import RefreshBusy, lease
from risk_sources import RiskSources, utcnow


def result(site):
    return {"site": site, "generated_at": utcnow().isoformat(),
            "sources": {"weather": {"status": "available", "retrieved_at": "2026-10-17T00:00:00+00:00"}}}


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(patch.object(database, 'DATABASE_PATH', Path(directory.name) / 'sites.db'))
        database.initialize_database()

    async def wait_until(self, condition, timeout=5):
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.01)

    async def test_lifespan_refreshes_repeatedly_without_dashboard_and_adds_new_site(self):
        calls = {}

        async def refresh(site, sources):
            calls[site['id']] = calls.get(site['id'], 0) + 1
            assessment = result(site)
            database.save_risk(site['id'], assessment)
            return assessment

        with patch.dict(os.environ, {'PLANIT_BACKGROUND_REFRESH': '1'}), \
             patch('refresh_jobs.intervals', return_value={'full': 0.03}), \
             patch('refresh_jobs.risk.refresh_site', side_effect=refresh):
            # Real application lifespan; no dashboard or refresh HTTP request.
            async with lifespan(app):
                app.state.refresh_scheduler.poll_seconds = 0.01
                await self.wait_until(lambda: calls.get(1, 0) >= 1)
                original_time = database.load_risk(1)['generated_at']
                custom = database.insert_site('New monitored site', 29, -81)
                await self.wait_until(lambda: calls.get(custom['id'], 0) >= 1)
                self.assertGreaterEqual(calls[1], 2)
                self.assertNotEqual(database.load_risk(1)['generated_at'], original_time)
                self.assertIsNotNone(database.load_risk(custom['id']))
        self.assertEqual(database.refresh_status()['leases'], [])

    async def test_two_workers_do_not_overlap_and_manual_run_is_rejected(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked(site, sources):
            started.set()
            await release.wait()
            return result(site)

        with patch('refresh_jobs.risk.refresh_site', side_effect=blocked):
            first = asyncio.create_task(refresh_jobs.run('full', sites=[database.find_site(1)]))
            await started.wait()
            try:
                with self.assertRaises(RefreshBusy):
                    await refresh_jobs.run('full', sites=[database.find_site(1)])
                self.assertEqual(len(database.refresh_status()['recent_runs']), 1)
            finally:
                release.set()
                await first
        self.assertEqual(database.refresh_status()['leases'], [])

    async def test_real_full_refresh_saves_and_retains_model_results_until_replaced(self):
        site = database.find_site(1)
        feeds = {name: {'status': 'available', 'data': {}, 'retrieved_at': utcnow().isoformat()}
                 for name in ('weather', 'alerts', 'wildfires', 'wildlife')}
        with patch.object(RiskSources, 'for_site', new=AsyncMock(return_value=feeds)), \
             patch('rivers.augment_assessment', side_effect=lambda site, assessment, client: assessment), \
             patch.dict(os.environ, {'PLANIT_ML_URL': ''}), \
             patch.object(risk, 'snapshot_is_fresh', return_value=False):
            first = await refresh_jobs.run('full', sites=[site])
            self.assertEqual(first['status'], 'succeeded')
            snapshot = database.load_risk(site['id'])
            prediction = {**ml_risk.unavailable('test'), 'availability': 'partial',
                          'model_version': 'teammate-v1', 'current_score': 0,
                          'current': {'time': snapshot['current_risk']['time'], 'score': 0},
                          'received_at': snapshot['generated_at']}
            snapshot['ml_assessment'] = prediction
            database.save_risk(site['id'], snapshot)
            await refresh_jobs.run('full', sites=[site])
            retained = database.load_risk(site['id'])['ml_assessment']
            self.assertTrue(retained['stale'])
            self.assertEqual(retained['current'], prediction['current'])
            self.assertEqual(retained['received_at'], prediction['received_at'])
            self.assertEqual(retained['input_generated_at'], snapshot['generated_at'])
            self.assertEqual(retained['latest_attempt']['reason'], 'trained_model_not_configured')
            with patch.object(ml_risk, 'predict', new=AsyncMock(return_value={
                    **prediction, 'model_version': 'teammate-v2', 'current_score': 7})):
                await refresh_jobs.run('full', sites=[site])
            replaced = database.load_risk(site['id'])['ml_assessment']
            self.assertFalse(replaced['stale'])
            self.assertEqual(replaced['model_version'], 'teammate-v2')
            self.assertEqual(replaced['current_score'], 7)

    async def test_lightning_runs_while_full_refresh_is_blocked(self):
        full_started, release = asyncio.Event(), asyncio.Event()

        async def full(site, sources):
            full_started.set()
            await release.wait()
            return result(site)

        async def fast(site, sources):
            return {'site_id': site['id'], 'sources': {'alerts': {'status': 'available'}}}

        with patch('refresh_jobs.risk.refresh_site', side_effect=full), \
             patch('refresh_jobs.lightning.refresh_site', side_effect=fast):
            task = asyncio.create_task(refresh_jobs.run('full', sites=[database.find_site(1)]))
            await full_started.wait()
            try:
                async with asyncio.timeout(2):
                    quick = await refresh_jobs.run('lightning', sites=[database.find_site(1)])
                self.assertEqual(quick['status'], 'succeeded')
                self.assertFalse(task.done())
            finally:
                release.set()
                await task

    async def test_failures_keep_last_success_and_do_not_stop_other_sites(self):
        sites = database.list_sites()[:3]
        with patch('refresh_jobs.risk.refresh_site', side_effect=lambda site, sources: result(site)):
            await refresh_jobs.run('full', sites=sites)
        original = {row['site_id']: row['last_success_at'] for row in database.refresh_status()['sites']}

        async def faulty(site, sources):
            if site['id'] == sites[0]['id']:
                response = result(site)
                response['sources']['weather'] = {'status': 'unavailable', 'error': 'HTTP 503'}
                return response
            if site['id'] == sites[1]['id']:
                raise RuntimeError('test failure')
            return result(site)

        with patch('refresh_jobs.risk.refresh_site', side_effect=faulty), \
             self.assertLogs('refresh_jobs', level='ERROR'):
            response = await refresh_jobs.run('full', sites=sites)
        self.assertEqual(response['status'], 'partial')
        self.assertEqual(len(response['failures']), 2)
        rows = {row['site_id']: row for row in database.refresh_status()['sites']}
        for site in sites[:2]:
            self.assertEqual(rows[site['id']]['last_success_at'], original[site['id']])
            self.assertIsNotNone(rows[site['id']]['last_failure_at'])
        self.assertIsNone(rows[sites[2]['id']]['last_error'])
        self.assertIsNotNone(database.refresh_status()['jobs'][0]['last_success_at'])

    async def test_cancel_releases_lease_and_records_interrupted_run(self):
        started = asyncio.Event()

        async def blocked(site, sources):
            started.set()
            await asyncio.Event().wait()

        with patch('refresh_jobs.risk.refresh_site', side_effect=blocked):
            task = asyncio.create_task(refresh_jobs.run('full', sites=[database.find_site(1)]))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(database.refresh_status()['leases'], [])
        self.assertEqual(database.latest_run('full')['status'], 'interrupted')

    async def test_stale_lease_recovery_does_not_let_old_owner_release_new_lock(self):
        self.assertTrue(database.claim_lease('job:full', 'old', 100, 60))
        self.assertFalse(database.claim_lease('job:full', 'new', 110, 60))
        self.assertTrue(database.claim_lease('job:full', 'new', 161, 60))
        self.assertFalse(database.renew_lease('job:full', 'old', 162, 60))
        database.release_lease('job:full', 'old')
        self.assertFalse(database.claim_lease('job:full', 'third', 163, 60))

    async def test_fast_monitor_fetches_no_wildlife_and_preserves_cached_source_times(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.host == 'api.open-meteo.com':
                self.assertEqual(request.url.params['current'], 'weather_code')
                self.assertNotIn('hourly', request.url.params)
                return httpx.Response(200, json={'current': {'time': '2026-10-17T12:00', 'weather_code': 95},
                                                'utc_offset_seconds': -14400})
            self.assertEqual(request.url.host, 'api.weather.gov')
            return httpx.Response(200, json={'features': []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            feeds = RiskSources(client)
            first = await lightning.refresh_site(database.find_site(1), feeds)
            second = await lightning.refresh_site(database.find_site(1), feeds)
        self.assertEqual(len(requests), 2)
        for name in ('weather', 'alerts'):
            self.assertEqual(first['sources'][name]['retrieved_at'], second['sources'][name]['retrieved_at'])
            self.assertTrue(second['sources'][name]['cache_hit'])
        self.assertNotEqual(first['checked_at'], second['checked_at'])
        self.assertEqual(second['weather_observed_at'], '2026-10-17T12:00')
        self.assertEqual(second['indicator']['measured_lightning_strikes']['availability'], 'unknown')
        self.assertEqual(second['indicator']['coverage'], 'partial')
        self.assertEqual(second['indicator']['level'], 'high')


class StatusEndpointTests(unittest.TestCase):
    def test_status_and_lightning_are_read_only_and_full_overlap_returns_409(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(database, 'DATABASE_PATH', Path(directory) / 'sites.db'), \
             patch.dict(os.environ, {'PLANIT_BACKGROUND_REFRESH': '0'}), TestClient(app) as client:
            self.assertEqual(client.get('/api/refresh/status').status_code, 200)
            self.assertEqual(client.get('/api/sites/1/lightning').json()['status'], 'pending')
            self.assertEqual(client.get('/api/sites/999999/lightning').status_code, 404)
            import time
            database.claim_lease('job:full', 'another-worker', time.time(), 60)
            self.assertEqual(client.post('/api/risks/refresh').status_code, 409)


if __name__ == '__main__':
    unittest.main()
