"""Probe documented Xweather lightning access without exposing credentials.

Reads the local .env (not printed) or process environment. No production database
writes, raw response/URL/error logging, or invented strike records.
"""
import json
import logging
import os
from pathlib import Path
import sys
import argparse
import asyncio

import httpx


def read_env():
    path = Path(__file__).resolve().parents[1] / '.env'
    values = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                name, value = line.split('=', 1)
                values[name.strip().removeprefix('export ')] = value.strip().strip('\"').strip("'")
    return {**values, **os.environ}


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adapter', action='store_true', help='Verify production adapter in memory, without touching saved state')
    args = parser.parse_args()
    env = read_env()
    if args.adapter:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import xweather_lightning
        for name in ('PLANIT_XWEATHER_API_KEY', 'PLANIT_XWEATHER_CLIENT_ID', 'PLANIT_XWEATHER_CLIENT_SECRET'):
            if name in env:
                os.environ[name] = env[name]
        async def run():
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                return await xweather_lightning.fetch({'latitude': 25.75, 'longitude': -80.7}, client)
        feed = asyncio.run(run())
        print(json.dumps({'adapter': 'xweather', 'availability': feed['availability'], 'reason': feed.get('reason'),
            'returned_validated_events': len(feed.get('events') or []), 'coverage_start': feed.get('coverage_start'),
            'coverage_end': feed.get('coverage_end'), 'coverage_window_seconds': feed.get('coverage_window_seconds'),
            'production_state_written': False}))
        return 0 if feed['availability'] == 'available' else 1
    combined = env.get('PLANIT_XWEATHER_API_KEY') or env.get('PLANIT_LIGHTNING_API_KEY', '')
    client_id, secret = env.get('PLANIT_XWEATHER_CLIENT_ID'), env.get('PLANIT_XWEATHER_CLIENT_SECRET')
    if not (client_id and secret) and combined.count('_') == 1:
        client_id, secret = combined.split('_', 1)
    if not client_id or not secret:
        print(json.dumps({'access_verified': False, 'reason': 'client_id_and_secret_required'}))
        return 2
    # Fixed demo coordinates, real request, bounded cost: one five-minute query.
    params = {'client_id': client_id, 'client_secret': secret, 'p': '25.75,-80.7',
              'radius': '10miles', 'filter': 'all', 'limit': 1000, 'format': 'json'}
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            response = client.get('https://data.api.xweather.com/lightning/closest', params=params)
        try:
            data = response.json()
        except ValueError:
            data = {}
        rows = data.get('response') if isinstance(data, dict) else None
        result = {'http_status': response.status_code, 'api_success': data.get('success'),
                  'access_verified': response.is_success and data.get('success') is True and isinstance(rows, list),
                  'returned_records': len(rows) if isinstance(rows, list) else None,
                  'coordinates': [25.75, -80.7], 'radius_miles': 10,
                  'requested_history': 'documented default past five minutes'}
        code = (data.get('error') or {}).get('code')
        result['error_code'] = code if code in ('warn_no_data', 'invalid_client', 'invalid_secret',
            'invalid_request', 'insufficient_permissions', 'unauthorized', 'rate_limit_exceeded') else 'provider_error' if code else None
        print(json.dumps(result))
        return 0 if result['access_verified'] else 1
    except httpx.HTTPError:
        print(json.dumps({'access_verified': False, 'reason': 'network_or_timeout'}))
        return 3


if __name__ == '__main__':
    sys.exit(main())
