# Risk response and ML handoff

The rule-based assessment and the teammate's trained ML hazard index are separate.
No rule level is converted to a numeric ML score. A real model is still required;
when none is connected, `ml_assessment.availability` is `unavailable` and its score
is null. The backend does not train a model or claim that the screening rules satisfy
the challenge's ML requirement.

## Temporal risk fields

`GET /api/sites/{site_id}/risk` returns:

- `current_risk`: latest environmental conditions, completed historical rainfall, and
  alerts applicable at the actual assessment time. `time` and `alerts_evaluated_at`
  record that time; `weather_observed_at` preserves the weather observation time,
  and `rainfall_as_of` records the completed rainfall window. Recent wildfire reports and wildlife sightings
  provide current context.
- `hourly_predicted_risk`: 48 separate hourly rule-based assessments, with
  offset-aware timestamps. Missing hours/measurements stay unknown.
- `highest_upcoming_risk`: the highest future rule-based level, its timestamp,
  and each hazard's peak and time. It excludes current measurements.
- `ml_assessment`: independently returned ML scores, hazards, model version,
  and target timestamps.

The legacy `overall` and `indicators` fields now alias **current risk only**.
Old v1 snapshots are invalidated, including in `GET /api/risks`, so they cannot
present tomorrow's heat as a current hazard.

Each rule-based frame is explicitly marked `assessment_type: "rule_based"`.
Future rainfall is accumulated only through the hour being assessed. It never
enters current risk or an earlier forecast hour. The preceding 24-hour forecast
window combines available historical precipitation with predicted precipitation;
any missing hour leaves that total unknown. Alert onset/end times are respected.
Wildfire spread and wildlife sightings are not forecast by the configured feeds,
so they remain current context rather than being copied into hourly predictions.

Every lightning card separates:

```json
{
  "coverage": "partial",
  "thunderstorm_forecast": {"availability": "available", "level": "low"},
  "measured_lightning_strikes": {
    "availability": "unknown", "level": "unknown", "count": null
  }
}
```

Thunderstorms or applicable alerts can still raise the screening level. Missing
strike measurements never become zero strikes or complete lightning coverage.
The forecast proxy remains separate from `lightning_countdown`. The measured
adapter implements persistent strike-distance/stop-work timing; access is pending.
See [FRONTEND_HANDOFF.md](FRONTEND_HANDOFF.md) for operational responses that work
even when this model adapter has no configured model.

## Export data to the teammate

Call **`GET /api/sites/{site_id}/ml-input`**. The JSON includes:

- `request_id`: identifies this exact input snapshot; echo it in the result.
- `schema_version`: `site-risk-features-v1`.
- `site`: ID and coordinates.
- `generated_at`: when this input snapshot was assessed.
- `weather_data`: current conditions, complete hourly forecasts, units,
  recent hourly precipitation, and recent rainfall totals.
- `source_availability`: source status and retrieval times.
- `samples`: current time followed by 48 forecast times, each with `id`, `time`,
  `time_scope`, and raw numeric `features`. `feature_units` documents their units.
- `supplemental_context`: captured, versioned river, HEFS, crew-report, and measured
  lightning context. This is not an expansion of the trained feature vector.
- `output_contract`: score range, proposed interpretation and explicit conversion
  ownership. The final target remains pending teammate agreement.

Features include gust, apparent temperature, preceding-hour/24-hour precipitation,
soil water content, weather code, nearest reported fire distance, wildlife count,
and measured strike count. Null means unknown/unavailable, not zero. Measured
strike count remains null until validated coverage exists. Missing nearby-fire
distance is also null. Do not interpret wildlife count as an injury-risk label.

The model owns its learned scoring, preprocessing, and missing-value handling.
It can use `weather_data` for additional forecast fields. The supplied rule levels
are not part of the exported sample features.

### What v1 actually contains

| Hazard/context | Existing sample features and limitations |
| --- | --- |
| Weather/heat | Gust, apparent temperature, hourly precipitation, WMO weather code; no worker exposure/injury labels |
| Rain/ground conditions | Preceding 24-hour rain and modeled soil water content; no local bearing capacity or inundation model |
| Lightning | Thunderstorm weather code; current measured count only with sufficient real coverage; every future measured count is null |
| Wildfire | Nearest reported EONET point distance, current context only; no fire-spread forecast |
| Public wildlife | Current observation count, not dangerous-species probability or a crew-report feature |
| River/HEFS and crew reports | **Supplemental only**, absent from the v1 sample feature vector; trained support not yet agreed |

These inputs do not establish that an unavailable model was trained to predict
each hazard. NWS warnings, required actions, and rule levels are not learned target
labels. The existing flat `site-risk-features-v1` and 49-sample structure are
preserved. A model that consumes supplemental data needs an agreed, versioned
feature/preprocessing contract and matching historical training data; collecting
context does not automatically make it a supported model input.

`supplemental_context.schema_version` is `site-risk-context-v1`. It includes
`captured_at`, `model_support`, saved `river`, separate `hefs`, active
`crew_reports`, and `measured_lightning`. It is captured once per assessment and
included in `request_id`; reads do not quietly replace it with newer reports or
forecasts and thereby invalidate a pending model response. The live detail's
reports/river forecast may consequently be newer than the model's captured input.
River context now carries a persisted evidence review and validity status. A raw
legacy confirmation boolean is not proof of exposure. The current detail's
`river_exposure` can reflect a withdrawal or changed location after capture;
do not reinterpret a historical model input as a newly verified association.
Historical river associations, HEFS issuances/targets/quantiles, report provenance
and resolution times, and validated strike-feed coverage must be reconstructed
**as known at the prediction time** before training on them to avoid leakage.

The existing current `measured_lightning_strike_count` counts distinct validated
records within 10 miles during `(window_start, window_end]`, a defined 30-minute
window ending at the latest covered instant no later than the sample target.
The coverage end may lag the target by at most 90 seconds. The context exposes
that exact window, radius, coverage status, and missing-data reason. Coverage must
span the whole window and be fresh. Missing/short/stale coverage leaves null;
out-of-radius, duplicate, future and invalid events cannot inflate the count.
All 48 future counts remain null. Training must use this same window and coverage
policy rather than replacing missing history with zero.

HEFS bounds are 0–1 **river-threshold** probabilities at original times/periods;
they are never converted into jobsite flooding probability or an overall hazard
score. See [HEFS_NOTES.md](HEFS_NOTES.md).

### Score interpretation still requires agreement

The API always expects a finite **0–10 score**. Under the intended likelihood
interpretation, probability **0.70 → score 7.0**. The **model service** must apply
`score = probability * 10` before submitting its result (the optional
`ml_risk.probability_to_score` helper documents/validates that operation). The
backend does not detect probabilities or automatically rescale submitted scores;
submitting `0.70` in `score` stores score 0.70. A 0–10 number alone does not prove
calibration. The teammate must still agree on the event/hazard target, horizon,
labeling, calibration, preprocessing, and feature version before a probability
interpretation or ML-based dashboard ordering can be claimed.

## Return model results

Post to **`POST /api/sites/{site_id}/ml-output`**:

```json
{
  "request_id": "COPY_FROM_ML_INPUT",
  "model_version": "planit-model-v1",
  "predictions": [
    {
      "time": "2026-10-18T12:00:00-04:00",
      "score": 8.7,
      "construction_hazards": [
        "Heat stress during outdoor work",
        "Vehicle traction loss on wet ground"
      ]
    }
  ]
}
```

This is a **format example**, not an actual model prediction. Use timestamps from
the exported samples and scores produced by the trained model. `model_id` is
optional. `model_version`, timezone-aware `time`, `score`, and
`construction_hazards` are required. Scores must already be finite numbers from
0 to 10; out-of-range scores are rejected rather than rescaled or clamped.

Send one or several predictions (up to 49). Predictions arriving separately are
merged only when their input request, model ID, and version match. Scores for a
new model version replace the previous version's result. The response preserves
the score and hazard descriptions and contains:

- `current_score` and `current`: only the prediction for the current exported time.
- `hourly_scores`: scores/hazards for their specific forecast timestamps; unscored
  hours have null scores and explicit unavailable status in serialized responses.
- `highest_upcoming`: the largest returned future ML score with its time/hazards.
- `model_version`, `availability`, `scored_samples`, and `expected_samples`.

Results are persisted in SQLite alongside the input assessment. Returning a
future score never fills `current_score`. A refreshed input invalidates the old
request ID; a delayed result for that old input returns 409. Re-export and re-score
the new input. Invalid numeric/time formats return 422; mismatched target times
or request IDs return 409. The frontend should also respect the snapshot's stale
flag and target times.

If refresh cannot obtain replacement ML predictions (including when no inference
URL is configured), previous scores are retained with their **original** target
times, model version, and `input_generated_at`. Their `ml_assessment.stale` flag
becomes true, and `latest_attempt` explains why no replacement arrived. Display
`current_score`, affected hourly `score`, and an unavailable peak are null. The
unchanged originals are available in `retained_predictions` and per-row
`original_score`; they must not be rendered as fresh current predictions.
New matching model results replace stale predictions, subject to target expiry.
The portfolio overview includes the ML stale flag separately from rule-based
assessment freshness. Retention is transactional so a submission arriving during
provider requests is read before the refreshed assessment is saved.

### Read-time freshness policy

Assessment validity lasts `max(900, 2*PLANIT_REFRESH_SECONDS + 120)` seconds after
`generated_at`. With the default 300-second refresh this is 900 seconds for both
complete and incomplete assessments. Missing sources can make a refresh due after
60 seconds; this does not expire the whole assessment after 60 seconds. Normal
background full runs wait 300 seconds after completion; failed/partial jobs retry
after at most 30 seconds. `freshness` exposes both `refresh_due_at` and `expires_at`.
Provider retrieval/observation/expiry timestamps remain separate and can be stale
even while an assessment is within its display lifetime.

ML validity is reevaluated on **every** detail, map, model-output POST response,
and briefing read. A current prediction applies only during
`[target, target + PLANIT_REFRESH_SECONDS)`; an hourly prediction describes
`[target, target + 1 hour)`. These are temporary application display windows,
not a settled model target or calibration claim. Expired targets, stale retained
inputs, or an expired input assessment have null display scores with explicit
`availability: "stale"`, `valid_until`, and original timestamps/scores. Future
rows do not become the current score. Valid future rows can still provide an
upcoming peak after the current prediction expires; top availability may be partial.
Saved briefings preserve their historical delivery payload while serializers
reevaluate displayed prediction validity and source age.

## Optional automatic inference

When your teammate hosts an inference endpoint, set `PLANIT_ML_URL` before
starting the backend. The backend POSTs the same export JSON to that URL during
risk refresh and expects the same model-result JSON above. Optional
`PLANIT_ML_TOKEN` sends a bearer token. `PLANIT_ML_MODEL_VERSION` can invalidate
cached assessments when the deployed model changes. The actual returned model
version is retained in the result.

A failed model request leaves the rule-based assessment available and reports
ML as unavailable. The export/import endpoints work without a hosted inference
service, which supports teammate development and manual handoff.

## Refresh all monitored sites

`POST /api/risks/refresh` now refreshes **all** saved sites, including frontend
creations and the demo. Optional `limit` (1–100) and `offset` support pagination;
neither filters by EIA ID. The CLI also refreshes every site:

```powershell
.\.venv\Scripts\python.exe -B scripts/refresh_risks.py
```

Regression checks:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```
# Separate METAR warning model

The experimental six-hour NWS Severe Thunderstorm Warning integration is documented
in [METAR_MODEL_HANDOFF.md](METAR_MODEL_HANDOFF.md). It uses `weather_model`, separate
from this generic 0–10 ML contract and its hourly scores.
