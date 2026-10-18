"""PlanIT METAR inference for arbitrary coordinates within the IEM US ASOS catalog.

Adapted from build_site_station_table.py, audit_historical_station_quality.py,
and build_dev300_v2.py. Research screening only, NOT an official warning.

Requires numpy, pandas, scikit-learn, joblib. See README.md.
"""
from __future__ import annotations

import io
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import joblib
import numpy as np
import pandas as pd

MODEL_FILE = Path(__file__).with_name('metar_2022_2024_final_hgb.joblib')
STATES = ('AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI '
          'MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT '
          'VT VA WA WV WI WY DC PR').split()
CATALOG_URL = 'https://mesonet.agron.iastate.edu/geojson/network.py?network={}'
ASOS_URL = 'https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py'
RAW_FEATURES = ['tmpf', 'dwpf', 'relh', 'drct', 'sknt', 'gust', 'p01i', 'alti', 'mslp', 'vsby']
DELAY_MINUTES = 60
MAX_AGE_MINUTES = 180
MAX_STATION_DISTANCE_KM = 100.0  # Review radius, not a claim of representativeness.


def _request(url: str, timeout: int = 35, retries: int = 3) -> bytes:
    for attempt in range(retries):
        try:
            request = Request(url, headers={'User-Agent': 'PlanIT-METAR-research-inference/1.0'})
            with urlopen(request, timeout=timeout) as response:
                data = response.read(20_000_001)
            if len(data) > 20_000_000:
                raise ValueError('Unexpectedly large weather API response')
            return data
        except (OSError, ValueError):
            if attempt == retries - 1:
                raise
            time.sleep(1 + 2 * attempt)
    raise RuntimeError('Request failed')


def _network_stations(state: str) -> list[dict]:
    network = f'{state}_ASOS'
    payload = json.loads(_request(CATALOG_URL.format(network)).decode('utf-8-sig'))
    stations = []
    for feature in payload.get('features', []):
        prop = feature.get('properties') or {}
        coordinates = (feature.get('geometry') or {}).get('coordinates') or []
        if len(coordinates) < 2:
            continue
        station_id = next((str(prop[k]).strip() for k in ('id', 'sid', 'station', 'station_id')
                           if prop.get(k) is not None and str(prop[k]).strip()), None)
        if not station_id:
            station_id = str(feature.get('id') or '').strip()
        if not station_id:
            continue
        lon, lat = float(coordinates[0]), float(coordinates[1])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        stations.append({'network': network, 'station_id': station_id,
                         'latitude': lat, 'longitude': lon})
    return stations


@lru_cache(maxsize=1)
def station_catalog() -> pd.DataFrame:
    """Retrieve the original US state/territory IEM ASOS networks, once per process.

    Fail closed if any network cannot be retrieved: an incomplete catalog could
    select the wrong 'nearest' station. May take time on first invocation.
    """
    records = []
    failures = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_network_stations, state): state for state in STATES}
        for future in as_completed(futures):
            try:
                records.extend(future.result())
            except Exception as exc:
                failures.append(f'{futures[future]}: {exc}')
    if failures:
        raise RuntimeError('Incomplete IEM station catalog: ' + '; '.join(sorted(failures)))
    if not records:
        raise RuntimeError('IEM returned no stations')
    return pd.DataFrame(records).drop_duplicates(['network', 'station_id']).reset_index(drop=True)


def nearest_station(latitude: float, longitude: float, max_distance_km: float = MAX_STATION_DISTANCE_KM) -> dict:
    """Find nearest catalog station for any coordinates; refuse unsupported locations."""
    lat, lon = float(latitude), float(longitude)
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError('Invalid latitude or longitude')
    stations = station_catalog()
    lat1, lon1 = np.radians([lat, lon])
    lat2 = np.radians(stations['latitude'].to_numpy(dtype=float))
    lon2 = np.radians(stations['longitude'].to_numpy(dtype=float))
    h = np.sin((lat2 - lat1)/2)**2 + np.cos(lat1)*np.cos(lat2)*np.sin((lon2 - lon1)/2)**2
    distances = 6371.0088 * 2 * np.arctan2(np.sqrt(h), np.sqrt(np.maximum(0, 1-h)))
    idx = int(np.argmin(distances))
    result = stations.iloc[idx].to_dict()
    result['distance_km'] = float(distances[idx])
    if result['distance_km'] > max_distance_km:
        raise ValueError(f'No IEM ASOS station within {max_distance_km:g} km; nearest is '
                         f'{result["distance_km"]:.1f} km away. No score returned.')
    return result


def fetch_station_observations(network: str, station_id: str, prediction_time_utc: datetime) -> pd.DataFrame:
    """Use the original IEM ASOS fields, report types and units."""
    end = prediction_time_utc + timedelta(minutes=1)
    start = prediction_time_utc - timedelta(hours=9)
    params = [('network', network), ('station', station_id)]
    params.extend(('data', name) for name in RAW_FEATURES)
    params.extend([('year1', str(start.year)), ('month1', str(start.month)), ('day1', str(start.day)),
                   ('year2', str(end.year)), ('month2', str(end.month)), ('day2', str(end.day)),
                   ('tz', 'Etc/UTC'), ('format', 'onlycomma'), ('missing', 'M'),
                   ('trace', 'T'), ('latlon', 'no'), ('elev', 'no'),
                   ('report_type', '3'), ('report_type', '4'), ('direct', 'no')])
    # The historical IEM endpoint accepts dates, not times; filter the returned
    # reports locally to ensure no future observations enter feature generation.
    response = _request(ASOS_URL + '?' + urlencode(params), timeout=90)
    data = response.decode('utf-8-sig', errors='replace')
    if data.lstrip().lower().startswith(('<html', '<!doctype', 'error')):
        raise RuntimeError('IEM returned an error instead of METAR CSV')
    frame = pd.read_csv(io.StringIO(data), comment='#', na_values=['M', ''], low_memory=False)
    needed = {'valid', *RAW_FEATURES}
    if not needed.issubset(frame.columns):
        raise ValueError(f'IEM response missing columns: {sorted(needed-set(frame.columns))}')
    return frame


def weather_at_snapshots(raw: pd.DataFrame, prediction_time_utc: datetime) -> pd.DataFrame:
    """Exact feature recipe from build_dev300_v2.weather_at_snapshots,
    generalized from the fixed 2025 hourly grid to four recent hourly snapshots.
    """
    t = pd.Timestamp(prediction_time_utc)
    if t.tzinfo is None:
        raise ValueError('prediction_time_utc must include a timezone')
    t = t.tz_convert('UTC')
    if t.minute or t.second or t.microsecond:
        raise ValueError('prediction_time_utc must be at the top of an hour')
    df = raw.copy()
    df['valid_dt'] = pd.to_datetime(df['valid'], utc=True, errors='coerce')
    df = df.dropna(subset=['valid_dt'])
    df = df[df.valid_dt <= t]
    df = df.sort_values('valid_dt', kind='stable').drop_duplicates('valid_dt', keep='first')
    df['precip_trace_reported'] = df['p01i'].astype(str).str.upper().eq('T').astype(int)
    for feature in RAW_FEATURES:
        if feature == 'p01i':
            df[feature] = df[feature].replace({'T': '0', 't': '0'})
        df[feature] = pd.to_numeric(df[feature], errors='coerce')
    df['available_dt'] = df.valid_dt + pd.Timedelta(minutes=DELAY_MINUTES)
    hourly = pd.DataFrame({'prediction_time_utc': pd.date_range(t - pd.Timedelta(hours=3), t, freq='h')})
    fields = ['available_dt', 'valid_dt', 'precip_trace_reported'] + RAW_FEATURES
    asof = pd.merge_asof(hourly, df[fields].sort_values('available_dt'),
                         left_on='prediction_time_utc', right_on='available_dt',
                         direction='backward',
                         tolerance=pd.Timedelta(minutes=MAX_AGE_MINUTES - DELAY_MINUTES))
    asof['has_station_report'] = asof.valid_dt.notna().astype('int8')
    asof['observation_age_minutes'] = (asof.prediction_time_utc - asof.valid_dt).dt.total_seconds()/60
    asof['gust_reported'] = asof.gust.notna().astype('int8')
    asof['mslp_reported'] = asof.mslp.notna().astype('int8')
    asof['alti_reported'] = asof.alti.notna().astype('int8')
    asof['precip_trace_reported'] = asof.precip_trace_reported.fillna(0).astype('int8')
    for feature in ('mslp', 'tmpf', 'relh', 'sknt'):
        asof[f'{feature}_change_3h'] = asof[feature] - asof[feature].shift(3)
    hr = asof.prediction_time_utc.dt.hour
    mo = asof.prediction_time_utc.dt.month
    asof['hour_sin'] = np.sin(2*np.pi*hr/24)
    asof['hour_cos'] = np.cos(2*np.pi*hr/24)
    asof['month_sin'] = np.sin(2*np.pi*(mo-1)/12)
    asof['month_cos'] = np.cos(2*np.pi*(mo-1)/12)
    return asof.drop(columns=['available_dt'])


@lru_cache(maxsize=2)
def load_model(model_path: str | None = None) -> dict:
    """Load only trusted joblib artifacts; pickle can execute code."""
    bundle = joblib.load(model_path or MODEL_FILE)
    if not isinstance(bundle, dict) or not {'model', 'features'}.issubset(bundle):
        raise ValueError('Expected a model bundle with model and features')
    return bundle


def predict(latitude: float, longitude: float, *, prediction_time_utc: datetime | None = None,
            model_path: str | None = None, max_station_distance_km: float = MAX_STATION_DISTANCE_KM) -> dict:
    """Get a research warning-screening score for a coordinate (no plant ID needed).

    Returns unavailable if a recent METAR report cannot be found. All API calls
    are synchronous; call from a backend worker, not an async event loop.
    """
    now = datetime.now(timezone.utc)
    if prediction_time_utc is None:
        t = now.replace(minute=0, second=0, microsecond=0)
    else:
        t = pd.Timestamp(prediction_time_utc)
        if t.tzinfo is None:
            raise ValueError('prediction_time_utc must include timezone')
        t = t.tz_convert('UTC').to_pydatetime()
    if t > now + timedelta(minutes=1):
        raise ValueError('Cannot predict using future observations')
    station = nearest_station(latitude, longitude, max_station_distance_km)
    raw = fetch_station_observations(station['network'], station['station_id'], t)
    features = weather_at_snapshots(raw, t)
    last = features.iloc[-1].copy()
    if int(last['has_station_report']) != 1:
        return {'status': 'unavailable', 'reason': 'No recent eligible METAR report',
                'station': station, 'prediction_time_utc': t.isoformat()}
    last['distance_km'] = station['distance_km']
    bundle = load_model(model_path)
    expected = list(bundle['features'])
    missing = sorted(set(expected) - set(last.index))
    if missing:
        raise ValueError(f'Preprocessing missing model features: {missing}')
    x = pd.DataFrame([last]).loc[:, expected].apply(pd.to_numeric, errors='coerce').astype('float32')
    score = float(bundle['model'].predict_proba(x)[0, 1])
    if not math.isfinite(score):
        raise ValueError('Model returned nonfinite score')
    return {'status': 'ok', 'model': 'metar_2022_2024_final_hgb', 'experimental_score': score,
            'prediction_target': 'new NWS Severe Thunderstorm Warning affecting location within 6 hours',
            'prediction_time_utc': t.isoformat(),
            'observation_time_utc': pd.Timestamp(last['valid_dt']).isoformat(),
            'observation_age_minutes': float(last['observation_age_minutes']),
            'station': station, 'calibrated': False, 'official_nws_warning': False}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Research-only METAR warning screening for arbitrary coordinates')
    parser.add_argument('latitude', type=float)
    parser.add_argument('longitude', type=float)
    args = parser.parse_args()
    print(json.dumps(predict(args.latitude, args.longitude), indent=2))
