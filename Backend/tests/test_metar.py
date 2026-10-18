"""METAR station, alignment, and isolated inference contract tests."""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import database
import metar
import metar_model
import risk
import site_responses

NOW = datetime(2026, 10, 18, 0, tzinfo=timezone.utc)
SITE = {"id": 77, "latitude": 25.75, "longitude": -80.70}


def feature(sid, lat, lon, online=True):
    return {"properties": {"sid": sid, "online": online, "country": "US", "network": "FL_ASOS"},
            "geometry": {"coordinates": [lon, lat]}}


class MetarTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enterContext(patch.object(database, 'DATABASE_PATH', Path(temp.name) / 'sites.db'))
        database.initialize_database()

    def test_station_nearest_online_and_persisted(self):
        station = metar.choose_station(SITE, [feature('OFF', 25.75, -80.70, False),
                                               feature('FAR', 27, -80), feature('MIA', 25.8, -80.3)], NOW)
        self.assertEqual(station['station_id'], 'MIA')
        database.save_metar_station(SITE, station)
        self.assertEqual(database.metar_station(SITE)['station_id'], 'MIA')
        self.assertIsNone(database.metar_station({**SITE, 'latitude': 26}))

    def test_units_trace_missing_and_three_hour_alignment(self):
        raw = ('station,valid,tmpf,dwpf,relh,drct,sknt,gust,p01i,alti,mslp,vsby\n'
               'MIA,2026-10-17 20:00,80,70,72,180,10,M,T,29.92,1013,10\n'
               'MIA,2026-10-17 23:00,82,71,70,200,12,20,0.04,29.98,1015,9\n')
        rows = metar.parse_observations(raw, 'MIA')
        self.assertEqual(rows[0]['p01i'], 0.0)
        self.assertEqual(rows[0]['precip_trace_reported'], 1)
        self.assertIsNone(rows[0]['gust'])
        self.assertEqual(rows[1]['alti'], 29.98)
        station = {'distance_km': 12.5}
        result = metar.features_for(station, rows, NOW)
        f = result['features']
        self.assertEqual(list(f), list(metar.FEATURE_ORDER))
        self.assertEqual(f['tmpf'], 82)
        self.assertEqual(f['tmpf_change_3h'], 2)
        self.assertEqual(f['mslp_change_3h'], 2)
        self.assertEqual(f['sknt_change_3h'], 2)
        self.assertEqual(f['observation_age_minutes'], 60)
        self.assertEqual(f['hour_sin'], 0)
        self.assertAlmostEqual(f['hour_cos'], 1)
        self.assertAlmostEqual(f['month_sin'], -1)
        self.assertAlmostEqual(f['month_cos'], 0, places=12)
        january = metar.features_for(station, [], datetime(2025, 1, 1, tzinfo=timezone.utc))['features']
        self.assertEqual(january['month_sin'], 0)
        self.assertEqual(january['month_cos'], 1)
        self.assertIsNone(metar.aligned(rows, NOW - timedelta(hours=10)))

    def test_reporting_delay_and_maximum_observation_age_boundaries(self):
        def report(minutes_old):
            return {'observed_at': (NOW - timedelta(minutes=minutes_old)).isoformat()}
        self.assertIsNone(metar.aligned([report(59)], NOW))
        self.assertEqual(metar.aligned([report(60)], NOW), report(60))
        self.assertEqual(metar.aligned([report(180)], NOW), report(180))
        self.assertIsNone(metar.aligned([report(181)], NOW))

    async def test_old_reports_are_stale_not_current(self):
        station = {"station_id": "OLD", "station_latitude": 25.8,
                   "station_longitude": -80.3, "distance_km": 12.5,
                   "provenance": "fixture", "selected_at": NOW.isoformat()}
        database.save_metar_station(SITE, station)
        csv_data = ('station,valid,tmpf,dwpf,relh,drct,sknt,gust,p01i,alti,mslp,vsby\n'
                    'OLD,2026-10-17 12:00,80,70,72,180,10,M,M,29.92,1013,10\n')
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=csv_data))) as client:
            result = await metar.assess(SITE, client, NOW)
        self.assertEqual(result['availability'], 'stale')
        self.assertEqual(result['reason'], 'no_aligned_observation_within_180_minute_window')
        self.assertIsNone(result['features']['tmpf'])

    async def test_station_cache_shared_and_provider_failure(self):
        catalog = {'features': [feature('MIA', 25.8, -80.3)]}
        calls = []
        def handle(request):
            calls.append(str(request.url))
            if 'network.py' in str(request.url):
                return httpx.Response(200, json=catalog)
            return httpx.Response(200, text='station,valid,tmpf,dwpf,relh,drct,sknt,gust,p01i,alti,mslp,vsby\n'
                                            'MIA,2026-10-17 23:00,82,71,70,200,12,20,0.04,29.98,1015,9\n')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            first, second = await asyncio.gather(metar.assess(SITE, client, NOW),
                                                 metar.assess({**SITE, 'id': 78}, client, NOW))
        self.assertEqual(first['availability'], 'available')
        self.assertEqual(second['availability'], 'available')
        self.assertEqual(len([url for url in calls if 'asos.py' in url]), 1)
        failed_site = {**SITE, 'id': 79}
        database.save_metar_station(failed_site, {**first['station'], 'station_id': 'FAIL'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))) as client:
            failed = await metar.assess(failed_site, client, NOW)
        self.assertEqual(failed['availability'], 'unavailable')
        self.assertEqual(failed['reason'], 'provider_http_404')
        self.assertEqual(failed['station']['station_id'], 'FAIL')

    def test_output_match_version_and_expiry(self):
        self.assertEqual(metar_model.serialize(None)['availability'], 'pending')
        station = {'station_id': 'MIA'}
        m = {'station': station, 'availability': 'available', 'feature_order': list(metar.FEATURE_ORDER),
             'features': {key: None for key in metar.FEATURE_ORDER},
             'current_observed_at': NOW.isoformat(), 'prior_observed_at': None,
             'availability_assumption': 'test'}
        request = metar_model.request_for({'generated_at': NOW.isoformat(), 'site': SITE}, m)
        with patch.dict(os.environ, {'PLANIT_METAR_ML_MODEL_VERSION': 'v1'}):
            output = metar_model.parse_output({'request_id': request['request_id'],
                                               'model_version': 'v1', 'probability': .42}, request)
            self.assertEqual(output['score'], 4.2)
            self.assertEqual(metar_model.serialize(output, NOW + timedelta(minutes=5))['availability'], 'stale')
            with self.assertRaises(ValueError):
                metar_model.parse_output({'request_id': 'wrong', 'model_version': 'v1',
                                          'probability': .42}, request)

    def test_frozen_model_matches_supplied_historical_prediction_path(self):
        import joblib
        import math
        import pandas as pd
        table = metar_model.MODEL_ROOT / 'dev300_hourly_training_2025.csv.gz'
        artifact = metar_model.artifact_path('geographic')
        sample = pd.read_csv(table, nrows=4, low_memory=False)
        bundle = joblib.load(artifact)
        self.assertEqual(list(bundle['features']), list(metar.FEATURE_ORDER))
        x = sample.loc[:, bundle['features']].apply(pd.to_numeric, errors='coerce').astype('float32')
        expected = bundle['model'].predict_proba(x)[:, 1]
        for index, row in x.iterrows():
            values = {key: (None if math.isnan(float(value)) else float(value))
                      for key, value in row.items()}
            request = {'request_id': f'historical-{index}',
                       'feature_order': list(metar.FEATURE_ORDER), 'features': values}
            result = metar_model.local_prediction(request)
            self.assertAlmostEqual(result['probability'], float(expected[index]), places=12)
            self.assertEqual(result['request_id'], request['request_id'])
            self.assertIn('geographic', result['model_version'])

        # The supplied first January training row establishes the month phase.
        self.assertEqual(float(sample.iloc[0]['month_sin']), 0)
        self.assertEqual(float(sample.iloc[0]['month_cos']), 1)

    async def test_local_inference_uses_current_metar_features(self):
        raw = ('station,valid,tmpf,dwpf,relh,drct,sknt,gust,p01i,alti,mslp,vsby\n'
               'MIA,2026-10-17 20:00,80,70,72,180,10,M,T,29.92,1013,10\n'
               'MIA,2026-10-17 23:00,82,71,70,200,12,20,0.04,29.98,1015,9\n')
        station = {'station_id': 'MIA', 'distance_km': 12.5}
        metar_input = {'availability': 'available', 'station': station,
                       **metar.features_for(station, metar.parse_observations(raw, 'MIA'), NOW)}
        assessment = {'site': SITE, 'generated_at': NOW.isoformat()}
        with patch.dict(os.environ, {'PLANIT_METAR_ML_URL': '', 'PLANIT_METAR_ML_MODEL': 'geographic',
                                     'PLANIT_METAR_ML_MODEL_VERSION': ''}):
            result = await metar_model.predict(assessment, metar_input)
        self.assertEqual(result['availability'], 'available')
        self.assertGreaterEqual(result['probability'], 0)
        self.assertLessEqual(result['probability'], 1)
        self.assertAlmostEqual(result['score'], result['probability'] * 10)
        self.assertEqual(result['current_observed_at'], '2026-10-17T23:00:00+00:00')
        self.assertEqual(result['outcome_end'], (NOW + timedelta(hours=6)).isoformat())
        self.assertEqual(result['input_availability'], 'available')

        stale_input = {**metar_input, 'availability': 'stale',
                       'reason': 'no_aligned_observation_within_180_minute_window',
                       **metar.features_for(station, [], NOW)}
        with patch.dict(os.environ, {'PLANIT_METAR_ML_URL': '', 'PLANIT_METAR_ML_MODEL': 'geographic',
                                     'PLANIT_METAR_ML_MODEL_VERSION': ''}):
            missing_result = await metar_model.predict(assessment, stale_input)
        self.assertEqual(missing_result['availability'], 'available')
        self.assertEqual(missing_result['input_availability'], 'stale')
        with patch.dict(os.environ, {'PLANIT_METAR_ML_URL': '', 'PLANIT_METAR_ML_MODEL': 'geographic',
                                     'PLANIT_METAR_ML_MODEL_VERSION': ''}), \
             patch('metar_model.local_prediction', side_effect=RuntimeError('fixture failure')):
            failed = await metar_model.predict(assessment, metar_input)
        self.assertEqual(failed['availability'], 'unavailable')
        self.assertEqual(failed['reason'], 'frozen_model_invalid_or_inference_failed')

    async def test_full_refresh_persists_separate_weather_model_result(self):
        site = database.insert_site('MODEL TEST', SITE['latitude'], SITE['longitude'])
        station = {'station_id': 'MIA', 'distance_km': 12.5}
        rows = metar.parse_observations(
            'station,valid,tmpf,dwpf,relh,drct,sknt,gust,p01i,alti,mslp,vsby\n'
            'MIA,2026-10-17 23:00,82,71,70,200,12,20,T,29.98,1015,9\n', 'MIA')
        metar_input = {'availability': 'available', 'station': station,
                       **metar.features_for(station, rows, NOW)}
        weather = {'current': {'time': NOW.isoformat(), 'weather_code': 0,
                               'apparent_temperature': 80, 'wind_gusts_10m': 5,
                               'soil_moisture_3_to_9cm': 0.2},
                   'utc_offset_seconds': 0, 'recent_hourly': {}, 'hourly': {}}
        feeds = {name: {'status': 'available', 'data': data} for name, data in (
            ('weather', weather), ('alerts', {'alerts': []}),
            ('wildfires', {'events': []}), ('wildlife', {'total_results': 0}))}
        sources = SimpleNamespace(for_site=AsyncMock(return_value=feeds), client=None)
        with patch.dict(os.environ, {'PLANIT_METAR_ENABLED': '1', 'PLANIT_METAR_ML_URL': '',
                                     'PLANIT_METAR_ML_MODEL': 'geographic',
                                     'PLANIT_METAR_ML_MODEL_VERSION': ''}), \
             patch('risk.utcnow', return_value=NOW), \
             patch('metar.assess', new=AsyncMock(return_value=metar_input)), \
             patch('rivers.augment_assessment', new=AsyncMock(
                 side_effect=lambda site, assessment, client: assessment)):
            saved = await risk.refresh_site_locked(site, sources)
        self.assertEqual(saved['weather_model']['availability'], 'available')
        self.assertEqual(saved['weather_model']['input_availability'], 'available')
        self.assertIsNone(saved['ml_assessment']['current_score'])
        self.assertEqual(database.load_risk(site['id'])['weather_model']['probability'],
                         saved['weather_model']['probability'])
        with patch('metar_model.utcnow', return_value=NOW):
            response = site_responses.detail(site)
        self.assertEqual(response['weather_model']['probability'], saved['weather_model']['probability'])
        self.assertEqual(response['weather_model']['target'], metar_model.TARGET)


if __name__ == '__main__':
    unittest.main()
