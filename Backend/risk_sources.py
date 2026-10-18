"""Public environmental feeds, with bounded requests and persistent caching."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import math
import os
import time

import httpx

import database
from weather import request_params, weather_response

URLS = {
    "weather": "https://api.open-meteo.com/v1/forecast",
    "alerts": "https://api.weather.gov/alerts/active",
    "wildfires": "https://eonet.gsfc.nasa.gov/api/v3/events",
    "wildlife": "https://api.inaturalist.org/v1/observations",
}


def utcnow():
    return datetime.now(timezone.utc)


def parse_time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def distance_km(latitude, longitude, other_latitude, other_longitude):
    lat1, lat2 = math.radians(latitude), math.radians(other_latitude)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(other_longitude - longitude)
    value = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, max(0, value))))


def parse_alerts(data):
    features = data["features"]
    if not isinstance(features, list):
        raise ValueError("Expected an alert feature list")
    alerts = []
    for feature in features:
        item = feature["properties"]
        if item.get("status") != "Actual" or item.get("messageType") == "Cancel":
            continue
        if parse_time(item["expires"]) <= utcnow():
            continue
        alerts.append({key: item.get(key) for key in (
            "id", "event", "severity", "certainty", "urgency", "headline",
            "description", "instruction", "onset", "effective", "sent", "ends", "expires", "areaDesc", "web",
        )})
    return {"alerts": alerts}


def parse_fires(data):
    if not isinstance(data["events"], list):
        raise ValueError("Expected a wildfire event list")
    events, unsupported = [], 0
    for event in data["events"]:
        if event.get("closed") is not None:
            continue
        geometries = event["geometry"]
        if not geometries:
            continue
        geometry = max(geometries, key=lambda value: parse_time(value["date"]))
        if parse_time(geometry["date"]) < utcnow() - timedelta(days=14):
            continue
        if geometry["type"] != "Point":
            unsupported += 1
            continue
        longitude, latitude = geometry["coordinates"]
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("Invalid wildfire coordinates")
        events.append({
            "id": event["id"], "name": event["title"], "url": event["link"],
            "latitude": latitude, "longitude": longitude,
            "reported_at": geometry["date"], "sources": event.get("sources", []),
        })
    return {"events": events, "unsupported_geometries": unsupported}


def parse_wildlife(data):
    if not isinstance(data["results"], list) or not isinstance(data["total_results"], int):
        raise ValueError("Expected observation results and count")
    sightings = []
    for observation in data["results"]:
        # Keep sensitive locations obscured. No sighting coordinates or observer names are exposed.
        taxon = observation.get("taxon") or {}
        sightings.append({
            "id": observation["id"], "url": observation["uri"],
            "observed_on": observation.get("observed_on"),
            "species": taxon.get("name"), "common_name": taxon.get("preferred_common_name"),
            "quality_grade": observation.get("quality_grade"),
            "location_obscured": bool(observation.get("obscured")
                                      or observation.get("geoprivacy") in ("obscured", "private")
                                      or observation.get("taxon_geoprivacy") in ("obscured", "private")),
        })
    return {"total_results": data["total_results"], "sightings": sightings,
            "radius_km": 10, "lookback_days": 30, "sample_limit": 20}


class RiskSources:
    def __init__(self, client):
        self.client = client
        self.locks = {name: asyncio.Lock() for name in URLS}
        self.last_wildlife_request = 0.0

    async def fetch(self, name, params, ttl, transform, cache_namespace=None):
        key = (cache_namespace or name) + ":v2:" + json.dumps(params, sort_keys=True)
        # Cache and lock by provider. Batch requests share a client and these locks;
        # EONET's global feed is downloaded once, not once per plant.
        lock = self.locks.setdefault(cache_namespace or name, asyncio.Lock())
        async with lock:
            cached = database.load_provider_cache(key, time.time())
            if cached is not None:
                return {**cached, "cache_hit": True}
            if name == "wildlife":
                await asyncio.sleep(max(0, 1.1 - (time.monotonic() - self.last_wildlife_request)))
                self.last_wildlife_request = time.monotonic()
            result = {"url": URLS[name], "retrieved_at": utcnow().isoformat(), "cache_hit": False}
            try:
                response = await self.client.get(URLS[name], params=params)
                response.raise_for_status()
                result.update(status="available", data=transform(response.json()))
            except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
                reason = (f"HTTP {error.response.status_code}" if isinstance(error, httpx.HTTPStatusError)
                          else "request failed" if isinstance(error, httpx.HTTPError)
                          else "invalid provider response")
                result.update(status="unavailable", data=None, error=reason)
                ttl = min(ttl, 60)
            result["expires_at"] = (utcnow() + timedelta(seconds=ttl)).isoformat()
            database.save_provider_cache(key, time.time() + ttl, result)
            return result

    async def for_lightning(self, site):
        latitude, longitude = site["latitude"], site["longitude"]

        def current_weather(data):
            current = data["current"]
            # Read required fields so malformed data is recorded as a failure.
            current["weather_code"]
            current["time"]
            return {"current": current, "utc_offset_seconds": data["utc_offset_seconds"]}

        weather, alerts = await asyncio.gather(
            self.fetch("weather", {"latitude": latitude, "longitude": longitude,
                                   "current": "weather_code", "timezone": "auto"}, 300, current_weather,
                       cache_namespace=f"lightning-weather:{site['id']}"),
            # Separate cache key: the fast monitor must not inherit the full job's
            # five-minute alert cache. This also keeps slow-provider locks separate.
            self.fetch("alerts", {"point": f"{latitude:.4f},{longitude:.4f}", "status": "actual"},
                       30, parse_alerts, cache_namespace=f"lightning-alerts:{site['id']}"),
        )
        return {"weather": weather, "alerts": alerts}

    async def for_site(self, site):
        latitude, longitude = site["latitude"], site["longitude"]
        today = utcnow().date()
        results = await asyncio.gather(
            self.fetch("weather", request_params(latitude, longitude), 900,
                       lambda data: weather_response(data, latitude, longitude)),
            self.fetch("alerts", {"point": f"{latitude:.4f},{longitude:.4f}", "status": "actual"},
                       300, parse_alerts),
            self.fetch("wildfires", {"category": "wildfires", "status": "open", "days": 14},
                       1800, parse_fires),
            self.fetch("wildlife", {
                "lat": latitude, "lng": longitude, "radius": 10, "taxon_id": 1,
                "captive": "false", "quality_grade": "research", "per_page": 20,
                "order_by": "observed_on", "order": "desc",
                "d1": (today - timedelta(days=30)).isoformat(), "d2": today.isoformat(),
            }, 86400, parse_wildlife),
        )
        return dict(zip(URLS, results))


def http_client():
    return httpx.AsyncClient(
        timeout=httpx.Timeout(15, connect=5), follow_redirects=True,
        headers={"User-Agent": os.environ.get("PLANIT_USER_AGENT", "PlanIT-Control/1.0"),
                 "Accept": "application/geo+json, application/json"},
        limits=httpx.Limits(max_connections=8),
    )
