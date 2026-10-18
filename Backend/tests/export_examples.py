"""Generate illustrative response files from isolated synthetic test fixtures."""
from pathlib import Path
import json
import sys
import tempfile
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_backend_flows import NOW, feeds
import database
import alerts
import risk
import ml_risk
import site_responses
import hefs
import rivers

destination = Path(__file__).resolve().parents[1] / 'docs' / 'examples'
destination.mkdir(parents=True, exist_ok=True)
for scenario in ('normal', 'dangerous', 'stale', 'unavailable'):
    with tempfile.TemporaryDirectory() as temporary, \
         patch.object(database, 'DATABASE_PATH', Path(temporary) / 'test.db'), \
         patch('risk.utcnow', return_value=NOW), \
         patch('freshness.utcnow', return_value=NOW), \
         patch('ml_risk.utcnow', return_value=NOW), \
         patch('hefs.utcnow', return_value=NOW):
        database.initialize_database()
        site = database.find_site(1)
        if scenario != 'unavailable':
            snapshot = risk.assess(site, feeds(115 if scenario == 'dangerous' else 80))
            snapshot['ml_assessment'] = ml_risk.unavailable('trained_model_not_configured')
            snapshot['ml_configuration_id'] = ml_risk.configuration_id()
            if scenario == 'stale':
                snapshot['generated_at'] = (NOW - timedelta(hours=2)).isoformat()
            database.save_risk(1, snapshot)
            alerts.evaluate(snapshot)
        result = {'example_only': True, 'scenario': scenario,
                  'description': 'Synthetic test fixture; never inserted into the application database.',
                  'map_response': site_responses.overview([site]), 'detail_response': site_responses.detail(site)}
        result['excerpt_notes'] = 'Legacy duplicate fields omitted; hourly arrays show the first two entries for readability.'
        detail = result['detail_response']
        for key in ('current_risk', 'hourly_predicted_risk', 'highest_upcoming_risk', 'overall',
                    'indicators', 'weather', 'ml_assessment', 'sources'):
            detail.pop(key, None)
        detail['rule_based_assessment']['hourly'] = detail['rule_based_assessment']['hourly'][:2]
        if detail.get('conditions'):
            for key in ('hourly', 'recent_hourly'):
                detail['conditions'][key] = {k: v[:2] if isinstance(v, list) else v
                                             for k, v in detail['conditions'].get(key, {}).items()}
        (destination / (scenario + '.json')).write_text(json.dumps(result, indent=2), encoding='utf-8')

# Recorded provider examples are kept distinct from synthetic site/model fixtures.
fixture_dir = Path(__file__).parent / 'fixtures' / 'hefs'
clock = datetime(2026, 10, 17, 21, tzinfo=timezone.utc)
for location, parameter, scenario in (('BIFM5', 'SSTG', 'hefs-bifm5-recorded'),
                                       ('BLDA1', 'QINE', 'hefs-blda1-unavailable')):
    association = {'location_id': location, 'parameter_id': parameter,
                   'exposure_confirmed': False, 'evidence': 'Recorded provider example; no jobsite association claimed'}
    forecast = hefs.unknown(None, association)
    map_data = json.loads((fixture_dir / f'{location}-{parameter}-map-quantiles.json').read_text())
    thresholds = map_data['metadata']['thresholds']
    for product, field in (('map-quantiles', 'period_maxima'), ('hydrograph-quantiles', 'individual_times')):
        data = json.loads((fixture_dir / f'{location}-{parameter}-{product}.json').read_text())
        forecast[field] = hefs.normalize_product(data, product, association, thresholds)
        forecast['sources'][product] = {'status': 'available', 'http_status': 200,
            'retrieved_at': '2026-10-17T20:48:42.205978+00:00' if location == 'BIFM5' else None,
            'expires_at': '2026-10-17T21:03:42.205978+00:00' if location == 'BIFM5' else None}
    forecast['reason'] = None if any(row['probability_bounds'] for row in forecast['period_maxima']) else 'no_usable_threshold_probabilities'
    for label, at in [(scenario, clock)] + ([('hefs-stale', clock + timedelta(hours=1))] if location == 'BIFM5' else []):
        result = hefs.serialize_forecast(forecast, now=at)
        # Short excerpts preserve original provider targets; no hourly resampling.
        result['individual_times'] = [row for row in result['individual_times']
                                      if datetime.fromisoformat(row['target_time']) >= clock][:4]
        example = {'example_only': True, 'scenario': label, 'serialized_at': at.isoformat(),
            'provenance': 'Recorded public NOAA response; no confirmed jobsite exposure; historical example, not a live forecast',
            'timestamp_note': 'BIFM5 time is probe capture time with application TTL; exact BLDA1 retrieval time was not recorded and remains null. Issuance, thresholds and targets are recorded NOAA values.',
            'excerpt_notes': 'First four future threshold rows shown; all recorded map periods retained.',
            'response': result}
        (destination / (label + '.json')).write_text(json.dumps(example, indent=2), encoding='utf-8')

target = NOW - timedelta(minutes=6)
snapshot = {'generated_at': NOW.isoformat(), 'ml_assessment': {
    **ml_risk.unavailable('TEST_OUTPUT_ONLY'), 'availability': 'partial',
    'model_version': 'TEST_ONLY_NOT_A_TRAINED_MODEL', 'current_score': 7.0,
    'current': {'time': target.isoformat(), 'score': 7.0, 'construction_hazards': ['TEST heat']}}}
example = {'example_only': True, 'scenario': 'expired-model', 'serialized_at': NOW.isoformat(),
           'provenance': 'Synthetic test-only model output; never submitted to application sites.',
           'response': ml_risk.serialize_predictions(snapshot, now=NOW)}
(destination / 'expired-model.json').write_text(json.dumps(example, indent=2), encoding='utf-8')

with tempfile.TemporaryDirectory() as temporary, \
     patch.object(database, 'DATABASE_PATH', Path(temporary) / 'example.db'), \
     patch('rivers.utcnow', return_value=clock):
    database.initialize_database()
    plant = next(site for site in database.list_sites() if site.get('eia_plant_id') == 2)
    example = {'example_only': True, 'scenario': 'documented-bankhead-connection',
        'provenance': 'Actual EIA plant and cited operator/USACE/NOAA evidence; generated in a temporary database. No weather, flood probabilities or live strikes are simulated.',
        'site': plant, 'river_exposure': rivers.exposure_for(plant['id']),
        'hefs_limitation': 'BLDA1 direct flow thresholds missing; this documents river connection only, not an available HEFS probability.'}
    (destination / 'bankhead-river-connection.json').write_text(json.dumps(example, indent=2), encoding='utf-8')
print('Wrote nine labeled examples, including documented Bankhead river connection.')
