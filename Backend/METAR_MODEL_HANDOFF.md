# Experimental METAR warning model handoff

The target is **a new NWS Severe Thunderstorm Warning covering the site in the six hours after prediction time**. It is not overall hazard, lightning, or injury probability. The supplied frozen model package is in `../PlanIT-ML/`. Local inference uses its geographic holdout artifact by default; `PLANIT_METAR_ML_MODEL=baseline` selects the separately fitted chronological baseline. Neither model may authorize dispatch, stop-work, or lightning all-clear decisions.

## Interface and current implementation

`metar.py` sends observations from the Iowa Environmental Mesonet (IEM) ASOS archive. IEM's [download documentation](https://mesonet.agron.iastate.edu/request/download.phtml) identifies the units and missing/trace markers. Its [CGI documentation](https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?help) documents historical `sts`/`ets`, station, field, report-type parameters and 429/503 responses. Its [network metadata documentation](https://mesonet.agron.iastate.edu/geojson/network.py?help) documents station coordinates and online filtering. The observation archive includes routine and special reports, with `M` for missing and `T` for precipitation trace. IEM says these archived reports have little quality control and the archive syncs about every ten minutes.

The persisted selection is the nearest currently online US station in IEM's AZOS ASOS-class network, measured by great-circle distance, with station ID, coordinates, distance, time, and source provenance. Selection is fixed for a site until its coordinates change. The training methodology used the nearest station from a 2025 ASOS catalog with possible archive overlap; the live online-only catalog is therefore a practical availability filter, **not a historically identical station universe**. A missing report never causes a station switch.

Observation queries request an eight-hour lookback and are cached by station and UTC hour for 120 seconds; station metadata is cached for 24 hours. Requests have a 12-second timeout, at most three attempts, bounded waits on 429/503, and a shared station lock. The provider's `retrieved_at` is when this backend received the HTTP response. The 60-minute reporting delay is **assumed availability**, not a verified receipt timestamp.

The implementation selects the newest observation at or before prediction time minus the assumed 60-minute reporting delay. It allows a further 120-minute tolerance, so an eligible report is **60–180 minutes old at prediction time**. It repeats selection for prediction time minus three hours, then subtracts the earlier field from the current field. The supplied methodology confirms backward as-of matching, routine and special reports, and first-report retention at duplicate observation timestamps. Its raw data-building script is absent, so full reconstruction from the original weather archive cannot be verified.

The ordered features are:

| Order | Feature | Unit / meaning |
| --- | --- | --- |
| 1 | `distance_km` | km, selected station to site |
| 2 | `precip_trace_reported` | 0/1 for IEM `T` in `p01i` |
| 3–12 | `tmpf`, `dwpf`, `relh`, `drct`, `sknt`, `gust`, `p01i`, `alti`, `mslp`, `vsby` | °F, °F, %, degrees true north, knots, knots, inches, inches Hg, hPa/mb, miles |
| 13–17 | `has_station_report`, `observation_age_minutes`, `gust_reported`, `mslp_reported`, `alti_reported` | 0/1, minutes from prediction time, 0/1 flags |
| 18–21 | `mslp_change_3h`, `tmpf_change_3h`, `relh_change_3h`, `sknt_change_3h` | current minus earlier, in each source field's unit |
| 22–25 | `hour_sin`, `hour_cos`, `month_sin`, `month_cos` | UTC formulas: sin/cos(2π × UTC hour/24), sin/cos(2π × (UTC month−1)/12); January is 0/1 |

Missing measurements remain JSON `null`; the local model adapter converts them to numeric `NaN` in an ordered `float32` DataFrame, exactly as the supplied prediction script does. No apparent temperature, forecast values, station metadata, or general zero substitution is used. IEM `T` becomes `precip_trace_reported=1` and `p01i=0` measurable inches. The no-report flags are zero and weather fields remain missing. Distance is rounded to three decimals, as observed in the training table. No unit conversion is applied because these are IEM's native columns. Altimeter and sea-level pressure stay distinct.

## Frozen inference service

With `PLANIT_METAR_ML_URL` empty, the backend loads the supplied frozen geographic artifact locally, checks its SHA-256 against the package manifest, verifies all 25 ordered features, and calls `predict_proba` for class 1. No training or artifact overwrite occurs. The response includes the artifact variant and checksum prefix as `model_version`. Setting `PLANIT_METAR_ML_URL` instead uses the optional remote service boundary: it receives one JSON object with `schema_version=metar-warning-v1`, request ID, site/station information, prediction/outcome times, target, feature order, and features, then returns `{ "request_id": "same hash", "model_version": "configured version", "probability": 0.42 }`. The backend rejects mismatched request IDs, versions, and nonfinite/out-of-range probabilities. It multiplies probability by ten explicitly and retains the original probability. There are no copied 48-hour predictions. `PLANIT_ML_URL` and its generic result remain separate.

The artifact records scikit-learn **1.9.1**. Local loading and predictions were verified with Python 3.14.4, scikit-learn 1.9.1, NumPy 2.5.4, pandas 3.0.6, and joblib 1.6.0; those versions are pinned in `requirements.txt`. The original full environment lockfile was not supplied. A model may score a row with no eligible recent observation because the training table contains such rows; the response exposes `input_availability` and `input_reason`. Provider request failures remain unavailable and do not create a fabricated feature vector.

The active result expires after one backend refresh interval (300 seconds by default), even though its *outcome* window extends six hours. This avoids presenting an old six-hour forecast as one issued at the current time. The result is informational. Official alerts, safety rules, the lightning countdown, and stop-work decisions do not use this experimental probability for clearance.

The supplied unseen-site Nov–Dec 2025 test had 86 positive site-hours among 132,678 rows, average precision **0.008924** and ROC-AUC **0.915374**. At the threshold chosen on development validation, it detected **0 of 86** positive site-hours and produced 20 flags on negatives. These are supplied research metrics, not a deployment calibration or evidence of improved operational accuracy.

## Ubuntu / WSL startup

From `Backend/` in Ubuntu/WSL, run:

```bash
python3 -m venv .venv-wsl
source .venv-wsl/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# Edit .env for your SQLite path, origins, and optional tokens.
set -a; source .env; set +a
python scripts/serve.py
```

Keep `PlanIT-ML/` next to `Backend/` so the frozen artifact is found. Existing deployment instructions describe persistent SQLite and workers. Run tests with `PLANIT_METAR_ENABLED=0 python -m unittest discover -s tests -p 'test_*.py' -q` to keep legacy mocked-provider tests offline; dedicated METAR tests also load the frozen model. With the default `PLANIT_METAR_ENABLED=1`, full/background refresh selects stations, fetches observations, and scores the local model automatically.

Example `weather_model` result: `{ "availability": "available", "target": "new_nws_severe_thunderstorm_warning_covering_site_within_next_6_hours", "probability": 0.42, "score": 4.2, "prediction_time": "2026-10-18T00:00:00+00:00", "outcome_start": "2026-10-18T00:00:00+00:00", "outcome_end": "2026-10-18T06:00:00+00:00", "outcome_hours": 6, "model_version": "example-only" }`. This is a contract illustration, not a model prediction.

The [`docs/examples/metar-model.json`](docs/examples/metar-model.json) response fragment shows an artifact-missing or disabled-model state, explicitly marked as illustrative. With the supplied artifact and installed dependencies, local inference is the default.

Run `python scripts/probe_metar_model.py` from `Backend/` for a live IEM-to-frozen-model check using a temporary database. The [recorded live example](docs/examples/metar-model-live.json) shows one such result; its probability is specific to that site's observation time and is not a current forecast after expiration.
