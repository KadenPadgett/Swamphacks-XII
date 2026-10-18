"""HEFS regression tests: recorded NOAA responses and isolated synthetic failures.

No network calls, production database writes, or fabricated production forecasts.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
import database
import hefs
import ml_risk
import refresh_jobs
import rivers
from main import app

FIXTURES = Path(__file__).parent / 'fixtures' / 'hefs'
NOW = datetime(2026, 10, 17, 21, tzinfo=timezone.utc)


def recorded(name):
    return json.loads((FIXTURES / name).read_text(encoding='utf-8'))


class HefsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.enterContext(patch.object(database, 'DATABASE_PATH', Path(folder.name) / 'test.db'))
        self.enterContext(patch.dict(os.environ, {'PLANIT_BACKGROUND_REFRESH': '0', 'PLANIT_WRITE_TOKEN': '',
                                                'PLANIT_ML_URL': '', 'PLANIT_HEFS_SECONDS': '900'}))
        self.enterContext(patch('hefs.utcnow', return_value=NOW))
        self.enterContext(patch('rivers.utcnow', return_value=NOW))
        self.enterContext(patch('freshness.utcnow', return_value=NOW))
        database.initialize_database()
        self.site = database.insert_site('TEST forecast adapter; exposure unconfirmed', 48.1958, -93.8069)
        self.sid = self.site['id']
        self.association = {'location_id': 'BIFM5', 'gauge_id': 'BIFM5', 'parameter_id': 'SSTG',
                            'exposure_confirmed': False, 'evidence': 'TEST association only; no verified jobsite exposure'}
        self.locations = recorded('BIFM5-SSTG-locations.json')
        self.map = recorded('BIFM5-SSTG-map-quantiles.json')
        self.hydro = recorded('BIFM5-SSTG-hydrograph-quantiles.json')
        self.requests = []

    def response(self, request):
        self.requests.append(request)
        product = request.url.path.rstrip('/').rsplit('/', 1)[-1]
        return httpx.Response(200, json={'locations': self.locations, 'map-quantiles': self.map,
                                         'hydrograph-quantiles': self.hydro}[product])

    async def refresh(self, handler=None):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler or self.response)) as client:
            return await hefs.refresh_site(self.site, client)

    def test_quantile_inverse_preserves_bounds_and_ties_instead_of_point_probabilities(self):
        for threshold, expected in ((15, (.1, .5)), (25, (0, .1)), (5, (.9, 1)), (10, (.1, .9))):
            with self.subTest(threshold=threshold):
                bounds = hefs.probability_bounds([.1, .5, .9], [20, 10, 8], threshold)
                self.assertEqual((bounds['lower'], bounds['upper']), expected)
                self.assertEqual(bounds['scale'], '0-1')
        tied = hefs.probability_bounds([.1, .5, .9], [10, 10, 10], 10)
        self.assertEqual((tied['lower'], tied['upper']), (0, 1))
        self.assertEqual(hefs.probability_bounds([.1, .5], '[20,10]', 15)['upper'], .5)

    def test_invalid_quantiles_are_rejected_without_inventing_missing_values(self):
        for exceedances, values, threshold in (([.1, .5], [10], 5), ([.1, .5], [10, 20], 5),
                ([.1, .1], [10, 10], 5), ([0, 1], [10, 5], 4), ([.1], [None], 4),
                ([.1], [10], float('nan')), ([True], [10], 4)):
            with self.subTest(values=values, threshold=threshold):
                with self.assertRaises(ValueError):
                    hefs.probability_bounds(exceedances, values, threshold)

    def test_recorded_bifm5_stage_forecast_has_usable_bounds_and_distinct_time_semantics(self):
        periods = hefs.normalize_product(self.map, 'map-quantiles', self.association)
        times = hefs.normalize_product(self.hydro, 'hydrograph-quantiles', self.association,
                                       self.map['metadata']['thresholds'])
        minor = next(row for row in periods if row['flood_category'] == 'minor')
        self.assertEqual(minor['threshold']['value'], 16)
        self.assertEqual(minor['threshold']['units'], 'FT')
        self.assertEqual(minor['probability_bounds']['upper'], .1)
        self.assertIsNone(minor['probability'])
        self.assertEqual(minor['issued_at'], '2026-10-17T00:00:00+00:00')
        self.assertEqual(minor['period_start'], minor['issued_at'])
        self.assertEqual(minor['period_end'], '2026-10-22T00:00:00+00:00')
        self.assertEqual(minor['time_semantics'], 'maximum_over_period')
        self.assertNotIn('target_time', minor)
        targets = sorted({row['target_time'] for row in times})
        self.assertEqual(datetime.fromisoformat(targets[1]) - datetime.fromisoformat(targets[0]), timedelta(hours=6))
        self.assertTrue(all(row['probability_bounds']['upper'] == .05 for row in times))
        self.assertTrue(all(row['time_semantics'] == 'individual_forecast_time' for row in times))
        self.assertNotIn('period_start', times[0])
        self.assertIn('not the probability that the jobsite floods', minor['interpretation'])

    def test_recorded_blda1_qine_does_not_convert_stage_or_calculated_flow_to_missing_flow_threshold(self):
        association = {**self.association, 'location_id': 'BLDA1', 'parameter_id': 'QINE'}
        data = recorded('BLDA1-QINE-map-quantiles.json')
        periods = hefs.normalize_product(data, 'map-quantiles', association)
        times = hefs.normalize_product(recorded('BLDA1-QINE-hydrograph-quantiles.json'),
                                       'hydrograph-quantiles', association, data['metadata']['thresholds'])
        self.assertTrue(periods and times)
        self.assertTrue(any(row['threshold']['raw'].get('calculated_flow') for row in periods))
        for row in periods + times:
            self.assertIsNone(row['threshold']['value'])
            self.assertIsNone(row['probability'])
            self.assertIsNone(row['probability_bounds'])
            self.assertEqual(row['availability'], 'unavailable')

    def test_missing_thresholds_units_and_sentinels_remain_explicitly_unavailable(self):
        for changes in ({'thresholds': None}, {'units': 'M'}, {'thresholds': {'minor': {'stage': -9999}}}):
            with self.subTest(changes=changes):
                data = deepcopy(self.map)
                data['metadata'].update(changes)
                rows = hefs.normalize_product(data, 'map-quantiles', self.association)
                self.assertTrue(rows)
                self.assertTrue(all(row['availability'] == 'unavailable' and row['probability'] is None
                                    and row['probability_bounds'] is None for row in rows))

    def test_explicit_valid_flow_threshold_uses_cfs_without_stage_conversion(self):
        # Synthetic adapter edge case, confined to this test.
        data = recorded('BLDA1-QINE-map-quantiles.json')
        data['metadata']['thresholds'] = {'minor': {'flow': 10000, 'stage': 189}}
        rows = hefs.normalize_product(data, 'map-quantiles', {'location_id': 'BLDA1', 'parameter_id': 'QINE'})
        self.assertEqual(rows[0]['threshold']['source_field'], 'flow')
        self.assertEqual(rows[0]['threshold']['units'], 'CFS')
        self.assertIsNotNone(rows[0]['probability_bounds'])
        self.assertIsNone(rows[0]['probability'])

    def test_bad_periods_mismatched_locations_and_targets_are_rejected(self):
        for field, value in (('forecast_length_units', 'hours'), ('location_id', 'OTHER'), ('parameter_id', 'QINE')):
            data = deepcopy(self.map)
            data['metadata'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                hefs.normalize_product(data, 'map-quantiles', self.association)
        data = deepcopy(self.map)
        data['value_set'][0]['forecast_length'] = 100
        with self.assertRaises(ValueError):
            hefs.normalize_product(data, 'map-quantiles', self.association)
        data = deepcopy(self.hydro)
        data['value_set'][0]['valid_datetime'] = '2020-01-01T00:00:00Z'
        with self.assertRaises(ValueError):
            hefs.normalize_product(data, 'hydrograph-quantiles', self.association)

    async def test_refresh_uses_real_contract_pins_issuance_and_keeps_rules_and_ml_separate(self):
        hefs.store_association(self.sid, self.association)
        result = await self.refresh()
        self.assertIn(result['availability'], ('available', 'partial'))
        self.assertTrue(any(row['availability'] == 'available' for row in result['individual_times']))
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.requests[2].url.params['forecast_datetime'], self.map['metadata']['forecast_datetime'])
        self.assertEqual(self.requests[2].url.params['parameter_id'], 'SSTG')
        self.assertFalse(result['association']['exposure_confirmed'])
        self.assertIsNone(database.load_risk(self.sid))
        self.assertIsNone(ml_risk.serialize_predictions(None)['current_score'])
        self.assertEqual(hefs.saved(self.sid)['period_maxima'], result['period_maxima'])

    async def test_failed_provider_after_cache_eviction_retains_original_bounds_and_source_times_as_stale(self):
        hefs.store_association(self.sid, self.association)
        before = await self.refresh()
        with database.connect() as conn:
            conn.execute('DELETE FROM provider_cache')
        def unavailable(request):
            return httpx.Response(503, headers={'Retry-After': '60'})
        with patch('hefs.utcnow', return_value=NOW + timedelta(minutes=20)), \
             patch('rivers.utcnow', return_value=NOW + timedelta(minutes=20)):
            after = await self.refresh(unavailable)
        self.assertEqual(after['availability'], 'stale')
        self.assertEqual(after['period_maxima'][0]['probability_bounds'], before['period_maxima'][0]['probability_bounds'])
        for product in ('map-quantiles', 'hydrograph-quantiles'):
            self.assertEqual(after['sources'][product]['retrieved_at'], before['sources'][product]['retrieved_at'])
            self.assertTrue(after['sources'][product]['stale'])
        self.assertIsNone(after['probability'])

    async def test_unsupported_location_or_parameter_stops_after_locations_and_stays_unknown(self):
        hefs.store_association(self.sid, self.association)
        self.locations[0]['parameters'] = [{'parameter_id': 'QINE'}]
        result = await self.refresh()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result['availability'], 'unavailable')
        self.assertEqual(result['reason'], 'unsupported_location_or_parameter')
        self.assertIsNone(result['probability'])
        self.assertFalse(result['individual_times'])

    async def test_malformed_locations_and_missing_product_values_are_sanitized(self):
        hefs.store_association(self.sid, self.association)
        self.locations[0]['parameters'] = [None]
        malformed = await self.refresh()
        self.assertEqual(malformed['sources']['locations']['error'], 'invalid_schema')
        with database.connect() as conn:
            conn.execute('DELETE FROM provider_cache')
        self.locations = recorded('BIFM5-SSTG-locations.json')
        self.map['value_set'] = []
        self.hydro['value_set'] = []
        missing = await self.refresh()
        self.assertEqual(missing['availability'], 'unavailable')
        self.assertEqual(missing['sources']['map-quantiles']['error'], 'missing_data')
        self.assertIsNone(missing['probability'])

    async def test_hydrograph_cannot_reuse_thresholds_from_a_different_forecast_cycle(self):
        hefs.store_association(self.sid, self.association)
        self.hydro['metadata']['forecast_datetime'] = '2026-10-17T12:00:00Z'
        result = await self.refresh()
        self.assertEqual(result['sources']['hydrograph-quantiles']['error'], 'invalid_schema')
        self.assertFalse(result['individual_times'])
        self.assertTrue(result['period_maxima'])

    async def test_source_expiry_and_target_expiry_are_rechecked_on_reads(self):
        hefs.store_association(self.sid, self.association)
        result = await self.refresh()
        later = hefs.serialize_forecast(result, NOW + timedelta(minutes=16))
        self.assertEqual(later['availability'], 'stale')
        self.assertTrue(later['stale'])
        self.assertEqual(later['period_maxima'][0]['issued_at'], result['period_maxima'][0]['issued_at'])
        self.assertEqual(later['period_maxima'][0]['period_start'], result['period_maxima'][0]['period_start'])
        self.assertEqual(later['period_maxima'][0]['probability_bounds'], result['period_maxima'][0]['probability_bounds'])

    async def test_association_routes_require_matching_gauge_and_do_not_confirm_exposure_by_proximity(self):
        with TestClient(app) as client:
            path = f'/api/sites/{self.sid}/hefs'
            self.assertEqual(client.get(path).json()['reason'], 'forecast_location_not_associated')
            self.assertEqual(client.put(path, json=self.association).status_code, 409)
            rivers.store(self.sid, 'BIFM5', {}, confirmed=False, association='manual')
            self.assertEqual(client.put(path, json={**self.association, 'exposure_confirmed': True}).status_code, 409)
            self.assertEqual(client.put(path, json={**self.association, 'gauge_id': 'OTHER'}).status_code, 422)

    async def test_scheduled_job_only_refreshes_explicitly_associated_sites_without_ml(self):
        hefs.store_association(self.sid, self.association)
        unassociated = database.insert_site('TEST unassociated site', 0, 0)
        from unittest.mock import AsyncMock
        outcome = {'site_id': self.sid, 'availability': 'available', 'sources': {}}
        with patch('hefs.refresh_site', new=AsyncMock(return_value=outcome)) as refresh:
            result = await refresh_jobs.run('hefs', trigger='test')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['site_count'], 1)
        self.assertEqual(refresh.await_args.args[0]['id'], self.sid)
        self.assertIsNone(hefs.association_for(unassociated['id']))


if __name__ == '__main__':
    unittest.main()
