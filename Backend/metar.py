"""IEM METAR observations for the frozen six-hour NWS warning model.

The training methodology is supplied, but its raw weather builder is absent.
No receipt timestamp is inferred from an observation.
"""

import asyncio
import csv
from datetime import datetime, timedelta, timezone
from io import StringIO
import math
import time

import httpx

import database
from risk_sources import distance_km, parse_time, utcnow

BASE = "https://mesonet.agron.iastate.edu"
STATIONS_URL = BASE + "/geojson/network.py"
OBS_URL = BASE + "/cgi-bin/request/asos.py"
FIELDS = ("tmpf", "dwpf", "relh", "drct", "sknt", "gust", "p01i", "alti", "mslp", "vsby")
# The frozen model bundle and training scripts use this exact feature order.
FEATURE_ORDER = ("distance_km", "precip_trace_reported", "tmpf", "dwpf", "relh", "drct",
                 "sknt", "gust", "p01i", "alti", "mslp", "vsby", "has_station_report",
                 "observation_age_minutes", "gust_reported", "mslp_reported", "alti_reported",
                 "mslp_change_3h", "tmpf_change_3h", "relh_change_3h", "sknt_change_3h",
                 "hour_sin", "hour_cos", "month_sin", "month_cos")
_locks = {}


def _lock(key):
    return _locks.setdefault(key, asyncio.Lock())


async def _get(client, url, params):
    """A bounded retry; honor Retry-After without blocking other feeds indefinitely."""
    for attempt in range(3):
        try:
            response = await client.get(url, params=params, timeout=12)
            if response.status_code in (429, 503) and attempt < 2:
                delay = min(2.0, max(0.0, float(response.headers.get("Retry-After", "1"))))
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            return response
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == 2:
                raise
            await asyncio.sleep(0.4 * (attempt + 1))
    raise RuntimeError("METAR retry exhausted")


def choose_station(site, features, now=None):
    """Nearest currently online US ASOS-class station; provisional eligibility."""
    now = now or utcnow()
    candidates = []
    for feature in features:
        props = feature.get("properties") or {}
        point = feature.get("geometry") or {}
        coords = point.get("coordinates") or []
        if (not props.get("online") or props.get("country") != "US"
                or not str(props.get("network", "")).endswith("_ASOS")
                or len(coords) < 2 or not props.get("sid")):
            continue
        lon, lat = coords[:2]
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (lat, lon)):
            continue
        candidates.append((distance_km(site['latitude'], site['longitude'], lat, lon),
                           str(props['sid']), lat, lon))
    if not candidates:
        raise ValueError("No eligible online US ASOS station in metadata")
    distance, sid, lat, lon = min(candidates, key=lambda row: (row[0], row[1]))
    return {"station_id": sid, "station_latitude": lat, "station_longitude": lon,
            "distance_km": distance, "selected_at": now.isoformat(),
            "provenance": "IEM AZOS online US state ASOS network; provisional eligibility"}


async def station_for(site, client):
    saved = database.metar_station(site)
    if saved:
        return saved
    async with _lock("station-catalog"):
        saved = database.metar_station(site)
        if saved:
            return saved
        key = "metar:AZOS:online:v1"
        catalog = database.load_provider_cache(key, time.time())
        if catalog is None:
            response = await _get(client, STATIONS_URL, {"network": "AZOS", "only_online": "1"})
            catalog = response.json()
            if not isinstance(catalog.get("features"), list) or not catalog['features']:
                raise ValueError("Empty IEM station catalog")
            database.save_provider_cache(key, time.time() + 86400, catalog)
        station = choose_station(site, catalog['features'])
        database.save_metar_station(site, station)
        return database.metar_station(site)


def _number(value):
    if value is None or str(value).strip() in ("", "M", "null", "None"):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def parse_observations(raw, station_id):
    lines = [line for line in raw.splitlines() if line.strip() and not line.startswith('#')]
    rows = []
    for row in csv.DictReader(StringIO("\n".join(lines))):
        if row.get('station') != station_id:
            continue
        instant = parse_time(row['valid']).astimezone(timezone.utc)
        values = {field: _number(row.get(field)) for field in FIELDS if field != 'p01i'}
        precip = row.get('p01i')
        values['precip_trace_reported'] = int(precip == 'T')
        # Training represents a reported trace as zero measurable inches plus a flag.
        values['p01i'] = 0.0 if precip == 'T' else _number(precip)
        rows.append({"observed_at": instant.isoformat(), **values})
    return sorted(rows, key=lambda row: row['observed_at'])


async def observations(station, client, now=None):
    now = now or utcnow()
    # Current lookback: 60-minute delay + 120-minute tolerance. Prior adds 3h.
    # Eight hours allow boundary and reporting cadence slack.
    end = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    key = f"metar:observations:v1:{station['station_id']}:{end.isoformat()}"
    async with _lock(key):
        cached = database.load_provider_cache(key, time.time())
        if cached is not None:
            return {**cached, 'cache_hit': True}
        params = {"station": station['station_id'], "data": ",".join(FIELDS),
                  "sts": (end - timedelta(hours=8)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                  "ets": (end + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                  "tz": "Etc/UTC", "format": "onlycomma", "missing": "M", "trace": "T",
                  "report_type": "3,4"}
        response = await _get(client, OBS_URL, params)
        rows = parse_observations(response.text, station['station_id'])
        result = {"observations": rows, "retrieved_at": utcnow().isoformat(),
                  "source_url": OBS_URL, "cache_hit": False}
        database.save_provider_cache(key, time.time() + 120, result)
        return result


def aligned(rows, target):
    """Latest report at least 60 and at most 180 minutes old at target."""
    cutoff = target - timedelta(minutes=60)
    eligible = [row for row in rows if parse_time(row['observed_at']) <= cutoff
                and timedelta(0) <= cutoff - parse_time(row['observed_at']) <= timedelta(minutes=120)]
    return max(eligible, key=lambda row: row['observed_at']) if eligible else None


def features_for(station, rows, target):
    target = target.astimezone(timezone.utc)
    current = aligned(rows, target)
    prior = aligned(rows, target - timedelta(hours=3))
    features = {field: current.get(field) if current else None for field in FIELDS}
    features.update(distance_km=round(station['distance_km'], 3),
                    precip_trace_reported=current['precip_trace_reported'] if current else 0,
                    has_station_report=int(current is not None),
                    observation_age_minutes=(target - parse_time(current['observed_at'])).total_seconds() / 60 if current else None,
                    gust_reported=int(current.get('gust') is not None) if current else 0,
                    mslp_reported=int(current.get('mslp') is not None) if current else 0,
                    alti_reported=int(current.get('alti') is not None) if current else 0)
    for field in ('mslp', 'tmpf', 'relh', 'sknt'):
        a, b = (current or {}).get(field), (prior or {}).get(field)
        features[field + '_change_3h'] = a - b if a is not None and b is not None else None
    features.update(hour_sin=math.sin(2 * math.pi * target.hour / 24),
                    hour_cos=math.cos(2 * math.pi * target.hour / 24),
                    month_sin=math.sin(2 * math.pi * (target.month - 1) / 12),
                    month_cos=math.cos(2 * math.pi * (target.month - 1) / 12))
    return {"features": {key: features[key] for key in FEATURE_ORDER},
            "feature_order": list(FEATURE_ORDER), "current_observed_at": current['observed_at'] if current else None,
            "prior_observed_at": prior['observed_at'] if prior else None,
            "availability_assumption": "observed_at plus 60 minutes; receipt time unverified",
            "alignment_status": "methodology_aligned_raw_builder_unavailable"}


async def assess(site, client, now=None):
    now = now or utcnow()
    result = {"availability": "pending", "provider": "Iowa Environmental Mesonet",
              "station": database.metar_station(site), "reason": None}
    try:
        station = result['station'] or await station_for(site, client)
        result['station'] = station
        fetched = await observations(station, client, now)
        engineered = features_for(station, fetched['observations'], now)
        result.update(engineered, retrieved_at=fetched['retrieved_at'], cache_hit=fetched['cache_hit'],
                      source_url=fetched['source_url'], observation_count=len(fetched['observations']),
                      latest_observed_at=(fetched['observations'][-1]['observed_at']
                                          if fetched['observations'] else None))
        if engineered['current_observed_at'] is None:
            result.update(availability='stale' if fetched['observations'] else 'unavailable',
                          reason=('no_aligned_observation_within_180_minute_window'
                                  if fetched['observations'] else 'no_station_observations_in_requested_history'))
        else:
            result['availability'] = 'available'
    except (httpx.HTTPError, ValueError, KeyError, TypeError, RuntimeError) as error:
        result.update(availability='unavailable', reason=(f'provider_http_{error.response.status_code}'
                      if isinstance(error, httpx.HTTPStatusError) else type(error).__name__))
    return result
