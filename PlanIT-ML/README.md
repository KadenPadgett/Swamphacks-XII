# PlanIT Control — Frozen 2025 Severe Thunderstorm Warning Model

**Research prototype only. Do not use model scores for dispatch, work suspension,
lightning all-clear, or any other safety authorization.**

## What this repository includes

- **`baseline_hgb.joblib`** — full 300-plant 2025 training experiment, chronological validation and test at those same plants.
- **`geographic_hgb.joblib`** — separately fitted geographic experiment: 209 development plants and 91 unseen plants, preserving shared-station / 5°-grid grouping. This is NOT the same fit as the baseline.
- Saved original metrics, holdout plant assignments, optional calendar-only ablation, model training scripts and validation diagnostics.
- **`dev300_hourly_training_2025.csv.gz`** — all 2,626,200 precomputed 2025 plant-hours, including the historical warning label. This is enough to rerun the training experiments without the original raw weather and warning archives.
- `dev300_plants.csv` and `dev300_nearest_station.csv` — coordinates and nearest-station assignments needed to reconstruct the geographic split.
- Optional `plant_warning_events_2025.csv` — issuance records used for event-level diagnostic counts.

## Setup (Windows PowerShell, Python 3.13)

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe predict_frozen_model.py --model geographic --rows 10
```

For best reproducibility, use the exact `scikit-learn`, `numpy`, `pandas`, and `joblib` versions originally used to serialize the model. Record versions on the **original** machine with:

```powershell
py -3.13 -m pip freeze > environment_original.txt
```

An existing `.joblib` checkpoint may not load reliably across different scikit-learn versions; only load files from a trusted source.

## How to run the existing model

```powershell
py -3.13 predict_frozen_model.py --model baseline --rows 10
py -3.13 predict_frozen_model.py --model geographic --rows 10
```

These commands read precomputed weather features from the included historical CSV and write `prediction_preview.csv`. The example script is NOT a live model-serving API. For another dataset, pass `--input new_precomputed_features.csv`; feature names and meaning must match the training pipeline exactly. The script reads saved `.joblib` bundles containing the estimator and ordered feature names.

### Training reproducibility (no downloads needed)

```powershell
py -3.13 train_dev300_baseline.py
py -3.13 train_dev300_geographic_holdout_v2.py
py -3.13 train_dev300_calendar_only.py
```

**Important:** These training commands **overwrite** output model files and metrics. To preserve the frozen checkpoint, do not run them in this repo unless you are intentionally reproducing the experiment in a copy of the folder. The calendar command is available only if its optional script was copied.

### Diagnostics without retraining

```powershell
py -3.13 diagnose_dev300_baseline.py
py -3.13 diagnose_dev300_geographic_holdout.py
```

Event-level analyses use `data/processed/plant_warning_events_2025.csv` if included. The CSV contains the source column `plant code` (with a space); the diagnostic code handles it. Without the events file, event-level outputs may be unavailable.

## What the model predicts

Probability-like score for a **new NWS Severe Thunderstorm Warning polygon covering a site in the next six hours**, NOT probability of lightning, actual storm impact, or another hazard.

Inputs: 25 numeric features comprising weather measurements, three-hour changes, station freshness/missingness, station distance, and cyclical hour/month. `prediction_time_utc` and `valid_dt` are NOT model predictors; the latter is an observation timestamp.

Historical data was constructed assuming a 60-minute METAR reporting delay and max 180-minute observation age; archived receipt times were not independently established. 2025 labels came from archived initial NWS warning issuances. Site-hour observations are correlated; the geographic holdout retains cross-boundary spatial dependence.

## What is NOT included

This repository does **not** by itself fetch live weather, select stations at new plants, compute current three-hour changes, provide an API server, or integrate rules for storms, wildfire, heat, flooding, soil, or wildlife. That code belongs to the separate PlanIT application.

If `--SkipTrainingData` was used when packaging, the pretrained models still load, but the default demonstration and training commands require the omitted CSV. You can pass your own 25-feature CSV instead.

The original raw EIA workbook, IEM archived warning shapefiles, station GeoJSON inventories, full IEM METAR caches, and exploratory data-building scripts are not required to **load and score** these frozen models or to **retrain on the supplied precomputed dataset**. They would be required to rebuild all training features and labels from original sources, a larger separate archival project.

## Evaluation summary

Geographic test, Nov–Dec 2025 (91 held-out plants): AP 0.008924, ROC-AUC 0.915374, 86 positive site-hours; the validation-selected threshold detected **0/86 positive site-hours** and generated **20 positive flags, all negative against the archived issuance labels**. Research-only ranking signal, not a reliable warning trigger. See saved metrics and diagnostics for full results.

## GitHub / data licensing

Before committing, check the size and licensing/provenance of every data file. GitHub blocks regular Git blobs larger than 100 MiB; prefer Git LFS for `.joblib`, `.csv.gz`, and large events CSV, or provide immutable release/download instructions rather than pushing raw archives. Do not commit personal contact details, private crew reports, credentials, or API tokens.
