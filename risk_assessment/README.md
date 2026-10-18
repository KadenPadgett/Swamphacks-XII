# PlanIT METAR — final-model integration handoff

This package runs the final `metar_2022_2024_final_hgb.joblib` model for **arbitrary latitude/longitude within the US/territory IEM ASOS station network**, without any fixed 300-plant station mapping.

## Files

- `planit_metar.py` — all station discovery, IEM API retrieval, original feature engineering, and inference in one module.
- `metar_2022_2024_final_hgb.joblib` — final 2022–2024 trained model bundle.
- `README.md` — this guide.

## Run

Install `numpy`, `pandas`, `joblib`, and a compatible `scikit-learn`. Use a scikit-learn version compatible with the final model artifact; the earlier handoff’s version claim has not been verified for the final artifact. Only load trusted joblib files.

```powershell
py -3.13 -m pip install numpy pandas joblib scikit-learn
py -3.13 planit_metar.py 29.65 -82.33
```

This needs internet access to `mesonet.agron.iastate.edu`. On first use the script downloads IEM ASOS catalogs for US states and territories concurrently; the catalog is cached in memory until backend restart. A failed catalog request fails closed rather than selecting from a partial catalog.

## Backend

```python
from planit_metar import predict
result = predict(latitude=29.65, longitude=-82.33)
```

The synchronous function performs blocking HTTP requests; call from a worker or synchronous backend route. It returns a JSON-serializable dictionary with `status`, `experimental_score` (if available), six-hour warning target, UTC prediction and observation times, station identity and distance, and explicit `calibrated: false` / `official_nws_warning: false`. If no eligible recent METAR report exists, status is `unavailable`. Catch network errors and return unavailable/503, never zero risk.

Example FastAPI integration:

```python
from fastapi import APIRouter, HTTPException
from planit_metar import predict
router = APIRouter()

@router.get('/api/ml/metar')
def metar_score(lat: float, lon: float):
    try:
        return predict(lat, lon)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=503, detail='METAR screening unavailable')
```

## Frontend

Fetch `/api/ml/metar?lat=...&lon=...` when the user creates/selects a plant. Label the score **experimental six-hour severe-thunderstorm-warning screening**, not a calibrated probability. Display station/time metadata and a clear unavailable state. Official NWS alerts and stop-work rules remain separate and authoritative.

## Provenance and exact feature processing

This module consolidates the original `build_site_station_table.py` (IEM catalog, haversine nearest-station lookup), `audit_historical_station_quality.py` (IEM ASOS request and raw fields), `build_dev300_v2.py` (`weather_at_snapshots`), and `predict_frozen_model.py` (bundle and scoring contract).

Original IEM fields: `tmpf`, `dwpf`, `relh`, `drct`, `sknt`, `gust`, `p01i`, `alti`, `mslp`, `vsby`; UTC reports, IEM report types 3 and 4, missing `M`, trace precipitation `T`. Processing deduplicates observation times, converts trace precipitation to zero plus a flag, assumes **60-minute publication delay**, accepts observations at most **180 minutes old**, and aligns the latest eligible report to each hourly snapshot. It adds reporting/missingness flags, 3-hour changes in pressure/temperature/humidity/wind, UTC hour/month cyclic encodings, and nearest-station distance. The saved model bundle supplies the **ordered 25-feature list**. Recent observation history is required for the 3-hour differences; one weather report is insufficient.

`predict()` uses the latest completed UTC hour by default, or accepts a timezone-aware `prediction_time_utc`. It refuses to use observations from the future.

## Geographic scope

No plant list is required. Any valid coordinates can be submitted, but **scoring is supported only where a US/territory IEM ASOS station is within 100 km**. Remote/global points without a nearby station return an error. This is a distance filter, **not** proof that the nearest station represents the local site. The original nearest-station policy is preserved; it does not silently switch stations based on reporting availability.

## Upgrade later

You can replace `metar_2022_2024_final_hgb.joblib` with a new trusted bundle containing `model` and `features` if its input names, order, units, preprocessing and target semantics remain identical. Restart the backend after replacement. If the feature contract changes, update the module as well.

## Limits and verification

Research-only model: **not calibrated, not operationally validated, and not an official NWS warning**. The historical 60-minute report delay was assumed rather than verified. The earlier model passed a synthetic 25-feature inference smoke test; the final 2022–2024 model has not been smoke-tested with this handoff. Live API behavior and end-to-end web-app integration have **not** been tested here. For production, add HTTP timeouts, request caching, concurrency controls and monitoring.

## Final artifact deployment check

The uploaded artifact is in the same directory as `planit_metar.py`. It was inspected locally and is a trusted joblib dictionary containing a `HistGradientBoostingClassifier`, `model`, `features`, two classes (`0`, `1`), and the expected ordered 25-feature list. The module's live IEM/network inference has not been end-to-end validated here.
