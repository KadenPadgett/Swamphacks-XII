"""NOAA NWPS gauge context and an explicitly rule-based river flood screen."""

import asyncio
from datetime import timedelta
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
from pathlib import Path
import time
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, HttpUrl, model_validator
import database
from risk_sources import distance_km, http_client, parse_time, utcnow
from refresh_locks import RefreshBusy, lease

BASE_URL = "https://api.water.noaa.gov/nwps/v1/gauges"
router = APIRouter(prefix="/api", tags=["Rivers"])


def initialize():
    with database.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS site_rivers (
            site_id INTEGER PRIMARY KEY, gauge_id TEXT, exposure_confirmed INTEGER NOT NULL DEFAULT 0,
            association TEXT NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS river_exposure_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT, site_id INTEGER NOT NULL,
            gauge_id TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
            reviewed_at TEXT NOT NULL, payload TEXT NOT NULL)""")
    seed_documented_connections()


def exposure_for(site_id, gauge_id=None):
    """Documented relevance is bound to the reviewed site and gauge, not proximity."""
    with database.connect() as conn:
        site = conn.execute('SELECT * FROM sites WHERE id=?', (site_id,)).fetchone()
        mapping = conn.execute('SELECT gauge_id,exposure_confirmed FROM site_rivers WHERE site_id=?', (site_id,)).fetchone()
        row = conn.execute('SELECT * FROM river_exposure_reviews WHERE site_id=? ORDER BY id DESC LIMIT 1',
                           (site_id,)).fetchone()
    result = {'status': 'unconfirmed', 'site_relevance_verified': False,
              'reason': 'documented_exposure_review_required', 'review': None,
              'site_inundation_probability': None,
              'limitation': 'Verified river relevance does not establish jobsite inundation or a safe river-stage threshold'}
    if not row:
        return result
    review = json.loads(row['payload'])
    result['review'] = {**review, 'id': row['id'], 'reviewed_at': row['reviewed_at'],
                        'site_latitude': row['latitude'], 'site_longitude': row['longitude'], 'gauge_id': row['gauge_id']}
    if (not site or not mapping or mapping['gauge_id'] != row['gauge_id']
            or (gauge_id is not None and gauge_id != row['gauge_id'])
            or site['latitude'] != row['latitude'] or site['longitude'] != row['longitude']):
        return {**result, 'status': 'invalidated', 'reason': 'reviewed_site_or_gauge_changed'}
    if review.get('decision') != 'confirmed':
        return {**result, 'reason': 'review_does_not_confirm_relevance'}
    if not mapping['exposure_confirmed']:
        return {**result, 'reason': 'exposure_confirmation_withdrawn'}
    return {**result, 'status': 'verified', 'site_relevance_verified': True, 'reason': None}


def saved(site_id):
    with database.connect() as conn:
        row = conn.execute("SELECT * FROM site_rivers WHERE site_id=?", (site_id,)).fetchone()
    if not row:
        return None
    verification = exposure_for(site_id, row['gauge_id'])
    confirmed = bool(row['exposure_confirmed']) and verification['site_relevance_verified']
    context = {**json.loads(row['payload']), 'exposure_confirmed': confirmed,
               'exposure_verification': verification}
    return {**dict(row), 'exposure_confirmed': confirmed, 'exposure_verification': verification, 'context': context}


def store(site_id, gauge_id, context, confirmed=False, association="nearest_gauge"):
    with database.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO site_rivers VALUES (?,?,?,?,?,?)",
                     (site_id, gauge_id, int(confirmed), association, utcnow().isoformat(), json.dumps(context)))


class MissingData(ValueError):
    """The provider responded successfully but omitted the requested data."""


def _safe_url(url):
    parts = urlsplit(url)
    # Never persist query strings, userinfo, response bodies or exception text.
    return urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))


def _cached_any(key):
    with database.connect() as conn:
        row = conn.execute("SELECT payload FROM provider_cache WHERE cache_key=?", (key,)).fetchone()
    return json.loads(row["payload"]) if row else None


def _cooldown_key(url):
    parts = urlsplit(url)
    family = "hefs" if parts.path.startswith("/hefs/") else "nwps"
    # NWPS collection and detail routes have distinct limits in live responses.
    route = "catalogue" if parts.path.rstrip("/").endswith("/gauges") else "data"
    return f"river:cooldown:{parts.hostname}:{family}:{route}"


def _retry_after(value):
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = (parsedate_to_datetime(value) - utcnow()).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 300
    return max(1, seconds) if math.isfinite(seconds) else 300


async def fetch_json(client, url, params=None, ttl=300, validator=None):
    """Fetch public NOAA JSON with safe diagnostics and bounded retry.

    ``validator(data)`` raises MissingData for absent usable data, ValueError or
    TypeError for an invalid schema. Lists are accepted when a validator is used.
    A failed attempt may return old data, but always status=unavailable/stale=True;
    retrieved_at and expires_at remain those of the successful source retrieval.
    """
    digest = hashlib.sha256((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
    key = "river:v2:" + digest
    cached = database.load_provider_cache(key, time.time())
    if cached:
        return {**cached, "cache_hit": True}
    # Coalesce identical catalogue/gauge requests across concurrent sites/workers.
    try:
        async with lease("river-fetch:" + digest, wait_seconds=10, ttl=30):
            cached = database.load_provider_cache(key, time.time())
            if cached:
                return {**cached, "cache_hit": True}
            return await _fetch_json_locked(client, url, params, ttl, validator, key)
    except RefreshBusy:
        return _failure_result(url, _cached_any(key), "request_in_progress", None, 0)


def _failure_result(url, previous, error, http_status, attempts):
    retained = previous if previous and previous.get("data") is not None else {}
    return {"url": _safe_url(url), "status": "unavailable", "data": retained.get("data"),
            "retrieved_at": retained.get("retrieved_at"), "expires_at": retained.get("expires_at"),
            "last_attempt_at": utcnow().isoformat(), "cache_hit": bool(retained),
            "stale": bool(retained), "error": error, "http_status": http_status, "attempts": attempts}


async def _fetch_json_locked(client, url, params, ttl, validator, key):
    previous = _cached_any(key)
    cooldown_key = _cooldown_key(url)
    cooldown = database.load_provider_cache(cooldown_key, time.time())
    if cooldown:
        result = _failure_result(url, previous, cooldown.get("error", "rate_limited"),
                                 cooldown.get("http_status", 429), 0)
        result["retry_at"] = cooldown["retry_at"]
        return result
    error, status, attempts, delay = None, None, 0, 60
    for attempt in range(2):
        attempts += 1
        response = None
        status = None
        try:
            # asyncio timeout also bounds streaming/custom transports; unrelated
            # hazard alerts are emitted before this optional enrichment by risk.py.
            async with asyncio.timeout(4):
                response = await client.get(url, params=params, timeout=4)
            status = response.status_code
            response.raise_for_status()
            if status == 204:
                raise MissingData("Empty provider response")
            data = response.json()
            if not isinstance(data, dict) and not (validator and isinstance(data, list)):
                raise ValueError("Expected JSON object")
            if validator:
                validator(data)
            now = utcnow()
            result = {"url": _safe_url(url), "status": "available", "data": data,
                      "retrieved_at": now.isoformat(), "last_attempt_at": now.isoformat(),
                      "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
                      "cache_hit": False, "stale": False, "http_status": status, "attempts": attempts}
            database.save_provider_cache(key, time.time() + ttl, result)
            return result
        except (httpx.TimeoutException, TimeoutError):
            error = "timeout"
        except httpx.HTTPStatusError:
            error = ("rate_limited" if status == 429 else "missing_data" if status in (204, 404)
                     else "http_error")
        except httpx.RequestError:
            error = "network_error"
        except MissingData:
            error = "missing_data"
        except (ValueError, TypeError, KeyError):
            error = "invalid_schema"
        if status == 429 or (status in (500, 502, 503, 504) and response.headers.get("Retry-After")):
            delay = _retry_after(response.headers.get("Retry-After"))
            retry_at = (utcnow() + timedelta(seconds=delay)).isoformat()
            database.save_provider_cache(cooldown_key, time.time() + delay,
                                         {"retry_at": retry_at, "error": error, "http_status": status})
            break
        if attempt == 0 and (error in ("timeout", "network_error") or status in (500, 502, 503, 504)):
            await asyncio.sleep(0.25)
            continue
        break
    result = _failure_result(url, previous, error, status, attempts)
    result["retry_at"] = (utcnow() + timedelta(seconds=delay)).isoformat()
    database.save_provider_cache(key, time.time() + delay, result)
    return result


def _validate_catalogue(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid gauge catalogue")
    if "gauges" not in data:
        raise MissingData("Missing gauge catalogue")
    if not isinstance(data["gauges"], list) or any(not isinstance(row, dict) for row in data["gauges"]):
        raise ValueError("Invalid gauge catalogue")
    for gauge in data["gauges"]:
        _validate_gauge(gauge)


def _validate_gauge(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid gauge object")
    if not data.get("lid"):
        raise MissingData("Missing gauge")
    if not isinstance(data["lid"], str):
        raise ValueError("Invalid gauge identifier")
    for field, bound in (("latitude", 90), ("longitude", 180)):
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or abs(value) > bound:
            raise ValueError("Invalid gauge coordinates")
    for field in ("status", "flood"):
        if field in data and not isinstance(data[field], dict):
            raise ValueError("Invalid gauge metadata")
    status = data.get("status", {})
    if "observed" in status and not isinstance(status["observed"], dict):
        raise ValueError("Invalid gauge observation")
    observed = status.get("observed", {})
    if observed.get("validTime"):
        if not isinstance(observed["validTime"], str):
            raise ValueError("Invalid observation time")
        parse_time(observed["validTime"])
    for field in ("primary", "secondary"):
        value = observed.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)):
            raise ValueError("Invalid gauge reading")
    flood = data.get("flood", {})
    if "categories" in flood and not isinstance(flood["categories"], dict):
        raise ValueError("Invalid flood thresholds")


async def context_for(site, client):
    previous = saved(site["id"])
    gauge_id = previous.get("gauge_id") if previous else None
    confirmed = bool(previous and previous["exposure_confirmed"])
    association = previous["association"] if previous else "nearest_gauge"
    discovery_source = previous["context"].get("discovery_source") if previous else None
    if not gauge_id:
        lat, lon = site["latitude"], site["longitude"]
        # NOAA's collection route limits live clients to 10 requests / 5 minutes.
        # One shared daily catalogue avoids one discovery request for every site.
        # bbox.* and srid=EPSG_4326 are documented, optional query parameters.
        response = await fetch_json(client, BASE_URL, {"srid": "EPSG_4326"}, ttl=86400,
                                    validator=_validate_catalogue)
        discovery_source = {k: v for k, v in response.items() if k != "data"}
        gauges = (response.get("data") or {}).get("gauges", [])
        candidates = []
        for gauge in gauges:
            try:
                distance = distance_km(lat, lon, gauge["latitude"], gauge["longitude"])
                if distance <= 10 and gauge.get("lid"):
                    candidates.append((distance, gauge["lid"]))
            except (KeyError, TypeError, ValueError):
                continue
        if not candidates:
            context = {"availability": "unknown", "reason": "no_gauge_within_10_km" if response["status"] == "available" else response.get("error", "river_provider_failed"),
                       "exposure_confirmed": False, "gauge": None, "source": {k:v for k,v in response.items() if k != "data"}}
            store(site["id"], None, context)
            return context
        gauge_id = min(candidates)[1]
    response = await fetch_json(client, BASE_URL + "/" + gauge_id, validator=_validate_gauge)
    data = response.get("data") or {}
    if not data and previous and previous["context"].get("observation"):
        # Persisted site evidence outlives the provider cache. Preserve it when an
        # outage occurs after cache eviction; a new attempt is not a new reading.
        context = previous["context"]
        old_source = context.get("source", {})
        context.update(availability="unavailable", reason=response.get("error", "missing_data"),
                       exposure_confirmed=confirmed, association=association,
                       freshness={"status": "stale", "observation_max_age_seconds": 10800})
        context["source"] = {k: v for k, v in response.items() if k != "data"}
        context["source"].update(retrieved_at=old_source.get("retrieved_at"),
                                 expires_at=old_source.get("expires_at"), stale=True)
        store(site["id"], gauge_id, context, confirmed, association)
        return context
    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, (int, float)) and value in (-999, -9999):
            return None
        return value
    observed = clean(data.get("status", {}).get("observed", {}))
    observed_at = observed.get("validTime")
    try:
        fresh = bool(observed_at and timedelta(0) <= utcnow() - parse_time(observed_at) <= timedelta(hours=3))
        distance = distance_km(site["latitude"], site["longitude"], data["latitude"], data["longitude"])
    except (KeyError, TypeError, ValueError):
        fresh, distance = False, None
    context = {"availability": "available" if response["status"] == "available" and fresh else "unavailable",
               "reason": response.get("error") if response["status"] != "available" else
                   (None if fresh else "stale_observation" if observed_at else "missing_data"),
               "freshness": {"status": "fresh" if fresh and response["status"] == "available" else
                             "stale" if observed_at else "missing", "observation_max_age_seconds": 10800},
               "association": association, "exposure_confirmed": confirmed,
               "exposure_verification": exposure_for(site['id'], gauge_id),
               "discovery_source": discovery_source,
               "limitation": "Nearby gauge proximity does not prove hydraulic connection or floodplain exposure",
               "gauge": {"id": gauge_id, "name": data.get("name"), "latitude": data.get("latitude"),
                         "longitude": data.get("longitude"), "distance_km": distance},
               "observation": observed, "thresholds": clean(data.get("flood", {}).get("categories", {})),
               "threshold_units": {"stage": data.get("flood", {}).get("stageUnits"),
                                   "flow": data.get("flood", {}).get("flowUnits")},
               "forecast_context": clean(data.get("status", {}).get("forecast")),
               "source": {k:v for k,v in response.items() if k != "data"}}
    store(site["id"], gauge_id, context, confirmed, association)
    return context


def apply_context(assessment, context):
    import risk
    card = assessment["current_risk"]["indicators"]["flooding"]
    rain = card["evidence"].get("preceding_24h_precipitation_in")
    confirmed = bool(context.get('exposure_confirmed') and
                     context.get('exposure_verification', {}).get('site_relevance_verified'))
    card["evidence"]["river_context"] = context
    card["evidence"]["direct_site_rule_level"] = card["level"]
    card["evidence"]["river_gauge_relevance"] = (
        "confirmed_site_exposure" if confirmed else "unconfirmed_nearby_context")
    reasons = []
    level = "unknown"
    if context.get("gauge") and risk.numeric(rain) and rain >= 1:
        level = "high" if confirmed else "watch"
        reasons.append("At least 1 inch of preceding 24-hour rain at a site with " +
                       ("confirmed river exposure" if confirmed else "a nearby gauge; exposure unconfirmed"))
    category = context.get("observation", {}).get("floodCategory")
    river_level = {"major": "severe", "moderate": "high", "minor": "high", "action": "watch",
                       "major_flooding": "severe", "moderate_flooding": "high", "minor_flooding": "high",
                       "near_flood": "watch", "no_flooding": "low"}.get(category, "unknown")
    use_gauge = context.get("availability") == "available" and confirmed
    card["evidence"]["nearby_river_conditions"] = {
        "reported_flood_category": category, "indicative_river_level": river_level,
        "used_for_site_escalation": use_gauge,
        "limitation": "A gauge flood category describes the river, not whether this jobsite floods"}
    if use_gauge:
        level = risk.worst(level, river_level)
        if risk.LEVELS[river_level] >= 1:
            reasons.append(f"Gauge with confirmed site exposure reports {category}")
    card["level"] = risk.worst(card["level"], level)
    card["evidence"]["river_warning_reasons"] = reasons
    card["basis"] = "Rule-based rainfall and applicable NWS flood warnings; gauge escalation requires confirmed site exposure"
    if "river" not in card["sources"]:
        card["sources"].append("river")
    if context["availability"] != "available" and card["coverage"] == "available":
        card["coverage"] = "partial"
    assessment["sources"]["river"] = {**context.get("source", {}),
                                       "status": context.get("source", {}).get("status", context["availability"]),
                                       "data_availability": context["availability"],
                                       "reason": context.get("reason"), "observed_at": context.get("observation", {}).get("validTime")}
    assessment["current_risk"]["overall"] = risk.overall(assessment["current_risk"]["indicators"])
    assessment["overall"] = assessment["current_risk"]["overall"]
    assessment["indicators"] = assessment["current_risk"]["indicators"]
    return assessment


async def augment_assessment(site, assessment, client):
    return apply_context(assessment, await context_for(site, client))


class RiverAssociation(BaseModel):
    gauge_id: str = Field(min_length=3, max_length=20, pattern=r"^[A-Za-z0-9]+$")
    exposure_confirmed: bool = False
    review: 'ExposureReview | None' = None

    @model_validator(mode='after')
    def requires_review(self):
        if self.exposure_confirmed and (self.review is None or self.review.decision != 'confirmed'):
            raise ValueError('Confirmed exposure requires a documented confirming review')
        return self


class ExposureReview(BaseModel):
    decision: str = Field(pattern=r'^(confirmed|unconfirmed)$')
    river_name: str = Field(min_length=2, max_length=200)
    evidence_type: str = Field(pattern=r'^(official_facility_connection|hydraulic_review|floodplain_review)$')
    evidence: str = Field(min_length=30, max_length=4000, pattern=r'\S')
    source_urls: list[HttpUrl] = Field(min_length=1, max_length=10)
    reviewed_by: str = Field(min_length=3, max_length=200, pattern=r'\S')


RiverAssociation.model_rebuild()


def save_review(site, gauge_id, review):
    payload = review.model_dump(mode='json') if isinstance(review, ExposureReview) else review
    with database.connect() as conn:
        conn.execute('INSERT INTO river_exposure_reviews (site_id,gauge_id,latitude,longitude,reviewed_at,payload) VALUES (?,?,?,?,?,?)',
                     (site['id'], gauge_id, site['latitude'], site['longitude'], utcnow().isoformat(), json.dumps(payload)))


def seed_documented_connections():
    """Import reviewed real-facility evidence once; never infer from distance.

    Respect existing manual choices, prior reviews and withdrawals. A changed EIA
    identity/coordinate requires a new review rather than silently reusing one.
    No source observations or HEFS probabilities are seeded here.
    """
    path = Path(__file__).with_name('data') / 'river_exposure_reviews.json'
    records = json.loads(path.read_text(encoding='utf-8'))['records']
    for record in records:
        with database.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            site = conn.execute('SELECT * FROM sites WHERE eia_plant_id=?', (record['eia_plant_id'],)).fetchone()
            if not site or any(site[key] != record[key] for key in ('name', 'utility', 'latitude', 'longitude')):
                continue
            prior = conn.execute('SELECT * FROM site_rivers WHERE site_id=?', (site['id'],)).fetchone()
            reviewed = conn.execute('SELECT 1 FROM river_exposure_reviews WHERE site_id=?', (site['id'],)).fetchone()
            if reviewed or (prior and (prior['association'] != 'nearest_gauge'
                                      or prior['gauge_id'] not in (None, record['gauge_id']))):
                continue
            review = ExposureReview.model_validate(record).model_dump(mode='json')
            review.update(record_id=record['record_id'], source_checked_date=record['source_checked_date'],
                          hefs_limitation=record['hefs_limitation'])
            # The transaction also preserves concurrent manual choices.
            conn.execute('INSERT OR REPLACE INTO site_rivers VALUES (?,?,?,?,?,?)',
                         (site['id'], record['gauge_id'], 1, 'documented_facility_connection', utcnow().isoformat(),
                          prior['payload'] if prior else '{}'))
            conn.execute('INSERT INTO river_exposure_reviews (site_id,gauge_id,latitude,longitude,reviewed_at,payload) VALUES (?,?,?,?,?,?)',
                         (site['id'], record['gauge_id'], site['latitude'], site['longitude'], utcnow().isoformat(), json.dumps(review)))


@router.get("/sites/{site_id}/river")
def get_river(site_id: int):
    if not database.find_site(site_id):
        raise HTTPException(404, "Site not found")
    return saved(site_id) or {"site_id": site_id, "availability": "unknown", "gauge_id": None}


@router.put("/sites/{site_id}/river")
async def associate(site_id: int, payload: RiverAssociation):
    site = database.find_site(site_id)
    if not site:
        raise HTTPException(404, "Site not found")
    async with http_client() as client:
        response = await fetch_json(client, BASE_URL + "/" + payload.gauge_id, validator=_validate_gauge)
        data = response.get("data") or {}
        if response["status"] != "available" or not data.get("lid"):
            raise HTTPException(422, "Could not verify this NOAA gauge")
        store(site_id, data["lid"], {}, payload.exposure_confirmed, "manual")
        if payload.review:
            save_review(site, data['lid'], payload.review)
        return await context_for(site, client)


@router.get('/sites/{site_id}/river/exposure')
def get_exposure(site_id: int):
    if not database.find_site(site_id):
        raise HTTPException(404, 'Site not found')
    return exposure_for(site_id)
