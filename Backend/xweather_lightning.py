"""Xweather measured pulses -> internal lightning feed. Never logs credentials."""
from datetime import datetime, timedelta, timezone
import math
import os
from email.utils import parsedate_to_datetime
import httpx
from risk_sources import parse_time, utcnow

URL = 'https://data.api.xweather.com/lightning/closest'
PAGE_SIZE = 1000
MAX_PAGES = 3
WINDOW_SECONDS = 240  # Within standard five-minute access, with request-latency margin.
_retry_until = None


def credentials():
    client_id = os.environ.get('PLANIT_XWEATHER_CLIENT_ID', '')
    secret = os.environ.get('PLANIT_XWEATHER_CLIENT_SECRET', '')
    if client_id and secret:
        return client_id, secret
    combined = os.environ.get('PLANIT_XWEATHER_API_KEY', '')
    if combined.count('_') == 1:
        client_id, secret = combined.split('_', 1)
        if client_id and secret:
            return client_id, secret
    return None


def map_records(rows, now):
    events = []
    for row in rows:
        loc, ob = row['loc'], row['ob']
        lat, lon = loc['lat'], loc['long']
        if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (lat, lon))
                or not -90 <= lat <= 90 or not -180 <= lon <= 180):
            raise ValueError('Invalid coordinates')
        identifier = row['id']
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 200:
            raise ValueError('Missing event identity')
        instant = (datetime.fromtimestamp(ob['timestampMS'] / 1000, timezone.utc)
                   if isinstance(ob.get('timestampMS'), (int, float)) and not isinstance(ob['timestampMS'], bool)
                   else parse_time(ob.get('dateTimeISOMS') or ob['dateTimeISO']))
        if not now - timedelta(minutes=5) <= instant <= now + timedelta(seconds=5):
            raise ValueError('Event outside recent window')
        if str(ob['pulse']['type']).lower() not in ('cg', 'ic'):
            raise ValueError('Unsupported pulse type')
        events.append({'id': identifier, 'time': instant.isoformat(), 'latitude': lat, 'longitude': lon})
    return events


async def fetch(site, client):
    global _retry_until
    now = utcnow().replace(microsecond=0)
    start = now - timedelta(seconds=WINDOW_SECONDS)
    base = {'provider': 'xweather', 'retrieved_at': now.isoformat(), 'simulated': False}
    pair = credentials()
    if not pair:
        return {**base, 'availability': 'unknown', 'reason': 'xweather_credentials_missing_or_invalid', 'events': None}
    if _retry_until and now < _retry_until:
        return {**base, 'availability': 'unavailable', 'reason': 'xweather_rate_limit_cooldown',
                'events': None, 'retry_at': _retry_until.isoformat()}
    params = {'client_id': pair[0], 'client_secret': pair[1],
              'p': f"{site['latitude']:.6f},{site['longitude']:.6f}", 'radius': '10miles',
              'filter': 'all', 'format': 'json', 'limit': PAGE_SIZE,
              'from': int(start.timestamp()), 'to': int(now.timestamp())}
    events, complete, reason = [], False, None
    try:
        for page in range(MAX_PAGES):
            response = await client.get(URL, params={**params, 'skip': page * PAGE_SIZE}, timeout=10)
            response.raise_for_status()
            payload = response.json()
            # warn_no_data with success=true/response=[] is a valid empty query.
            error = payload.get('error')
            if payload.get('success') is not True or (error and error.get('code') != 'warn_no_data'):
                reason = 'xweather_api_error'
                break
            rows = payload['response']
            if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
                raise ValueError('Invalid response collection')
            events.extend(map_records(rows, now))
            if len(rows) < PAGE_SIZE:
                complete = True
                break
        if not complete:
            reason = reason or 'xweather_pagination_incomplete'
    except httpx.HTTPStatusError as error:
        reason = f'provider_http_{error.response.status_code}'
        if error.response.status_code == 429:
            value = error.response.headers.get('Retry-After')
            try:
                delay = float(value)
            except (ValueError, TypeError):
                try:
                    delay = (parsedate_to_datetime(value) - now).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    delay = 60
            _retry_until = now + timedelta(seconds=max(1, min(86400, delay)) if math.isfinite(delay) else 60)
    except httpx.HTTPError:
        reason = 'provider_request_failed'
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        reason = 'xweather_schema_invalid'
    # Validated events from completed pages still trigger stop-work on a partial
    # failure; incomplete coverage can never clear a hold or provide ML counts.
    return {**base, 'availability': 'available' if complete else 'unavailable',
            'reason': reason, 'events': events,
            'coverage_start': start.isoformat() if complete else None,
            'coverage_end': now.isoformat() if complete else None,
            'coverage_window_seconds': WINDOW_SECONDS}
