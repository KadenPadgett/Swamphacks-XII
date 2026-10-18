"""Rule-based current and hourly screens, independent of trained ML inference."""

import asyncio
from datetime import datetime, timedelta, timezone
import math
import os

import database
import ml_risk
import metar_model
import freshness
from risk_sources import RiskSources, distance_km, http_client, parse_time, utcnow
from refresh_locks import lease

METHOD_VERSION = "planning-screen-v2"
ASSESSMENT_REVISION = 8
LEVELS = {"unknown": -1, "info": -1, "low": 0, "watch": 1, "high": 2, "severe": 3}
ALERT_TERMS = {
    "weather": ("wind", "tornado", "hurricane", "tropical", "storm", "snow", "blizzard", "ice"),
    "extreme_heat": ("heat",), "lightning": ("thunderstorm", "lightning"),
    "flooding": ("flood",), "wildfire": ("red flag", "fire weather", "wildfire", "fire warning"),
}


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def threshold(value, watch, high, severe):
    if not numeric(value):
        return "unknown"
    return "severe" if value >= severe else "high" if value >= high else "watch" if value >= watch else "low"


def worst(*levels):
    return max(levels, key=lambda value: LEVELS[value]) if levels else "unknown"


def coverage(*values):
    count = sum(numeric(value) for value in values)
    return "available" if count == len(values) else "partial" if count else "unavailable"


def card(level, basis, evidence, sources, availability):
    return {"level": level, "basis": basis, "evidence": evidence,
            "sources": sources, "coverage": availability}


def overall(indicators):
    known = worst(*(item["level"] if item["level"] != "info" else "unknown"
                    for item in indicators.values()))
    gaps = [name for name, item in indicators.items()
            if item["coverage"] != "available" or item["level"] == "unknown"]
    return {
        "assessment_type": "rule_based", "level": "unknown" if gaps and known == "low" else known,
        "highest_known_level": known,
        "method": "Maximum rule-based indicator level; wildlife is informational",
        "drivers": [name for name, item in indicators.items()
                    if item["level"] == known and LEVELS[known] > 0],
        "coverage_complete": not gaps, "coverage_gaps": gaps,
    }


def alert_applies(alert, instant):
    """A future-onset alert must not become a current hazard."""
    start = alert.get("onset") or alert.get("effective") or alert.get("sent")
    end = alert.get("ends") or alert.get("expires")
    return bool(end and (not start or parse_time(start) <= instant) and instant < parse_time(end))


def assess_frame(site, sources, point, instant, rain_24h, hourly_rain, predicted=False):
    alerts_available = sources["alerts"]["status"] == "available"
    alerts = [alert for alert in (sources["alerts"].get("data") or {}).get("alerts", [])
              if alert_applies(alert, instant)]

    def with_alerts(category, item):
        matches = [alert for alert in alerts if any(
            term in (alert.get("event") or "").lower() for term in ALERT_TERMS[category]
        )]
        item["evidence"]["alerts"] = matches
        item["sources"].append("alerts")
        for alert in matches:
            severity = {"Extreme": "severe", "Severe": "high", "Moderate": "watch", "Minor": "watch"}.get(
                alert.get("severity"), "watch")
            if "Warning" in (alert.get("event") or ""):
                severity = worst(severity, "high")
            item["level"] = worst(item["level"], severity)
        if not alerts_available:
            item["coverage"] = "partial" if item["coverage"] != "unavailable" else "unavailable"
        elif item["coverage"] == "unavailable":
            item["coverage"] = "partial"
        return item

    gust, heat, moisture = (point.get(field) for field in
                           ("wind_gusts_10m", "apparent_temperature", "soil_moisture_3_to_9cm"))
    indicators = {
        "weather": with_alerts("weather", card(
            worst(threshold(gust, 30, 45, 65), threshold(hourly_rain, 0.3, 0.75, 1.0)),
            "Wind and preceding-hour precipitation at this time",
            {"gust_mph": gust, "preceding_hour_precipitation_in": hourly_rain,
             "gust_thresholds_mph": [30, 45, 65], "rain_thresholds_in": [0.3, 0.75, 1.0]},
            ["weather"], coverage(gust, hourly_rain),
        )),
        "extreme_heat": with_alerts("extreme_heat", card(
            threshold(heat, 90, 100, 110), "Apparent-temperature rule at this time",
            {"apparent_temperature_f": heat, "thresholds_f": [90, 100, 110]},
            ["weather"], coverage(heat),
        )),
        "flooding": with_alerts("flooding", card(
            threshold(rain_24h, 1, 2, 4),
            "Applicable NWS alerts and preceding 24-hour precipitation; no inundation model",
            {"preceding_24h_precipitation_in": rain_24h, "rain_thresholds_in": [1, 2, 4]},
            ["weather"], coverage(rain_24h),
        )),
        "soil_saturation": card(
            threshold(moisture, 0.30, 0.40, 0.50),
            "Modeled soil wetness at this time; saturation needs soil-specific porosity",
            {"volumetric_water_content": moisture, "unit": "m³/m³", "depth_cm": [3, 9],
             "thresholds": [0.30, 0.40, 0.50], "saturation_percent": None},
            ["weather"], coverage(moisture),
        ),
    }
    code = point.get("weather_code")
    thunder_level = "high" if code in (95, 96, 99) else "low" if numeric(code) else "unknown"
    lightning = with_alerts("lightning", card(
        "high" if thunder_level == "high" else "unknown",
        "Thunderstorm model indicator is separate from unknown measured lightning",
        {"weather_code": code}, ["weather"], "partial" if numeric(code) else "unavailable",
    ))
    lightning["thunderstorm_forecast"] = {
        "availability": "available" if numeric(code) else "unknown", "level": thunder_level,
        "weather_code": code, "time_scope": "predicted" if predicted else "current_model_estimate",
    }
    lightning["measured_lightning_strikes"] = {
        "availability": "unknown", "level": "unknown", "count": None,
        "reason": "No measured lightning-strike feed is connected",
    }
    lightning["coverage"] = "partial" if numeric(code) or alerts_available else "unavailable"
    indicators["lightning"] = lightning

    if not predicted:
        fire_data = sources["wildfires"].get("data") or {}
        nearby = []
        for fire in fire_data.get("events", []):
            distance = distance_km(site["latitude"], site["longitude"], fire["latitude"], fire["longitude"])
            if distance <= 50:
                nearby.append({**fire, "distance_km": round(distance, 2)})
        nearby.sort(key=lambda item: item["distance_km"])
        nearest = nearby[0]["distance_km"] if nearby else None
        fire_level = ("high" if nearest is not None and nearest <= 10 else
                      "watch" if nearest is not None and nearest <= 25 else
                      "low" if sources["wildfires"]["status"] == "available" else "unknown")
        indicators["wildfire"] = with_alerts("wildfire", card(
            fire_level, "Currently reported wildfire points and applicable NWS fire alerts; not fire perimeters",
            {"nearby_events": nearby, "search_radius_km": 50, "high_within_km": 10,
             "watch_within_km": 25, "lookback_days": 14}, ["wildfires"],
            "partial" if fire_data.get("unsupported_geometries") else sources["wildfires"]["status"],
        ))
        indicators["wildlife_sightings"] = card(
            "info" if sources["wildlife"]["status"] == "available" else "unknown",
            "Recent sightings provide ecological context, not a danger rating",
            sources["wildlife"].get("data") or {}, ["wildlife"], sources["wildlife"]["status"],
        )
    return {
        "time": instant.isoformat(), "assessment_type": "rule_based",
        "time_scope": "predicted" if predicted else "current",
        "overall": overall(indicators), "indicators": indicators,
    }


def weather_time(value, offset):
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone(timedelta(seconds=offset)))
    return instant


def hourly_points(data, offset):
    result = {}
    for index, timestamp in enumerate(data.get("time", [])):
        result[weather_time(timestamp, offset)] = {
            field: values[index] for field, values in data.items()
            if field != "time" and isinstance(values, list) and index < len(values)
        }
    return result


def preceding_rain(points, end, hours):
    values = [points.get(end - timedelta(hours=hour), {}).get("precipitation") for hour in range(hours)]
    return round(sum(values), 4) if all(numeric(value) for value in values) else None


def assess(site, sources):
    now = utcnow()
    weather = sources["weather"].get("data") or {}
    offset = weather.get("utc_offset_seconds", 0)
    current_point = weather.get("current", {})
    current_time = weather_time(current_point["time"], offset) if current_point.get("time") else now
    observation_boundary = current_time.replace(minute=0, second=0, microsecond=0)
    boundary = now.astimezone(timezone(timedelta(seconds=offset))).replace(minute=0, second=0, microsecond=0)
    history = hourly_points(weather.get("recent_hourly", {}), offset)
    history = {time: point for time, point in history.items() if time <= observation_boundary}
    predictions = hourly_points(weather.get("hourly", {}), offset)
    predictions = {time: point for time, point in predictions.items() if time > now}
    # Alerts apply now, even when the latest weather observation predates them.
    current = assess_frame(site, sources, current_point, now,
                           preceding_rain(history, observation_boundary, 24),
                           preceding_rain(history, observation_boundary, 1))
    current["weather_observed_at"] = current_time.isoformat() if current_point.get("time") else None
    current["alerts_evaluated_at"] = now.isoformat()
    current["rainfall_as_of"] = observation_boundary.isoformat() if history else None

    # Missing forecast hours stay unknown instead of disappearing.
    hourly = []
    past_and_predicted = {**history, **predictions}
    for hour in range(1, 49):
        instant = boundary + timedelta(hours=hour)
        point = predictions.get(instant, {})
        hourly.append(assess_frame(site, sources, point, instant,
                                  preceding_rain(past_and_predicted, instant, 24),
                                  point.get("precipitation"), predicted=True))
    peak = max(hourly, key=lambda item: LEVELS[item["overall"]["highest_known_level"]])
    peaks = {}
    for category in hourly[0]["indicators"]:
        item = max(hourly, key=lambda frame: LEVELS[frame["indicators"][category]["level"]])
        peaks[category] = {"time": item["time"], **item["indicators"][category]}
        if any(frame["indicators"][category]["coverage"] != "available" for frame in hourly):
            peaks[category]["coverage"] = ("partial" if peaks[category]["coverage"] == "available"
                                           else peaks[category]["coverage"])
    highest = {
        "assessment_type": "rule_based", "time_scope": "upcoming",
        "time": peak["time"] if peak["overall"]["highest_known_level"] != "unknown" else None,
        "overall": overall(peaks), "indicators": peaks,
        "forecast_data_complete": all(
            all(numeric(predictions.get(boundary + timedelta(hours=hour), {}).get(field))
                for field in ("wind_gusts_10m", "apparent_temperature", "soil_moisture_3_to_9cm",
                              "precipitation", "weather_code")) for hour in range(1, 49)
        ),
        "unavailable_predictions": ["wildfire_spread", "wildlife_sightings", "measured_lightning_strikes"],
    }
    return {
        "site": site, "generated_at": now.isoformat(), "method_version": METHOD_VERSION,
        "assessment_revision": ASSESSMENT_REVISION,
        "assessment_type": "rule_based", "forecast_hours": 48,
        "current_risk": current, "hourly_predicted_risk": hourly, "highest_upcoming_risk": highest,
        # Legacy fields now refer only to current risk, never the forecast maximum.
        "overall": current["overall"], "indicators": current["indicators"],
        "sources": {name: {key: value for key, value in source.items() if key != "data"}
                    for name, source in sources.items()},
        "weather": weather or None,
    }


def snapshot_is_fresh(snapshot, site):
    return (snapshot is not None and snapshot.get("method_version") == METHOD_VERSION
            and snapshot.get("assessment_revision") == ASSESSMENT_REVISION
            and snapshot.get("ml_configuration_id") == ml_risk.configuration_id()
            and snapshot.get("metar_ml_configuration_id", metar_model.configuration_id()) == metar_model.configuration_id()
            and snapshot.get("site") == site
            and not freshness.assessment(snapshot, now=utcnow())['stale'])


async def refresh_site(site, sources):
    # On-demand requests and full jobs also share a per-site lease.
    before = database.load_risk(site['id'])
    async with lease(f"site-risk:{site['id']}", wait_seconds=120):
        latest = database.load_risk(site['id'])
        if latest and latest.get('generated_at') != (before or {}).get('generated_at'):
            # A concurrent initial/manual/scheduled worker already refreshed this
            # site while we waited. Partial coverage can be retry-due immediately,
            # but it must not cause the same wave of requests to run twice.
            return {**latest, 'cached': True, 'stale': not snapshot_is_fresh(latest, site)}
        return await refresh_site_locked(site, sources)


async def refresh_site_locked(site, sources):
    import alerts, briefings, rivers, metar, metar_model
    snapshot = database.load_risk(site["id"])
    if snapshot_is_fresh(snapshot, site) and not freshness.assessment(snapshot, now=utcnow())['refresh_due']:
        alerts.evaluate(snapshot, include_lightning=False)
        return {**snapshot, "cached": True, "stale": False}
    result = assess(site, await sources.for_site(site))
    zone = (result.get("weather") or {}).get("timezone")
    if zone:
        briefings.set_timezone(site["id"], zone, source="weather")
    # Emit newly dangerous direct weather/NWS rules immediately, without clearing
    # existing river episodes until the combined flood evidence is evaluated.
    alerts.evaluate(result, include_lightning=False, allow_resolution=False)
    result = await rivers.augment_assessment(site, result, sources.client)
    # Notify rule-based safety actions before waiting on the optional ML service.
    alerts.evaluate(result, include_lightning=False)
    result['ml_context'] = ml_risk.capture_context(result)
    result["ml_assessment"] = await ml_risk.predict(result)
    result["ml_configuration_id"] = ml_risk.configuration_id()
    result['metar'] = (await metar.assess(site, sources.client, now=parse_time(result['generated_at']))
                       if os.environ.get('PLANIT_METAR_ENABLED', '1').lower() not in ('0', 'false', 'no')
                       else {"availability": "unavailable", "reason": "metar_disabled", "station": database.metar_station(site)})
    result['weather_model'] = await metar_model.predict(result, result['metar'])
    result['metar_ml_configuration_id'] = metar_model.configuration_id()
    result = database.save_refreshed_risk(site["id"], result)
    return {**result, "cached": False, "stale": False}


async def get_site_risk(site):
    async with http_client() as client:
        return await refresh_site(site, RiskSources(client))


async def refresh_many(sites):
    import refresh_jobs
    result = await refresh_jobs.run("full", sites=sites)
    return result["risks"]


def risk_overview(sites):
    import site_responses
    return site_responses.overview(sites)
