"""NOAA HEFS river-threshold forecasts, separate from jobsite rules and ML.

See HEFS_NOTES.md for verified endpoints and conservative quantile inversion.
No proximity-based exposure, hourly interpolation, or site flood probability.
"""
from copy import deepcopy
from datetime import timedelta
import json
import math

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator
import database
import freshness
import rivers
from refresh_locks import lease, RefreshBusy
from risk_sources import http_client, utcnow

BASE_URL = 'https://api.water.noaa.gov/hefs/v1'
router = APIRouter(prefix='/api', tags=['HEFS river forecasts'])
LIMITATION = 'River-threshold exceedance at the forecast location, not the probability that the jobsite floods'


def initialize():
    with database.connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS site_hefs (
            site_id INTEGER PRIMARY KEY, association TEXT NOT NULL, payload TEXT)''')


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def probability_bounds(exceedances, values, threshold):
    """Conservative inverse of decreasing exceedance quantiles, without interpolation.

    Q(e)>T implies P(X>T)>=e; Q(e)<T implies P(X>T)<=e. Equal/rounded
    values supply no bound. Discrete ties never become an exact probability.
    """
    if isinstance(values, str):
        values = json.loads(values)
    if (not isinstance(exceedances, list) or not isinstance(values, list)
            or not exceedances or len(exceedances) != len(values)
            or not finite(threshold) or any(not finite(v) for v in values)
            or any(not finite(e) or not 0 < e < 1 for e in exceedances)
            or len(set(exceedances)) != len(exceedances)):
        raise ValueError('Invalid quantiles')
    pairs = sorted(zip(exceedances, values))
    if any(a[1] < b[1] for a, b in zip(pairs, pairs[1:])):
        raise ValueError('Exceedance quantiles must be non-increasing')
    lower = max([e for e, q in pairs if q > threshold] or [0.0])
    upper = min([e for e, q in pairs if q < threshold] or [1.0])
    return {'lower': lower, 'upper': upper, 'lower_inclusive': True, 'upper_inclusive': True,
            'scale': '0-1', 'method': 'conservative_exceedance_quantile_bounds_v1'}


def validate_locations(data):
    if not isinstance(data, list) or any(not isinstance(r, dict) or not isinstance(r.get('parameters'), list)
                                       or not isinstance(r.get('location_id'), str)
                                       or any(not isinstance(p, dict) or not isinstance(p.get('parameter_id'), str)
                                              for p in r.get('parameters', [])) for r in data):
        raise ValueError('Invalid HEFS location list')


def validate_forecast(data):
    if (not isinstance(data, dict) or not isinstance(data.get('metadata'), dict)
            or not isinstance(data.get('value_set'), list)):
        raise ValueError('Invalid HEFS forecast envelope')
    if not data['value_set']:
        raise rivers.MissingData('No forecast values')


def association_for(site_id):
    with database.connect() as conn:
        row = conn.execute('SELECT association FROM site_hefs WHERE site_id=?', (site_id,)).fetchone()
    return json.loads(row['association']) if row else None


def store_association(site_id, association):
    with database.connect() as conn:
        # Changing the mapping invalidates the previous location's forecasts.
        conn.execute('INSERT OR REPLACE INTO site_hefs VALUES (?,?,NULL)', (site_id, json.dumps(association)))


def associated_sites():
    with database.connect() as conn:
        return [dict(row) for row in conn.execute('SELECT sites.* FROM sites JOIN site_hefs ON sites.id=site_hefs.site_id')]


def unknown(site_id, association=None, reason='forecast_location_not_associated'):
    return {'site_id': site_id, 'source': 'NOAA HEFS (experimental)', 'association': association,
            'applicability': 'context_only',
            'site_relevance': {'status': 'unconfirmed', 'site_relevance_verified': False,
                               'reason': 'documented_exposure_review_required'},
            'availability': 'unavailable', 'reason': reason, 'probability': None,
            'probability_scale': '0-1', 'interpretation': LIMITATION,
            'individual_times': [], 'period_maxima': [], 'sources': {}, 'stale': False}


def serialize_forecast(payload, now=None):
    now = now or utcnow()
    result = deepcopy(payload)
    result['sources'] = freshness.sources(result.get('sources'), now)
    usable = []
    missing = False
    for field in ('individual_times', 'period_maxima'):
        for row in result.get(field, []):
            issued = freshness.timestamp(row.get('issued_at'))
            end = freshness.timestamp(row.get('target_time') or row.get('period_end'))
            feed = result['sources'].get(row.get('product'), {})
            stale = (not issued or not end or not issued <= now < issued + timedelta(hours=48)
                     or end < now or bool(feed.get('stale')) or feed.get('status') != 'available')
            row['stale'] = stale
            row['freshness'] = {'issued_at': row.get('issued_at'), 'retrieved_at': feed.get('retrieved_at'),
                                'expires_at': feed.get('expires_at'), 'stale': stale}
            if row.get('probability_bounds') is not None:
                row['availability'] = 'stale' if stale else 'available'
                usable.append(not stale)
            else:
                missing = True
    result['stale'] = bool(usable and not any(usable))
    result['availability'] = ('available' if all(usable) and not missing else 'partial') if any(usable) else 'stale' if usable else 'unavailable'
    result['serialized_at'] = now.isoformat()
    return result


def saved(site_id):
    with database.connect() as conn:
        row = conn.execute('SELECT * FROM site_hefs WHERE site_id=?', (site_id,)).fetchone()
    if not row:
        return unknown(site_id)
    association = json.loads(row['association'])
    payload = json.loads(row['payload']) if row['payload'] else unknown(site_id, association, 'not_yet_refreshed')
    payload['site_relevance'] = rivers.exposure_for(site_id, association.get('gauge_id'))
    payload['applicability'] = ('verified_river_context' if payload['site_relevance']['site_relevance_verified'] else 'context_only')
    return serialize_forecast(payload)


def normalize_product(data, product, association, thresholds=None):
    validate_forecast(data)
    meta = data['metadata']
    parameter, units = meta.get('parameter_id'), meta.get('units')
    if meta.get('location_id') != association['location_id'] or parameter != association['parameter_id']:
        raise ValueError('Forecast location or parameter mismatch')
    supported = (parameter, units) in (('SSTG', 'FT'), ('QINE', 'CFS'))
    issued, start, end = [freshness.timestamp(meta.get(key)) for key in
                          ('forecast_datetime', 'start_datetime', 'end_datetime')]
    if not all((issued, start, end)) or end <= start:
        raise ValueError('Invalid forecast period')
    thresholds = thresholds if thresholds is not None else meta.get('thresholds', meta.get('threshold'))
    thresholds = thresholds if isinstance(thresholds, dict) else {}
    rows = []
    for value_set in data['value_set']:
        if not isinstance(value_set, dict):
            raise ValueError('Invalid quantile row')
        if product == 'map-quantiles':
            length = value_set.get('forecast_length')
            if meta.get('forecast_length_units') != 'days' or not finite(length) or not 0 < length <= 366:
                raise ValueError('Unsupported period units or length')
            target_end = start + timedelta(days=length)
            if target_end > end:
                raise ValueError('Period exceeds forecast support')
            target = {'time_semantics': 'maximum_over_period', 'period_start': start.isoformat(),
                      'period_end': target_end.isoformat(), 'forecast_length_days': length}
        else:
            instant = freshness.timestamp(value_set.get('valid_datetime'))
            if not instant or not start <= instant <= end:
                raise ValueError('Invalid forecast target')
            target = {'time_semantics': 'individual_forecast_time', 'target_time': instant.isoformat()}
        for category, raw_threshold in (thresholds or {'unknown': {}}).items():
            if not isinstance(raw_threshold, dict):
                raise ValueError('Invalid threshold')
            threshold_field = 'stage' if parameter == 'SSTG' else 'flow'
            # calculated_flow is retained, but its rating-curve/datum conversion
            # is not documented in the schema. Do not infer a usable threshold.
            threshold = raw_threshold.get(threshold_field) if supported else None
            if threshold in (-999, -9999):
                threshold = None
            row = {**target, 'product': product, 'source': BASE_URL + '/' + product + '/',
                   'location_id': association['location_id'], 'parameter_id': parameter,
                   'issued_at': issued.isoformat(), 'units': units, 'flood_category': category,
                   'threshold': {'value': threshold if finite(threshold) else None, 'units': units,
                                 'source_field': threshold_field, 'raw': raw_threshold},
                   'probability': None, 'probability_bounds': None, 'availability': 'unavailable',
                   'reason': 'threshold_missing_or_units_unsupported',
                   'exceedance_quantiles': meta.get('exceedance_quantiles'),
                   'quantile_values': value_set.get('quantile_values'),
                   'interpretation': LIMITATION}
            if finite(threshold):
                row['probability_bounds'] = probability_bounds(meta.get('exceedance_quantiles'), value_set.get('quantile_values'), threshold)
                row.update(availability='available', reason=None)
            rows.append(row)
    return rows


async def refresh_site(site, client):
    async with lease('hefs:' + str(site['id'])):
        association = association_for(site['id'])
        result = unknown(site['id'], association)
        result['site_relevance'] = rivers.exposure_for(site['id'], (association or {}).get('gauge_id'))
        result['applicability'] = ('verified_river_context' if result['site_relevance']['site_relevance_verified'] else 'context_only')
        if not association:
            return result
        with database.connect() as conn:
            row = conn.execute('SELECT payload FROM site_hefs WHERE site_id=?', (site['id'],)).fetchone()
        previous = json.loads(row['payload']) if row and row['payload'] else {}

        def retain_product(product, field):
            if not result[field] and previous.get(field):
                result[field] = deepcopy(previous[field])
                old_source = previous.get('sources', {}).get(product, {})
                attempt = result['sources'].get(product, result['sources'].get('locations', {}))
                result['sources'][product] = {**attempt, 'status': 'unavailable', 'stale': True,
                    'retrieved_at': old_source.get('retrieved_at'), 'expires_at': old_source.get('expires_at')}
        params = {'location_id': association['location_id']}
        locations = await rivers.fetch_json(client, BASE_URL + '/locations/', params, ttl=86400, validator=validate_locations)
        result['sources']['locations'] = {k: v for k, v in locations.items() if k != 'data'}
        locations_data = locations.get('data') or []
        supported = any(row['location_id'] == association['location_id'] and
                        any(p.get('parameter_id') == association['parameter_id'] for p in row['parameters']) for row in locations_data)
        if not supported:
            result['reason'] = 'unsupported_location_or_parameter' if locations['status'] == 'available' else locations.get('error', 'provider_unavailable')
            if locations['status'] != 'available':
                for product, field in (('map-quantiles', 'period_maxima'), ('hydrograph-quantiles', 'individual_times')):
                    retain_product(product, field)
        else:
            params['parameter_id'] = association['parameter_id']
            thresholds = None
            issuance = None
            for product, field in (('map-quantiles', 'period_maxima'), ('hydrograph-quantiles', 'individual_times')):
                # Pin hydrograph to the map issuance so thresholds/forecast do not
                # silently cross cycles during an upstream update.
                query = {**params, **({'forecast_datetime': issuance} if issuance else {})}
                response = await rivers.fetch_json(client, BASE_URL + '/' + product + '/', query,
                                                   ttl=900, validator=validate_forecast)
                result['sources'][product] = {k: v for k, v in response.items() if k != 'data'}
                data = response.get('data')
                if data:
                    try:
                        if issuance and freshness.timestamp(data['metadata'].get('forecast_datetime')) != freshness.timestamp(issuance):
                            raise ValueError('Hydrograph issuance does not match threshold issuance')
                        result[field] = normalize_product(data, product, association, thresholds)
                        if product == 'map-quantiles':
                            thresholds = data['metadata'].get('thresholds', data['metadata'].get('threshold'))
                            issuance = data['metadata']['forecast_datetime']
                    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                        result['sources'][product].update(status='unavailable', error='invalid_schema')
                retain_product(product, field)
            result['reason'] = None if any(r.get('probability_bounds') for f in ('period_maxima', 'individual_times') for r in result[f]) else 'no_usable_threshold_probabilities'
        with database.connect() as conn:
            # A changed association must not receive an old in-flight result.
            conn.execute('UPDATE site_hefs SET payload=? WHERE site_id=? AND association=?',
                         (json.dumps(result, allow_nan=False), site['id'], json.dumps(association)))
        return serialize_forecast(result)


class Association(BaseModel):
    location_id: str = Field(pattern=r'^[A-Z0-9]{3,20}$')
    gauge_id: str = Field(pattern=r'^[A-Z0-9]{3,20}$')
    parameter_id: str = Field(default='SSTG', pattern=r'^(SSTG|QINE)$')
    exposure_confirmed: bool = False
    evidence: str = Field(min_length=10, max_length=2000)

    @model_validator(mode='after')
    def same_location(self):
        if self.location_id != self.gauge_id:
            raise ValueError('This adapter requires the NWPS gauge and HEFS location to share an ID')
        return self


def require_site(site_id):
    site = database.find_site(site_id)
    if not site:
        raise HTTPException(404, 'Site not found')
    return site


@router.get('/sites/{site_id}/hefs')
def get_forecast(site_id: int):
    require_site(site_id)
    return saved(site_id)


@router.put('/sites/{site_id}/hefs')
async def associate(site_id: int, payload: Association):
    site = require_site(site_id)
    river = rivers.saved(site_id)
    if not river or river.get('gauge_id') != payload.gauge_id:
        raise HTTPException(409, 'Associate the matching NWPS gauge first')
    if payload.exposure_confirmed and not river.get('exposure_confirmed'):
        raise HTTPException(409, 'Confirm river exposure with site-specific evidence first')
    try:
        async with lease('hefs:' + str(site_id)):
            store_association(site_id, {**payload.model_dump(), 'associated_at': utcnow().isoformat()})
        async with http_client() as client:
            return await refresh_site(site, client)
    except RefreshBusy as error:
        raise HTTPException(409, str(error)) from error


@router.post('/sites/{site_id}/hefs/refresh')
async def refresh(site_id: int):
    site = require_site(site_id)
    try:
        async with http_client() as client:
            return await refresh_site(site, client)
    except RefreshBusy as error:
        raise HTTPException(409, str(error)) from error
