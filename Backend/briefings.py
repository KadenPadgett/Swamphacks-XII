"""Durable next-local-day safety briefings, independent of an ML service.

The scheduler calls ``tick`` frequently. The site/date unique key and a
transactional in-app notification make concurrent ticks and restarts safe.
IANA timezones (the ``tzdata`` package on Windows) are required; an unknown
timezone is never silently replaced by the server timezone or a state guess.
"""

from datetime import date, datetime, time, timedelta, timezone
from copy import deepcopy
import json
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import database
import freshness
import ml_risk


router = APIRouter(tags=["briefings"])
LEVELS = {"unknown": -1, "info": -1, "low": 0, "watch": 1, "high": 2, "severe": 3}


def initialize():
    with database.connect() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS site_timezones (
            site_id INTEGER PRIMARY KEY, timezone TEXT NOT NULL,
            source TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS briefings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, site_id INTEGER NOT NULL,
            target_date TEXT NOT NULL, generated_at TEXT NOT NULL, payload TEXT NOT NULL,
            UNIQUE(site_id, target_date)
        )""")


def _now(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return value.astimezone(timezone.utc)


def _parse(value):
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return instant.astimezone(timezone.utc) if instant.tzinfo else None


def _zone(value):
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
        raise ValueError("Use a valid IANA timezone; install tzdata if timezone data is missing") from error


def set_timezone(site_id, iana, source="manual"):
    _zone(iana)
    if not database.find_site(site_id):
        raise ValueError("Site not found")
    with database.connect() as connection:
        # Weather refreshes may discover timezones, but never override an explicit
        # site configuration chosen by its owner.
        connection.execute("""INSERT INTO site_timezones VALUES (?, ?, ?, ?)
            ON CONFLICT(site_id) DO UPDATE SET timezone=excluded.timezone,
                source=excluded.source, updated_at=excluded.updated_at
            WHERE site_timezones.source != 'manual' OR excluded.source = 'manual'""",
            (site_id, iana, source, _now().isoformat()))
    return timezone_status(site_id)


def timezone_status(site_id):
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM site_timezones WHERE site_id=?", (site_id,)).fetchone()
    return {"availability": "available", **dict(row)} if row else {
        "site_id": site_id, "availability": "unknown", "timezone": None,
        "source": None, "updated_at": None,
        "reason": "Timezone not yet configured or discovered from weather data",
    }


def get_timezone(site_id):
    return timezone_status(site_id)["timezone"]


def _period(day, zone):
    # Construct both local midnights independently. Adding 24 UTC hours is wrong
    # on the spring/fall DST transitions (23/25-hour local calendar days).
    start = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    slots = []
    instant = start
    while instant < end:
        slots.append(instant)
        instant += timedelta(hours=1)
    return start, end, slots


def _within(rows, start, end):
    return sorted((row for row in rows if (instant := _parse(row.get("time"))) is not None
                   and start <= instant < end), key=lambda row: _parse(row["time"]))


def _rule_forecast(snapshot, start, end, slots):
    frames = _within(snapshot.get("hourly_predicted_risk", []), start, end)
    known = [row for row in frames
             if LEVELS.get(row.get("overall", {}).get("highest_known_level", "unknown"), -1) >= 0]
    highest_level = max((row["overall"]["highest_known_level"] for row in known),
                        key=LEVELS.get, default="unknown")
    peaks = [row for row in known if row["overall"]["highest_known_level"] == highest_level]
    periods = []
    for frame in peaks:
        instant = _parse(frame["time"])
        finish = min(instant + timedelta(hours=1), end)
        hazards = sorted(frame.get("overall", {}).get("drivers", []))
        if periods and periods[-1]["end"] == instant.isoformat() and periods[-1]["hazards"] == hazards:
            periods[-1]["end"] = finish.isoformat()
        else:
            periods.append({"start": instant.isoformat(), "end": finish.isoformat(),
                            "level": highest_level, "hazards": hazards})
    available_times = {_parse(frame["time"]) for frame in known}
    return {
        "assessment_type": "rule_based", "method_version": snapshot.get("method_version"),
        "availability": ("available" if all(slot in available_times for slot in slots)
                         else "partial" if known else "unavailable"),
        "highest_known_level": highest_level, "highest_risk_periods": periods,
        "hourly": frames, "expected_hours": len(slots), "available_hours": len(available_times),
        "missing_hours": [slot.isoformat() for slot in slots if slot not in available_times],
    }


def _model_forecast(snapshot, slots, now=None):
    source = ml_risk.serialize_predictions(snapshot, now=now)
    predictions = {}
    for row in source.get("hourly_scores", []):
        instant = _parse(row.get("time"))
        score = row.get("score")
        if instant in slots:
            valid = (isinstance(score, (int, float)) and not isinstance(score, bool)
                     and math.isfinite(score) and 0 <= score <= 10
                     and row.get("availability") == "available")
            predictions[instant] = {**row, "score": score if valid else None}
    hourly = [predictions.get(slot, {"time": slot.isoformat(), "score": None,
                                    "availability": "unavailable", "construction_hazards": [],
                                    "reason": source.get("reason") or "No model prediction for this hour"})
              for slot in slots]
    known = [row for row in hourly if row["score"] is not None]
    stale = any(row.get("stale") or row.get("availability") == "stale" for row in hourly)
    return {
        "assessment_type": "trained_ml", "score_range": [0, 10],
        "availability": ("available" if len(known) == len(slots) else "partial" if known
                         else "stale" if stale else "unavailable"),
        "reason": None if known else source.get("reason") or "No model predictions for the target date",
        "model_id": source.get("model_id"), "model_version": source.get("model_version"),
        "stale": stale or bool(source.get("stale", False)),
        "stale_reason": source.get("stale_reason"),
        "input_generated_at": source.get("input_generated_at"),
        "received_at": source.get("received_at"), "hourly_scores": hourly,
        "serialized_at": source.get("serialized_at"),
        "retained_predictions": source.get("retained_predictions"),
        "highest_upcoming": max(known, key=lambda row: row["score"]) if known else None,
    }


def _environment(snapshot, start, end, now=None):
    weather = snapshot.get("weather") or {}
    hourly = weather.get("hourly") or {}
    rows = []
    offset = timezone(timedelta(seconds=weather.get("utc_offset_seconds", 0)))
    for index, value in enumerate(hourly.get("time", [])):
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=offset)
        if start <= instant < end:
            row = {field: values[index] for field, values in hourly.items()
                   if field != "time" and isinstance(values, list) and index < len(values)}
            rows.append({"time": instant.isoformat(), **row})
    return {"availability": "available" if rows else "unavailable", "hourly": rows,
            "units": weather.get("hourly_units", {}), "timezone": weather.get("timezone"),
            "source_utc_offset_seconds": weather.get("utc_offset_seconds"),
            "recent_rainfall": weather.get("recent_rainfall"),
            "sources": freshness.sources(snapshot.get("sources", {}), now=now)}


def _missing_information(model, rules, assessment_freshness, river_forecast=None):
    missing = []
    if model["availability"] in ("unavailable", "stale"):
        missing.append("Trained ML predictions are unavailable for use; hazard levels below are rule-based.")
    elif model["availability"] == "partial":
        missing.append("Trained ML predictions cover only part of the next local day.")
    if model.get("stale"):
        missing.append("Retained model predictions are stale; their original timestamps are preserved.")
    if rules["availability"] != "available":
        missing.append("Environmental rule-based forecasts do not cover every hour of the next local day.")
    if assessment_freshness["stale"]:
        missing.append("The underlying assessment is stale or unavailable; generating a briefing does not refresh its sources.")
    for name, source in assessment_freshness.get("sources", {}).items():
        if source.get("status") != "available":
            missing.append(f"{name}: source data is unavailable or incomplete.")
        elif source.get("stale"):
            missing.append(f"{name}: cached source data is stale.")
    if river_forecast is not None:
        if river_forecast.get("availability") not in ("available", "partial"):
            missing.append("HEFS river-threshold probabilities are unavailable; missing probability does not mean zero.")
        elif river_forecast.get("stale"):
            missing.append("The retained HEFS river forecast is stale; its original issuance and target times are preserved.")
    return missing


def serialize_briefing(payload, now=None):
    """Reevaluate validity on reads without changing saved report/delivery history."""
    import hefs

    now = _now(now)
    result = deepcopy(payload)
    saved_freshness = result.get("freshness", {})
    snapshot = {
        "generated_at": saved_freshness.get("assessment_generated_at"),
        "sources": saved_freshness.get("source_timestamps", saved_freshness.get("sources", {})),
        "ml_assessment": result.get("ml_predictions") or {},
        "overall": {"coverage_complete": saved_freshness.get("coverage_complete", False)},
        "current_risk": {"weather_observed_at": saved_freshness.get("weather_observed_at")},
    }
    latest = freshness.assessment(snapshot, now=now)
    result["freshness"] = {**latest, "assessment_stale": latest["stale"],
                           "source_timestamps": latest["sources"],
                           "source_gaps": [name for name, source in latest["sources"].items()
                                           if source.get("status") != "available"]}
    _, _, slots = _period(date.fromisoformat(result["target_date"]), _zone(result["timezone"]))
    result["ml_predictions"] = _model_forecast(snapshot, slots, now=now)
    result["environmental_forecasts"]["sources"] = latest["sources"]
    if result.get("river_forecast"):
        result["river_forecast"] = hefs.serialize_forecast(result["river_forecast"], now=now)
    result["missing_information"] = _missing_information(
        result["ml_predictions"], result["rule_based_assessment"], latest, result.get("river_forecast"))
    return result


def _payload(site, zone_name, day, now, snapshot):
    import crew_reports
    import alerts
    import hefs

    zone = _zone(zone_name)
    start, end, slots = _period(day, zone)
    snapshot = snapshot or {}
    rules = _rule_forecast(snapshot, start, end, slots)
    model = _model_forecast(snapshot, slots, now=now)
    assessment_freshness = freshness.assessment(snapshot, now=now)
    river_forecast = hefs.saved(site["id"])
    source_gaps = [name for name, source in snapshot.get("sources", {}).items()
                   if source.get("status") != "available"]
    missing = _missing_information(model, rules, assessment_freshness, river_forecast)
    return {
        "site": {key: site[key] for key in ("id", "name", "latitude", "longitude")},
        "site_id": site["id"], "target_date": day.isoformat(), "timezone": zone_name,
        "period": {"start": start.astimezone(zone).isoformat(), "end": end.astimezone(zone).isoformat(),
                   "start_utc": start.isoformat(), "end_utc": end.isoformat(), "hours": len(slots)},
        "generated_at": now.isoformat(), "rule_based_assessment": rules,
        "ml_predictions": model, "required_safety_actions": alerts.required_actions(site["id"]),
        "active_alerts": alerts.for_site(site["id"]),
        "environmental_forecasts": _environment(snapshot, start, end, now=now),
        # River threshold probabilities retain their own target times/periods;
        # they are never copied into hourly site hazard or model scores.
        "river_forecast": river_forecast,
        "unresolved_crew_reports": crew_reports.active_reports(site["id"]),
        "freshness": {**assessment_freshness, "assessment_stale": assessment_freshness["stale"],
                      "source_timestamps": assessment_freshness["sources"], "source_gaps": source_gaps},
        "missing_information": missing,
        "delivery": {"channel": "in_app", "status": "delivered", "delivered_at": now.isoformat()},
    }


async def generate(site_id, now=None, target_date=None, trigger="manual", preview=False):
    """Generate once per site/target date and commit its notification atomically.

    Preview is read-only and always uses the latest saved assessment. Actual
    manual delivery is explicit: like scheduled delivery it consumes that
    date's once-only notification and will not be sent again at 18:00.
    """
    import notifications

    now = _now(now)
    site = database.find_site(site_id)
    if not site:
        raise LookupError("Site not found")
    zone_name = get_timezone(site_id)
    if not zone_name:
        raise ValueError("Site timezone is unknown; configure its IANA timezone first")
    zone = _zone(zone_name)
    if target_date is None:
        day = now.astimezone(zone).date() + timedelta(days=1)
    elif isinstance(target_date, datetime):
        raise ValueError("target_date must be a calendar date, not a timestamp")
    else:
        day = date.fromisoformat(target_date) if isinstance(target_date, str) else target_date
    if not isinstance(day, date):
        raise ValueError("target_date must be a calendar date")

    if preview:
        payload = _payload(site, zone_name, day, now, database.load_risk(site_id))
        payload["trigger"] = "preview"
        payload["delivery"] = {"channel": "in_app", "status": "preview", "delivered_at": None,
                               "consumes_scheduled_delivery": False}
        return {"created": False, "preview": True, "delivery_mode": "preview", "briefing": payload}

    # Do all data reads before the transaction; generation needs no provider
    # request and remains useful during an outage or with no ML configured.
    with database.connect() as connection:
        row = connection.execute("SELECT payload FROM briefings WHERE site_id=? AND target_date=?",
                                 (site_id, day.isoformat())).fetchone()
    if row:
        return {"created": False, "preview": False, "delivery_mode": "actual_delivery",
                "briefing": serialize_briefing(json.loads(row["payload"]), now=now)}
    payload = _payload(site, zone_name, day, now, database.load_risk(site_id))
    payload["trigger"] = trigger
    payload["delivery"]["consumes_scheduled_delivery"] = True
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT payload FROM briefings WHERE site_id=? AND target_date=?",
                                 (site_id, day.isoformat())).fetchone()
        if row:
            return {"created": False, "preview": False, "delivery_mode": "actual_delivery",
                    "briefing": serialize_briefing(json.loads(row["payload"]), now=now)}
        cursor = connection.execute("""INSERT INTO briefings
            (site_id, target_date, generated_at, payload) VALUES (?, ?, ?, '{}')""",
            (site_id, day.isoformat(), now.isoformat()))
        payload["id"] = cursor.lastrowid
        connection.execute("UPDATE briefings SET payload=? WHERE id=?",
                           (json.dumps(payload, allow_nan=False), payload["id"]))
        notifications.publish(site_id, "briefing", payload,
                              dedupe_key=f"briefing:{site_id}:{day.isoformat()}", connection=connection)
    return {"created": True, "preview": False, "delivery_mode": "actual_delivery", "briefing": payload}


async def tick(now=None):
    now = _now(now)
    result = {"generated": 0, "deduplicated": 0, "skipped": [], "failures": []}
    for site in database.list_sites():
        try:
            zone_name = get_timezone(site["id"])
            if not zone_name:
                result["skipped"].append({"site_id": site["id"], "reason": "timezone_unknown"})
                continue
            local_now = now.astimezone(_zone(zone_name))
            if local_now.hour < 18:
                continue
            generated = await generate(site["id"], now=now, trigger="scheduled")
            result["generated" if generated["created"] else "deduplicated"] += 1
        except Exception as error:
            # One damaged site's timezone/data cannot stop all other deliveries.
            result["failures"].append({"site_id": site["id"], "error": str(error)})
    return result


def list_briefings(site_id, limit=30, now=None):
    with database.connect() as connection:
        rows = connection.execute("SELECT payload FROM briefings WHERE site_id=? ORDER BY target_date DESC LIMIT ?",
                                  (site_id, limit)).fetchall()
    return [serialize_briefing(json.loads(row["payload"]), now=now) for row in rows]


class TimezoneInput(BaseModel):
    timezone: str = Field(min_length=1, max_length=100)


class BriefingInput(BaseModel):
    target_date: date | None = None


@router.put("/api/sites/{site_id}/timezone")
def update_timezone(site_id: int, payload: TimezoneInput):
    if not database.find_site(site_id):
        raise HTTPException(404, "Site not found")
    try:
        return set_timezone(site_id, payload.timezone)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/api/sites/{site_id}/briefings")
def get_briefings(site_id: int, limit: int = Query(default=30, ge=1, le=365)):
    if not database.find_site(site_id):
        raise HTTPException(404, "Site not found")
    rows = list_briefings(site_id, limit)
    return {"site_id": site_id, "timezone": timezone_status(site_id), "count": len(rows), "briefings": rows}


@router.post("/api/sites/{site_id}/briefings")
async def trigger_briefing(site_id: int, payload: BriefingInput | None = None):
    """Deliver once for this site/date now, consuming its scheduled delivery."""
    try:
        return await generate(site_id, target_date=payload.target_date if payload else None)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/api/sites/{site_id}/briefings/preview")
async def preview_briefing(site_id: int, payload: BriefingInput | None = None):
    """Preview saved data without storing a briefing or creating a notification."""
    try:
        return await generate(site_id, target_date=payload.target_date if payload else None, preview=True)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
