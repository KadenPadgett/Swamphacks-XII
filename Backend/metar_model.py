"""Separate inference boundary for a six-hour NWS Severe Thunderstorm Warning."""

import asyncio
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path

import httpx
import freshness

from risk_sources import parse_time, utcnow

TARGET = "new_nws_severe_thunderstorm_warning_covering_site_within_next_6_hours"
MODEL_ROOT = Path(__file__).resolve().parent.parent / 'PlanIT-ML' / 'data' / 'processed' / 'development_300_2025'
ARTIFACTS = {
    'geographic': ('model_geographic_holdout/geographic_hgb.joblib',
                   '44ea5e7ccd840e4476f22f48347fb1ee44d5d24913358f411bb67d6728648654'),
    'baseline': ('model_baseline/baseline_hgb.joblib',
                 'd789c0af61492a49ad84b0fd1ba07ffe3bb3e7d2f5d60bee48db472ccff22406'),
}


def model_choice():
    return os.environ.get('PLANIT_METAR_ML_MODEL', 'geographic').strip().lower()


def artifact_path(choice=None):
    choice = choice or model_choice()
    if choice not in ARTIFACTS:
        raise ValueError('Unknown frozen METAR model selection')
    return Path(os.environ.get('PLANIT_METAR_ML_ARTIFACT', MODEL_ROOT / ARTIFACTS[choice][0]))


@lru_cache(maxsize=4)
def _load_artifact(path, mtime_ns, size, expected_hash):
    """Verify the supplied frozen file before invoking joblib's pickle loader."""
    import joblib
    import metar
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if digest != expected_hash:
        raise ValueError('Frozen model checksum mismatch')
    bundle = joblib.load(path)
    if (not isinstance(bundle, dict) or list(bundle.get('features', [])) != list(metar.FEATURE_ORDER)
            or not hasattr(bundle.get('model'), 'predict_proba')):
        raise ValueError('Frozen model feature contract mismatch')
    return bundle


def local_prediction(request):
    """Score one current METAR row exactly as the supplied prediction script does."""
    import numpy as np
    import pandas as pd
    import metar
    choice = model_choice()
    path = artifact_path(choice)
    stat = path.stat()
    bundle = _load_artifact(str(path), stat.st_mtime_ns, stat.st_size, ARTIFACTS[choice][1])
    if request['feature_order'] != list(bundle['features']):
        raise ValueError('Input feature order does not match frozen artifact')
    row = pd.DataFrame([request['features']], columns=bundle['features'])
    row = row.apply(pd.to_numeric, errors='coerce').astype('float32')
    if list(row.columns) != list(metar.FEATURE_ORDER):
        raise ValueError('Input columns do not match METAR contract')
    classes = list(bundle['model'].classes_)
    if 1 not in classes:
        raise ValueError('Frozen model has no positive warning class')
    probability = float(bundle['model'].predict_proba(row)[0, classes.index(1)])
    if not np.isfinite(probability):
        raise ValueError('Frozen model returned a nonfinite probability')
    return {'request_id': request['request_id'], 'model_source': 'local_frozen_artifact',
            'model_version': f'2025-{choice}-hgb-sha256:{ARTIFACTS[choice][1][:12]}',
            'probability': probability}


def configuration_id():
    try:
        artifact = artifact_path()
        stat = artifact.stat()
        artifact_state = f'{artifact}:{stat.st_mtime_ns}:{stat.st_size}'
    except (ValueError, OSError):
        artifact_state = 'artifact_missing_or_invalid'
    config = '|'.join((os.environ.get('PLANIT_METAR_ML_URL', ''),
                       os.environ.get('PLANIT_METAR_ML_MODEL_VERSION', ''),
                       model_choice(), os.environ.get('PLANIT_METAR_ML_ARTIFACT', ''),
                       ARTIFACTS.get(model_choice(), ('', ''))[1], artifact_state))
    return hashlib.sha256(config.encode()).hexdigest()[:16]


def request_for(assessment, metar):
    issued = parse_time(assessment['generated_at']).astimezone(timezone.utc)
    body = {"schema_version": "metar-warning-v1", "site_id": assessment['site']['id'],
            "prediction_time": issued.isoformat(), "outcome_start": issued.isoformat(),
            "outcome_end": (issued + timedelta(hours=6)).isoformat(),
            "target": TARGET, "station": metar['station'],
            "feature_order": metar['feature_order'], "features": metar['features'],
            "current_observed_at": metar['current_observed_at'],
            "prior_observed_at": metar['prior_observed_at'],
            "availability_assumption": metar['availability_assumption'],
            "input_availability": metar['availability'],
            "input_reason": metar.get('reason'),
            "alignment_status": metar.get('alignment_status', 'methodology_aligned_raw_builder_unavailable')}
    body['request_id'] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return body


def unavailable(reason, metar=None, prediction_time=None):
    instant = parse_time(prediction_time).astimezone(timezone.utc) if prediction_time else None
    return {"availability": "unavailable", "reason": reason, "target": TARGET,
            "model_version": None, "probability": None, "score": None,
            "prediction_time": instant.isoformat() if instant else None,
            "outcome_start": instant.isoformat() if instant else None,
            "outcome_end": (instant + timedelta(hours=6)).isoformat() if instant else None,
            "outcome_hours": 6, "station": (metar or {}).get('station')}


def parse_output(payload, request):
    if payload.get('request_id') != request['request_id']:
        raise ValueError('Model response does not match the input snapshot')
    version = payload.get('model_version')
    expected = os.environ.get('PLANIT_METAR_ML_MODEL_VERSION')
    if not isinstance(version, str) or not version or (expected and version != expected):
        raise ValueError('Missing or unexpected model version')
    probability = payload.get('probability')
    if (isinstance(probability, bool) or not isinstance(probability, (int, float))
            or not math.isfinite(probability) or not 0 <= probability <= 1):
        raise ValueError('Expected a finite probability between zero and one')
    return {"availability": "available", "target": TARGET,
            "request_id": request['request_id'], "model_version": version,
            "model_source": payload.get('model_source', 'remote_service'),
            "probability": probability, "score": probability * 10,
            "score_definition": "10 times the six-hour warning probability",
            "prediction_time": request['prediction_time'],
            "outcome_start": request['outcome_start'], "outcome_end": request['outcome_end'],
            "outcome_hours": 6, "station": request['station'],
            "input_availability": request['input_availability'],
            "input_reason": request['input_reason'],
            "valid_until": min(parse_time(request['prediction_time']) + timedelta(seconds=freshness.refresh_seconds()),
                               parse_time(request['outcome_end'])).isoformat(),
            "current_observed_at": request['current_observed_at'],
            "prior_observed_at": request['prior_observed_at'],
            "received_at": utcnow().isoformat()}


async def predict(assessment, metar):
    if metar['availability'] not in ('available', 'stale') or 'features' not in metar:
        return unavailable('metar_' + str(metar.get('reason') or metar['availability']), metar,
                           assessment['generated_at'])
    endpoint = os.environ.get('PLANIT_METAR_ML_URL')
    request = request_for(assessment, metar)
    if not endpoint:
        try:
            payload = await asyncio.to_thread(local_prediction, request)
            return parse_output(payload, request)
        except (ImportError, ModuleNotFoundError):
            return unavailable('model_dependencies_missing', metar, assessment['generated_at'])
        except FileNotFoundError:
            return unavailable('frozen_model_artifact_missing', metar, assessment['generated_at'])
        except Exception:
            return unavailable('frozen_model_invalid_or_inference_failed', metar, assessment['generated_at'])
    headers = {}
    if os.environ.get('PLANIT_METAR_ML_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['PLANIT_METAR_ML_TOKEN']
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(endpoint, json=request, headers=headers)
            response.raise_for_status()
        return parse_output(response.json(), request)
    except Exception:
        return unavailable('model_request_failed_or_invalid_output', metar, assessment['generated_at'])


def serialize(result, now=None):
    if result is None:
        return {**unavailable('assessment_pending'), 'availability': 'pending'}
    result = dict(result)
    if result.get('availability') == 'available':
        now = now or utcnow()
        start = parse_time(result['prediction_time'])
        end = parse_time(result['valid_until'])
        if now < start or now >= end:
            result.update(availability='stale', reason='prediction_expired',
                          original_probability=result['probability'], original_score=result['score'],
                          probability=None, score=None)
    return result
