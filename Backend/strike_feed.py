"""Measured strike adapter and durable lightning holds. No simulated production feed."""

from datetime import datetime, timedelta, timezone
import hashlib
import math
import os
import logging
import re
from urllib.parse import urlsplit, urlunsplit

import httpx
import alerts
import database
import xweather_lightning
from risk_sources import distance_km, parse_time, utcnow, http_client
from fastapi import APIRouter, HTTPException
from refresh_locks import lease, RefreshBusy

router = APIRouter(prefix='/api', tags=['Measured lightning'])

URL = "https://api.weather.com/v3/wx/lightning/60minute/desktop"
MILES_10_KM = 16.09344
FRESH_SECONDS = 90


class _SecretUrlFilter(logging.Filter):
    def filter(self, record):
        # httpx INFO logs include request query strings, even when exceptions are
        # safely caught. Remove those queries before any handler sees the record.
        message = record.getMessage()
        def redact(match):
            parts = urlsplit(match.group(0))
            return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
        record.msg = re.sub(r'https://(?:data\.api\.xweather\.com|api\.weather\.com)/[^\s\"]*', redact, message)
        record.args = ()
        return True


logging.getLogger('httpx').addFilter(_SecretUrlFilter())


def provider_name():
    selected = os.environ.get('PLANIT_LIGHTNING_PROVIDER', 'auto').lower()
    if selected == 'auto':
        return 'xweather' if (os.environ.get('PLANIT_XWEATHER_API_KEY') or os.environ.get('PLANIT_XWEATHER_CLIENT_ID')
                             or os.environ.get('PLANIT_XWEATHER_CLIENT_SECRET')) else 'the_weather_company'
    return selected


def initialize():
    with database.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS lightning_state (
            site_id INTEGER PRIMARY KEY, latest_strike_at TEXT, deadline TEXT,
            last_attempt_at TEXT, last_success_at TEXT, coverage_start TEXT,
            availability TEXT NOT NULL DEFAULT 'unknown', reason TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS lightning_events (
            provider TEXT NOT NULL, event_id TEXT NOT NULL, site_id INTEGER NOT NULL,
            time TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
            distance_miles REAL NOT NULL, received_at TEXT NOT NULL,
            PRIMARY KEY(provider,event_id,site_id))""")
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(lightning_state)')}
        if 'configuration_provider' not in columns:
            conn.execute('ALTER TABLE lightning_state ADD COLUMN configuration_provider TEXT')


def configured():
    if provider_name() == 'xweather':
        return bool(xweather_lightning.credentials())
    return provider_name() == 'the_weather_company' and bool(os.environ.get("PLANIT_LIGHTNING_API_KEY"))


def map_twc(payload, now):
    """Provider boundary: validate actual coordinates/times, never infer strikes."""
    fields = [payload[key] for key in ("latitude", "longitude", "validTimeUtc")]
    if any(not isinstance(field, list) for field in fields) or len({len(field) for field in fields}) != 1:
        raise ValueError("Invalid strike arrays")
    events = []
    for lat, lon, value in zip(*fields):
        if (isinstance(lat, bool) or isinstance(lon, bool) or not isinstance(lat, (int, float))
                or not isinstance(lon, (int, float)) or not math.isfinite(lat) or not math.isfinite(lon)
                or not -90 <= lat <= 90 or not -180 <= lon <= 180):
            raise ValueError("Invalid strike coordinates")
        # TWC's validTimeUtc is explicitly UTC, including offset-less API values.
        instant = parse_time(value)
        if instant > now + timedelta(seconds=5) or instant < now - timedelta(hours=2):
            raise ValueError("Strike outside the recent observation window")
        event_id = hashlib.sha256(f"{lat}:{lon}:{instant.isoformat()}".encode()).hexdigest()
        events.append({"id": event_id, "time": instant.isoformat(), "latitude": lat, "longitude": lon})
    return events


async def fetch(site, client):
    if provider_name() == 'xweather':
        return await xweather_lightning.fetch(site, client)
    if provider_name() != 'the_weather_company':
        return {'provider': 'unknown', 'availability': 'unknown', 'events': None,
                'reason': 'lightning_provider_not_supported', 'simulated': False}
    now = utcnow()
    base = {"provider": "the_weather_company", "retrieved_at": now.isoformat(), "simulated": False}
    key = os.environ.get("PLANIT_LIGHTNING_API_KEY")
    if not key:
        return {**base, "availability": "unknown", "reason": "measured_lightning_not_configured", "events": None}
    try:
        response = await client.get(URL, params={"geocode": f"{site['latitude']:.6f},{site['longitude']:.6f}",
                                               "apiKey": key, "format": "json", "units": "e", "strikeType": "all"})
        response.raise_for_status()
        events = map_twc(response.json(), now)
        return {**base, "availability": "available", "events": events,
                "coverage_start": (now - timedelta(minutes=55)).isoformat(), "coverage_end": now.isoformat()}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as error:
        # Never log the HTTP exception URL: TWC authenticates in the query string.
        reason = f"provider_http_{error.response.status_code}" if isinstance(error, httpx.HTTPStatusError) else "provider_request_or_schema_failed"
        return {**base, "availability": "unavailable", "reason": reason, "events": None}


def apply(site, feed, now=None):
    """Commit strikes, hold reset, alert and notification in one transaction.

    Internal adapter contract; there is intentionally no public strike-injection
    endpoint. Synthetic events belong only in isolated test databases.
    """
    now = now or utcnow()
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO lightning_state(site_id) VALUES (?)", (site["id"],))
        previous = dict(conn.execute("SELECT * FROM lightning_state WHERE site_id=?", (site["id"],)).fetchone())
        available = feed["availability"] == "available"
        coverage_start = feed.get('coverage_start')
        if provider_name() == 'xweather' and available and coverage_start and previous.get('coverage_start') and previous.get('last_success_at'):
            same_provider = (previous.get('configuration_provider') == provider_name() or
                             previous.get('configuration_provider') is None and provider_name() == 'the_weather_company')
            if (same_provider and parse_time(coverage_start) <= parse_time(previous['last_success_at'])
                    and parse_time(feed['coverage_end']) >= parse_time(previous['last_success_at'])):
                coverage_start = min(parse_time(coverage_start), parse_time(previous['coverage_start'])).isoformat()
        conn.execute("""UPDATE lightning_state SET last_attempt_at=?,availability=?,reason=?,
            last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,
            coverage_start=CASE WHEN ? THEN ? ELSE coverage_start END,
            configuration_provider=CASE WHEN ? THEN ? ELSE configuration_provider END WHERE site_id=?""",
            (now.isoformat(), feed["availability"], feed.get("reason"), available, feed.get("coverage_end"),
             available, coverage_start, available, provider_name(), site["id"]))
        latest = parse_time(previous["latest_strike_at"]) if previous["latest_strike_at"] else None
        for event in sorted(feed.get("events") or [], key=lambda item: parse_time(item["time"])):
            instant = parse_time(event["time"])
            if instant > now or instant < now - timedelta(hours=2):
                continue
            distance = distance_km(site["latitude"], site["longitude"], event["latitude"], event["longitude"])
            if distance > MILES_10_KM:
                continue
            inserted = conn.execute("INSERT OR IGNORE INTO lightning_events VALUES (?,?,?,?,?,?,?,?)",
                (feed["provider"], event["id"], site["id"], instant.isoformat(), event["latitude"], event["longitude"],
                 distance / 1.609344, now.isoformat())).rowcount
            if not inserted or (latest and instant <= latest):
                continue
            latest = instant
            deadline = instant + timedelta(minutes=30)
            conn.execute("UPDATE lightning_state SET latest_strike_at=?,deadline=? WHERE site_id=?",
                         (instant.isoformat(), deadline.isoformat(), site["id"]))
            if deadline > now:
                alerts.upsert(site["id"], "lightning_strike", "severe", "Measured lightning within 10 miles: stop outdoor work",
                    {"strike_time": instant.isoformat(), "deadline": deadline.isoformat(), "distance_miles": distance / 1.609344,
                     "latitude": event["latitude"], "longitude": event["longitude"], "provider": feed["provider"], "simulated": False},
                    origin="measured_lightning", stop_work=True, now=now, notify_update=True, connection=conn)
        current = _state(dict(conn.execute("SELECT * FROM lightning_state WHERE site_id=?", (site["id"],)).fetchone()), now)
        if current["status"] == "clear":
            alerts.resolve(site["id"], "lightning_strike", "measured_lightning", now, conn)
    return current


def _state(row, now):
    deadline = parse_time(row["deadline"]) if row.get("deadline") else None
    end = parse_time(row["last_success_at"]) if row.get("last_success_at") else None
    start = parse_time(row["coverage_start"]) if row.get("coverage_start") else None
    matching_provider = (row.get('configuration_provider') == provider_name() or
                         row.get('configuration_provider') is None and provider_name() == 'the_weather_company')
    fresh = (configured() and matching_provider and row["availability"] == "available" and end is not None
             and timedelta(0) <= now - end <= timedelta(seconds=FRESH_SECONDS))
    # The timer expiring alone never authorizes clearance. A fresh successful
    # feed must cover the entire preceding 30 minutes, including any outage.
    coverage_complete = bool(fresh and start and start <= now - timedelta(minutes=30))
    status = "stop_work" if deadline and now < deadline else "clear" if coverage_complete else "unknown"
    remaining = max(0, math.ceil((deadline - now).total_seconds())) if deadline else None
    return {**row, "status": status, "availability": "available" if fresh else "unknown",
            "countdown_remaining_seconds": remaining, "coverage_complete": coverage_complete,
            "measured_strike_feed_configured": configured(), "simulated": False,
            'provider_verification': {
                'provider': provider_name(), 'credentials_configured': configured(),
                'feed_access': ('not_configured' if not configured() else 'verified_current' if fresh
                                else 'previously_verified' if end and matching_provider else 'not_verified'),
                'last_successful_coverage_at': row.get('last_success_at'),
                'strike_detection': 'observed' if row.get('latest_strike_at') else 'not_yet_observed',
                'last_error': row.get('reason'),
                'verification_url': f"/api/sites/{row['site_id']}/lightning/verify"},
            "server_time": now.isoformat(),
            "reason": ('measured_lightning_not_configured' if not configured() else
                       row.get("reason") or (None if fresh else "No fresh measured lightning coverage"))}


def state(site_id, now=None):
    with database.connect() as conn:
        row = conn.execute("SELECT * FROM lightning_state WHERE site_id=?", (site_id,)).fetchone()
    return _state(dict(row) if row else {"site_id": site_id, "availability": "unknown", "latest_strike_at": None,
                                       "deadline": None, "last_success_at": None, "coverage_start": None}, now or utcnow())


def measured_count(site_id, target_time, now=None):
    """Count validated records within 10 miles over the latest covered 30 minutes.

    Window ends at min(current sample time, latest successful feed end). No
    future samples are filled; an incomplete/outdated coverage window stays null.
    """
    now = now or utcnow()
    target = parse_time(target_time)
    status = state(site_id, now)
    base = {'count': None, 'availability': 'unknown', 'radius_miles': 10, 'window_minutes': 30,
            'target_time': target.isoformat(), 'window_start': None, 'window_end': None,
            'coverage_start': status.get('coverage_start'), 'coverage_end': status.get('last_success_at')}
    if target > now or status['availability'] != 'available' or not status.get('coverage_start'):
        return {**base, 'reason': 'measured_coverage_missing_or_future_target'}
    end = min(target, parse_time(status['last_success_at']))
    start = end - timedelta(minutes=30)
    if (target-end).total_seconds() > FRESH_SECONDS or parse_time(status['coverage_start']) > start:
        return {**base, 'reason': 'incomplete_counting_window'}
    with database.connect() as conn:
        site = dict(conn.execute('SELECT * FROM sites WHERE id=?', (site_id,)).fetchone())
        rows = conn.execute('SELECT time,latitude,longitude FROM lightning_events WHERE site_id=? AND julianday(time)>julianday(?) AND julianday(time)<=julianday(?)',
                            (site_id,start.isoformat(),end.isoformat())).fetchall()
    count = 0
    for row in rows:
        lat, lon = row['latitude'], row['longitude']
        if (all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in (lat,lon))
                and -90 <= lat <= 90 and -180 <= lon <= 180
                and distance_km(site['latitude'],site['longitude'],lat,lon) <= MILES_10_KM):
            count += 1
    return {**base, 'count': count, 'availability': 'available', 'reason': None,
            'window_start': start.isoformat(), 'window_end': end.isoformat(), 'window_semantics': '(start,end]'}


async def refresh_site(site, client):
    async with lease('strikes:' + str(site['id'])):
        return apply(site, await fetch(site, client))


@router.get('/sites/{site_id}/lightning/strikes')
def get_strike_state(site_id: int):
    if not database.find_site(site_id):
        raise HTTPException(404, 'Site not found')
    return state(site_id)


@router.post('/sites/{site_id}/lightning/verify')
async def verify_provider(site_id: int):
    """Make an actual adapter request; never inject events or infer entitlement.

    A successful empty feed verifies access, not observation of an actual strike.
    Missing credentials/provider failure persist unknown coverage and preserve holds.
    """
    site = database.find_site(site_id)
    if not site:
        raise HTTPException(404, 'Site not found')
    try:
        async with http_client() as client:
            return await refresh_site(site, client)
    except RefreshBusy as error:
        raise HTTPException(409, str(error)) from error
