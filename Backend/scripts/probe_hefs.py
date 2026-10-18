"""Read official HEFS schema and public examples; no production site mutations."""
import json
import argparse
from pathlib import Path
import sys
from datetime import datetime, timezone, timedelta
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hefs

cache = Path(__file__).resolve().parents[1] / '.cache'
cache.mkdir(exist_ok=True)
parser = argparse.ArgumentParser()
parser.add_argument('--location', default='BIFM5')
parser.add_argument('--parameter', default='SSTG')
args = parser.parse_args()
captured_at = datetime.now(timezone.utc)
products = {}
with httpx.Client(timeout=20, follow_redirects=True) as client:
    response = client.get('https://api.water.noaa.gov/hefs/v1/schema/', headers={'Accept': 'application/json'})
    response.raise_for_status()
    (cache / 'hefs-schema.txt').write_text(response.text, encoding='utf-8')
    print('Schema HTTP', response.status_code, response.headers.get('content-type'), response.text[:160])
    try:
        schema = response.json()
    except ValueError:
        print('Schema saved as text for inspection')
        schema = {'paths': {}}
    (cache / 'hefs-schema.json').write_text(json.dumps(schema), encoding='utf-8')
    for path, methods in schema['paths'].items():
        operation = methods.get('get', {})
        print(path, json.dumps({'summary': operation.get('summary'), 'parameters': operation.get('parameters'),
                               'response': operation.get('responses', {}).get('200')})[:10000])
    for endpoint in ('locations', 'map-quantiles', 'hydrograph-quantiles'):
        params = {'location_id': args.location}
        if endpoint != 'locations':
            params['parameter_id'] = args.parameter
        response = client.get(f'https://api.water.noaa.gov/hefs/v1/{endpoint}/', params=params)
        print(endpoint, response.status_code)
        if response.status_code != 200:
            continue
        payload = response.json()
        products[endpoint] = payload
        (cache / f'hefs-{args.location}-{args.parameter}-{endpoint}.json').write_text(json.dumps(payload), encoding='utf-8')
        print(json.dumps({**payload, 'value_set': payload.get('value_set',[])[:2]} if isinstance(payload,dict) else payload)[:9000])
    response = client.get('https://raw.githubusercontent.com/NOAA-OWP/data-service-notebooks/master/HEFS/4_map_quantiles_demo.ipynb')
    response.raise_for_status()
    notebook = response.json()
    (cache/'hefs-map-notebook.json').write_text(json.dumps(notebook),encoding='utf-8')
    for cell in notebook['cells']:
        source = ''.join(cell.get('source',[]))
        if cell.get('cell_type')=='markdown' and any(term in source.lower() for term in ('maximum','exceedance','day')):
            print(source[:4000])

association = {'location_id': args.location, 'parameter_id': args.parameter}
normalized = hefs.unknown(None, association)
thresholds = (products.get('map-quantiles', {}).get('metadata', {}).get('thresholds'))
for product, field in (('map-quantiles', 'period_maxima'), ('hydrograph-quantiles', 'individual_times')):
    if product not in products:
        continue
    normalized[field] = hefs.normalize_product(products[product], product, association, thresholds)
    normalized['sources'][product] = {'status': 'available', 'http_status': 200,
        'retrieved_at': captured_at.isoformat(), 'expires_at': (captured_at + timedelta(seconds=900)).isoformat()}
normalized = hefs.serialize_forecast(normalized, now=captured_at)
usable = [row for field in ('period_maxima', 'individual_times') for row in normalized[field]
          if row['availability'] == 'available' and row['probability_bounds'] is not None]
normalized['reason'] = None if usable else 'no_usable_threshold_probabilities'
(cache / f'hefs-{args.location}-{args.parameter}-normalized.json').write_text(json.dumps(normalized, indent=2), encoding='utf-8')
print('Normalized availability:', normalized['availability'], 'usable bounds:', len(usable))
if usable:
    print('First usable river-threshold bound:', json.dumps(usable[0]))
