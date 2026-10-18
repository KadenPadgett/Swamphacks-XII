import asyncio
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import database
import initial_assessments
import main
import metar_model
import risk
import site_responses
from risk_sources import utcnow


class InitialAssessmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(patch.object(database, 'DATABASE_PATH', Path(directory.name) / 'sites.db'))
        database.initialize_database()

    async def asyncTearDown(self):
        await initial_assessments.stop()

    async def wait_for(self, site_id, state):
        async with asyncio.timeout(3):
            while database.initial_assessment(site_id)['status'] != state:
                await asyncio.sleep(.01)

    async def test_create_returns_pending_then_completed_with_partial_sources(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def assess(site):
            started.set()
            await release.wait()
            result = {'site': site, 'generated_at': utcnow().isoformat(),
                      'sources': {'weather': {'status': 'available'},
                                  'wildfires': {'status': 'unavailable', 'error': 'provider timeout'}},
                      'metar': {'availability': 'unavailable', 'reason': 'no_recent_observation'},
                      'weather_model': metar_model.unavailable('metar_no_recent_observation')}
            database.save_risk(site['id'], result)
            return result

        with patch.object(risk, 'get_site_risk', side_effect=assess):
            created = await main.create_site(main.SiteCreate(name='New job', latitude=30, longitude=-81))
            site_id = created['id']
            self.assertEqual(created['initial_assessment']['status'], 'pending')
            await started.wait()
            self.assertEqual(database.initial_assessment(site_id)['status'], 'running')
            self.assertEqual(site_responses.overview([database.find_site(site_id)])['sites'][0]['initial_assessment']['status'], 'running')
            release.set()
            await self.wait_for(site_id, 'completed')
        detail = site_responses.detail(database.find_site(site_id))
        self.assertEqual(detail['initial_assessment']['status'], 'completed')
        self.assertIsNotNone(detail['initial_assessment']['finished_at'])
        self.assertEqual({f['source'] for f in detail['initial_assessment']['source_failures']},
                         {'wildfires', 'metar', 'weather_model'})
        self.assertEqual(detail['weather_model']['target'], metar_model.TARGET)
        self.assertEqual(detail['weather_model']['probability'], None)

    async def test_failure_and_retry_state(self):
        site = database.insert_site('Retry job', 30, -81)
        with patch.object(risk, 'get_site_risk', new=AsyncMock(side_effect=RuntimeError('weather gateway down'))):
            await initial_assessments.run(site['id'])
        failed = database.initial_assessment(site['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('weather gateway down', failed['error'])
        self.assertGreater(failed['next_retry_at'], failed['finished_at'])
        self.assertFalse(database.claim_initial_assessment(site['id'], utcnow().isoformat()))
        self.assertTrue(database.retry_initial_assessment(site['id'], utcnow().isoformat()))
        with patch.object(risk, 'get_site_risk', new=AsyncMock(return_value={
                'site': site, 'sources': {}, 'metar': {'availability': 'available'},
                'weather_model': {'availability': 'available'}})):
            self.assertTrue(await initial_assessments.run(site['id']))
        self.assertEqual(database.initial_assessment(site['id'])['attempts'], 2)
        self.assertEqual(database.initial_assessment(site['id'])['status'], 'completed')

    async def test_retry_endpoint_queues_failed_assessment(self):
        site = database.insert_site('Retry endpoint', 30, -81)
        claimed_at = utcnow().isoformat()
        database.claim_initial_assessment(site['id'], claimed_at)
        database.finish_initial_assessment(site['id'], claimed_at, utcnow().isoformat(),
                                           'failed', error='provider timeout')
        with patch.object(initial_assessments, 'queue') as queue:
            response = await main.retry_initial_assessment(site['id'])
        self.assertEqual(response['status'], 'pending')
        queue.assert_called_once_with(site['id'])

    async def test_concurrent_claim_only_runs_once(self):
        site = database.insert_site('Concurrent job', 30, -81)
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def assess(_site):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {'sources': {}, 'metar': {'availability': 'available'},
                    'weather_model': {'availability': 'available'}}

        with patch.object(risk, 'get_site_risk', side_effect=assess):
            first = asyncio.create_task(initial_assessments.run(site['id']))
            await started.wait()
            self.assertFalse(await initial_assessments.run(site['id']))
            release.set()
            await first
        self.assertEqual(calls, 1)

    async def test_manual_refresh_waiting_for_initial_uses_saved_partial_result(self):
        site = database.insert_site('Shared refresh job', 30, -81)
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def locked(_site, _sources):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            result = {'site': site, 'generated_at': utcnow().isoformat(),
                      'sources': {'weather': {'status': 'unavailable'}}}
            database.save_risk(site['id'], result)
            return result

        with patch.object(risk, 'refresh_site_locked', side_effect=locked):
            first = asyncio.create_task(risk.refresh_site(site, None))
            await started.wait()
            manual = asyncio.create_task(risk.refresh_site(site, None))
            await asyncio.sleep(.02)
            release.set()
            await first
            second = await manual
        self.assertEqual(calls, 1)
        self.assertTrue(second['cached'])

    async def test_initial_worker_reuses_a_just_completed_scheduled_assessment(self):
        site = database.insert_site('Scheduled first', 30, -81)
        snapshot = {'site': site, 'generated_at': utcnow().isoformat(),
                    'method_version': risk.METHOD_VERSION,
                    'assessment_revision': risk.ASSESSMENT_REVISION,
                    'sources': {'weather': {'status': 'unavailable', 'error': 'timeout'}},
                    'metar': {'availability': 'unavailable', 'reason': 'missing'},
                    'weather_model': metar_model.unavailable('metar_missing')}
        database.save_risk(site['id'], snapshot)
        with patch.object(risk, 'get_site_risk', new=AsyncMock()) as refresh:
            self.assertTrue(await initial_assessments.run(site['id']))
        refresh.assert_not_awaited()
        self.assertEqual(database.initial_assessment(site['id'])['status'], 'completed')

    async def test_expired_weather_model_stays_separate_from_rule_rank(self):
        site = database.insert_site('Prediction job', 30, -81)
        now = utcnow()
        snapshot = {'site': site, 'generated_at': now.isoformat(),
                    'method_version': risk.METHOD_VERSION, 'assessment_revision': risk.ASSESSMENT_REVISION,
                    'current_risk': {'overall': {'level': 'high', 'highest_known_level': 'high',
                                                 'coverage_complete': True}},
                    'weather_model': {'availability': 'available', 'target': metar_model.TARGET,
                                      'probability': .8, 'score': 8, 'prediction_time':
                                          (now - timedelta(minutes=20)).isoformat(),
                                      'valid_until': (now - timedelta(minutes=5)).isoformat()}}
        database.save_risk(site['id'], snapshot)
        row = site_responses.overview([site])['sites'][0]
        self.assertEqual(row['weather_model']['availability'], 'stale')
        self.assertIsNone(row['weather_model']['probability'])
        self.assertIsNone(row['current_score'])
        self.assertEqual(row['ranking_basis'], 'rule_based')
        self.assertEqual(row['rule_based_severity'], 'high')
