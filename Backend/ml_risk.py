"""Adapter for an independently trained model; never derives scores from rules."""

import hashlib
import math
import os
from datetime import datetime, timedelta
from copy import deepcopy

import httpx
from pydantic import AwareDatetime, BaseModel, Field
import freshness
from risk_sources import utcnow

SCHEMA_VERSION = "site-risk-features-v1"
FEATURE_UNITS = {
    "gust_mph": "mph", "apparent_temperature_f": "degF",
    "preceding_hour_precipitation_in": "inch", "preceding_24h_precipitation_in": "inch",
    "soil_water_content": "m3/m3", "weather_code": "WMO code",
    "nearest_reported_fire_km": "km", "wildlife_sighting_count": "count",
    "measured_lightning_strike_count": "count",
}


class ModelPrediction(BaseModel):
    time: AwareDatetime
    score: float = Field(ge=0, le=10, strict=True)
    construction_hazards: list[str]


class ModelOutput(BaseModel):
    request_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    model_id: str | None = None
    predictions: list[ModelPrediction] = Field(min_length=1, max_length=49)


def configuration_id():
    config = os.environ.get("PLANIT_ML_URL", "") + "|" + os.environ.get("PLANIT_ML_MODEL_VERSION", "")
    return hashlib.sha256(config.encode()).hexdigest()[:16]


def samples_for(assessment):
    samples = []
    for index, frame in enumerate([assessment["current_risk"], *assessment["hourly_predicted_risk"]]):
        indicators = frame["indicators"]
        fires = indicators.get("wildfire", {}).get("evidence", {}).get("nearby_events", [])
        samples.append({
            "id": "current" if index == 0 else f"hour_{index}",
            "time": frame["time"], "time_scope": frame["time_scope"],
            "features": {
                "gust_mph": indicators["weather"]["evidence"]["gust_mph"],
                "apparent_temperature_f": indicators["extreme_heat"]["evidence"]["apparent_temperature_f"],
                "preceding_hour_precipitation_in": indicators["weather"]["evidence"]["preceding_hour_precipitation_in"],
                "preceding_24h_precipitation_in": indicators["flooding"]["evidence"]["preceding_24h_precipitation_in"],
                "soil_water_content": indicators["soil_saturation"]["evidence"]["volumetric_water_content"],
                "weather_code": indicators["lightning"]["evidence"]["weather_code"],
                "nearest_reported_fire_km": fires[0]["distance_km"] if fires else None,
                "wildlife_sighting_count": indicators.get("wildlife_sightings", {}).get("evidence", {}).get("total_results"),
                "measured_lightning_strike_count": (
                    assessment.get('ml_context', {}).get('measured_lightning', {}).get('count') if index == 0 else None),
            },
        })
    return samples


def unavailable(reason):
    return {"assessment_type": "trained_ml", "availability": "unavailable", "reason": reason,
            "model_id": None, "model_version": None, "score_range": [0, 10],
            "current_score": None, "current": None, "hourly_scores": [], "highest_upcoming": None}


def serialize_predictions(snapshot, now=None):
    """Reassess target validity on every read, even during a failed refresh.

    Current predictions apply for one refresh interval after their target; hourly
    predictions describe [target,target+1h). Neither interval implies calibration.
    Original results remain available for audit under retained_predictions.
    """
    now = now or utcnow()
    snapshot = snapshot or {}
    source = snapshot.get('ml_assessment') or unavailable('assessment_or_model_unavailable')
    raw = deepcopy(source.get('retained_predictions') or source)
    raw.pop('retained_predictions', None)
    state = freshness.assessment(snapshot, now)
    stale_input = bool(raw.get('stale')) or state['stale']

    def row_for(row, seconds, current=False):
        if row is None:
            return None
        result = deepcopy(row)
        target = freshness.timestamp(row.get('time'))
        original = row.get('original_score', row.get('score'))
        valid_number = isinstance(original, (int,float)) and not isinstance(original,bool) and math.isfinite(original) and 0 <= original <= 10
        until = target + timedelta(seconds=seconds) if target else None
        expired = target is None or now >= until or (current and now < target)
        stale = valid_number and (stale_input or expired)
        result.update(original_score=original, score=original if valid_number and not stale else None,
                      availability='stale' if stale else 'available' if valid_number else 'unavailable',
                      stale=stale, valid_until=until.isoformat() if until else None)
        if stale:
            result['stale_reason'] = 'input_assessment_stale' if stale_input else 'prediction_target_expired_or_invalid'
        return result

    current = row_for(raw.get('current'), freshness.refresh_seconds(), current=True)
    hourly = [row_for(row, 3600) for row in raw.get('hourly_scores', [])]
    valid = [row for row in hourly if row['score'] is not None]
    known_original = has_predictions(raw)
    current_score = current['score'] if current else None
    any_valid = current_score is not None or bool(valid)
    any_missing = current_score is None or any(row['score'] is None for row in hourly)
    result = {**raw, 'current': current, 'current_score': current_score, 'hourly_scores': hourly,
              'highest_upcoming': max(valid, key=lambda row: row['score']) if valid else None,
              'availability': ('partial' if any_missing else 'available') if any_valid else 'stale' if known_original else 'unavailable',
              'stale': bool(known_original and (stale_input or not any_valid)),
              'current_availability': current['availability'] if current else 'unavailable',
              'serialized_at': now.isoformat(), 'retained_predictions': raw,
              'input_generated_at': raw.get('input_generated_at') or snapshot.get('generated_at')}
    return result


def capture_context(assessment):
    """Snapshot supplemental data once, so export/import hashes remain stable."""
    import crew_reports, rivers, strike_feed
    import hefs
    site_id = assessment['site']['id']
    return {'schema_version': 'site-risk-context-v1', 'captured_at': utcnow().isoformat(),
            'model_support': 'Supplemental context only; training support has not been agreed',
            'river': rivers.saved(site_id), 'hefs': hefs.saved(site_id),
            'crew_reports': crew_reports.active_reports(site_id),
            'measured_lightning': strike_feed.measured_count(site_id, assessment['current_risk']['time'])}


def probability_to_score(probability):
    """Optional model-service conversion for the proposed likelihood contract.

    Inference providers must call their equivalent before submitting score. The
    backend never auto-detects a probability or rescales arbitrary numeric output.
    """
    if (isinstance(probability, bool) or not isinstance(probability, (int,float))
            or not math.isfinite(probability) or not 0 <= probability <= 1):
        raise ValueError('A finite probability from 0 to 1 is required')
    return probability * 10


def has_predictions(assessment):
    return bool(assessment and (
        assessment.get("current_score") is not None
        or any(row.get("score") is not None for row in assessment.get("hourly_scores", []))
    ))


def retain_on_refresh(previous_snapshot, refreshed_snapshot):
    """Keep actual model results, with their original times, until replaced."""
    incoming = refreshed_snapshot["ml_assessment"]
    if has_predictions(incoming):
        return {**incoming, "stale": False,
                "input_generated_at": refreshed_snapshot["generated_at"]}
    previous = (previous_snapshot or {}).get("ml_assessment")
    if not has_predictions(previous):
        return incoming
    return {
        **previous, "stale": True,
        "stale_reason": "inputs_refreshed_without_replacement_predictions",
        "input_generated_at": previous.get("input_generated_at") or previous_snapshot["generated_at"],
        "latest_attempt": {"time": refreshed_snapshot["generated_at"],
                           "availability": incoming.get("availability"),
                           "reason": incoming.get("reason")},
    }


def request_for(assessment):
    import json
    payload = {
        "schema_version": SCHEMA_VERSION,
        "site": {key: assessment["site"][key] for key in ("id", "latitude", "longitude")},
        "generated_at": assessment["generated_at"],
        "feature_units": FEATURE_UNITS, "samples": samples_for(assessment),
        "weather_data": assessment.get("weather"), "source_availability": assessment.get("sources", {}),
        "supplemental_context": assessment.get('ml_context', {
            'schema_version': 'site-risk-context-v1', 'availability': 'unavailable',
            'reason': 'This saved assessment predates context capture'}),
        "output_contract": {'score_range': [0,10], 'target_definition_status': 'pending_teammate_agreement',
                            'intended_interpretation': 'likelihood_scaled_by_10',
                            'conversion_location': 'model_service_before_submission', 'probability_example': {'probability': 0.70, 'score': 7.0}},
    }
    # Reject delayed results after any input snapshot has changed.
    payload["request_id"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return payload


def merge_predictions(previous, incoming):
    """Application boundary for partial outputs from the teammate's final model."""
    if not previous or any(previous.get(key) != incoming.get(key)
                           for key in ("request_id", "model_id", "model_version")):
        return incoming
    merged = {**incoming}
    merged["current"] = incoming.get("current") or previous.get("current")
    merged["current_score"] = merged["current"]["score"] if merged["current"] else None
    old = {row["time"]: row for row in previous.get("hourly_scores", []) if row.get("score") is not None}
    merged["hourly_scores"] = [old.get(row["time"], row) if row.get("score") is None else row
                               for row in incoming["hourly_scores"]]
    known = [row for row in merged["hourly_scores"] if row.get("score") is not None]
    merged["highest_upcoming"] = max(known, key=lambda row: row["score"]) if known else None
    merged["scored_samples"] = len(known) + int(merged["current"] is not None)
    merged["availability"] = "available" if merged["scored_samples"] == merged["expected_samples"] else "partial"
    return merged


def parse_prediction(payload, request):
    if payload.get("request_id") != request["request_id"]:
        raise ValueError("Model result does not match the exported input snapshot")
    if not isinstance(payload.get("model_version"), str) or not payload["model_version"].strip():
        raise ValueError("Missing model version")
    samples = request["samples"]
    times = {datetime.fromisoformat(sample["time"]): sample for sample in samples}
    predictions = {}
    if not payload["predictions"]:
        raise ValueError("No predictions supplied")
    for row in payload["predictions"]:
        score = row["score"]
        instant = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
        if instant.tzinfo is None or instant not in times:
            raise ValueError("Prediction time must match an exported sample and include a UTC offset")
        if instant in predictions or not isinstance(score, (int, float)) or isinstance(score, bool):
            raise ValueError("Invalid or duplicate model score")
        if not math.isfinite(score) or not 0 <= score <= 10:
            raise ValueError("Model score must already be in the range 0 to 10")
        hazards = row["construction_hazards"]
        if not isinstance(hazards, list) or any(not isinstance(item, str) for item in hazards):
            raise ValueError("Construction hazards must be a list of descriptions")
        predictions[instant] = {"time": times[instant]["time"], "score": score,
                                "construction_hazards": hazards, "availability": "available"}
    current = predictions.get(datetime.fromisoformat(samples[0]["time"]))
    hourly = [predictions.get(datetime.fromisoformat(sample["time"]), {
        "time": sample["time"], "score": None, "construction_hazards": [], "availability": "unknown",
    }) for sample in samples[1:]]
    known_future = [item for item in hourly if item["score"] is not None]
    return {
        "assessment_type": "trained_ml", "request_id": request["request_id"],
        "availability": "available" if len(predictions) == len(samples) else "partial",
        "model_id": payload.get("model_id"), "model_version": payload["model_version"],
        "score_range": [0, 10], "current_score": current["score"] if current else None,
        "current": current, "hourly_scores": hourly,
        "highest_upcoming": max(known_future, key=lambda item: item["score"]) if known_future else None,
        "scored_samples": len(predictions), "expected_samples": len(samples),
    }


async def predict(assessment):
    endpoint = os.environ.get("PLANIT_ML_URL")
    if not endpoint:
        return unavailable("trained_model_not_configured")
    payload = request_for(assessment)
    headers = {}
    if os.environ.get("PLANIT_ML_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["PLANIT_ML_TOKEN"]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(endpoint, json=payload, headers=headers)
            response.raise_for_status()
            return parse_prediction(response.json(), payload)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return unavailable("model_request_failed_or_invalid_output")
