# PlanIT Control · 2025 Data Selection and Preparation Methodology

**Project:** PlanIT Control · Sperry Tech  
**Dataset:** 300-plant development prototype, United States, calendar year 2025  
**Documentation status:** Based on the project scripts inspected and the user's reported audit/build outputs, not on independent re-execution against the user's local data.  
**Purpose:** Research prototype for predicting whether a **new NWS severe thunderstorm warning polygon** will cover a plant in the next six hours. This is **not** an operational crew safety decision engine.

## 1. Executive summary and dataset lineage

The current artifact is `data/processed/development_300_2025/dev300_hourly_training_2025.csv.gz`, constructed by `build_dev300_v2.py`. It combines (1) EIA power-plant coordinates, (2) Iowa Environmental Mesonet (IEM) severe thunderstorm warning polygon/plant matches, and (3) IEM ASOS/METAR station observations. Plants were chosen *without reference to warning outcomes*. A single nearest station is assigned to each selected plant as a **geographic proxy**, with a temporal as-of join to prevent using observations dated after prediction time.

**Observed build outcome, as reported by the user:**

| Property | Value |
|---|---:|
| Valid EIA plant locations in geographic audit | 17,321 |
| Candidate ASOS stations in catalog | 2,937 |
| Catalog stations with possible 2025 archive overlap | 2,661 |
| Main geographically diverse pilot pool | 2,000 plants |
| Proposed additional remote-site pool | 150 plants |
| Final fixed development sample | **300 plants** |
| Distinct nearest stations for those plants | **268** |
| State labels represented | **49** |
| Plants ≥50 km from nearest station | **23** |
| Plants from proposed remote supplement | **19** |
| Stations cached for development sample | **268/268** |
| Plants assembled | **300/300** |
| Hourly rows per plant | **8,754** |
| Total labeled site-hours | **2,626,200** |
| Positive site-hour labels | **6,715** |
| Positive proportion | **0.256%** |
| Site-hours with an eligible station report | **2,464,975 (93.86%)** |
| Sites unavailable at assembly | **0** |

**Important interpretation:** 6,715 positive *site-hour labels* are not 6,715 independent severe storms or distinct warning polygons. One polygon may affect several plants, and a single warning may produce multiple positive preceding hours.

## 2. Source data and provenance

### 2.1 EIA generator/plant spreadsheet

- Local file: `data/raw/eia/plant_data_2025.xlsx`.
- The working reader uses the **second Excel row for headers** (`header=1`).
- Relevant columns: `plant code`, `plant name`, `latitude`, `longitude`; geography/state fields where present.
- Retain sites with usable plant identifiers and geographic coordinates. The audited source contained **17,321 valid, unique-coordinated plant locations**.
- EIA coordinates are location proxies and may require checking for multi-site facilities, mapping errors, and exact plant operating location.

### 2.2 Severe thunderstorm warning polygons

- Local files: `data/raw/iem/warnings_2025.shp` plus shapefile sidecars; derived `data/processed/plant_warning_matches_2025.csv` and `data/processed/plant_warning_events_2025.csv`.
- The 2025 IEM warning shapefile was reported to contain **489,928** total records, including **90,817** with `PHENOM='SV'` and `SIG='W'`.
- Retain **severe thunderstorm warnings** (`PHENOM='SV'`, `SIG='W'`) and **actual polygon geometries** (`GTYPE='P'`), not generic county-based geometry (`GTYPE='C'`).
- Match point locations for EIA plants against warning polygons using a spatial relationship suitable for point-in-polygon matching. The prepared event table records which sites a polygon covers.
- `INIT_ISS` is the event's **initial issuance timestamp** in UTC. Do not confuse a warning's expiration, update, or later appearance in an archive with a new initial issuance.
- The earlier matching output was reported as **97,595 plant-warning matches** across **14,219** plants, deduplicated on `(plant code, WFO, ETN, INIT_ISS)`. These are preprocessing audit counts, not training positives.
- The 300-plant build parses initial issuance timestamps from `plant_warning_events_2025.csv` and retains 2025 events.
- **Not yet certified:** historical archive completeness, polygon source/version integrity, or whether every timestamp represents availability to a production feed in real time.

### 2.3 Weather stations and historical observations

- Catalog: `data/raw/iem/station_catalog.csv`, assembled from IEM state ASOS network metadata/GeoJSON.
- Catalog contained **2,937** records. **276** were excluded where archive metadata positively indicated **no overlap** with 2025, leaving **2,661** possible-overlap stations. Another **27** stations were known to begin archives during 2025.
- Archive start/end metadata establishes **potential period overlap only**; it does not establish actual hourly data availability.
- Station selection initially uses **great-circle/haversine distance** from each plant to catalog station coordinates. It does not account for terrain, elevation, barriers, road distance, local storm variability, or instrumentation differences.
- The 2025 station quality collector downloads from the IEM ASOS archive, requesting routine and special reports and UTC timestamps. Cached records are stored as compressed CSVs under `data/raw/weather/iem_station_cache_2025/`. Each distinct `(network, station_id)` is downloaded once and reused across plants.
- Requested meteorological fields: `tmpf`, `dwpf`, `relh`, `drct`, `sknt`, `gust`, `p01i`, `alti`, `mslp`, `vsby`.
- Missing values returned as `M` are coerced to missing numeric values (`NaN`). Precipitation trace `T` is represented numerically as zero **only for feature availability**, with `precip_trace_reported=1` to preserve its distinct meaning in the derived dataset. Raw cached responses retain the original trace marker.
- Historical METAR archives can incorporate later corrections, and observation timestamps are not verified historical publication timestamps.

## 3. Selection stages: from 17,321 plants to 300

### Stage A — 2,000-plant broad pilot

- A seeded, outcome-blind sample of EIA plants: nominally **75% random** and **25% geographically spread** using approximately 5° latitude/longitude grid cells.
- The known QA site **Golden Hills Solar**, EIA plant code `61315`, is included as an anchor.
- Warning outcomes are **not** selection criteria. In particular, do not require a site to have experienced a 2025 warning; doing that would bias class prevalence and the population represented.
- Geographic plant/station join produces `selected_plants_2025.csv`, `plant_station_candidates_2025.csv`, and `plant_station_nearest_2025.csv`.

### Stage B — all-plants geographic station audit

- `audit_station_coverage.py` compares all 17,321 plants against the 2,661 possible-overlap catalog stations, with up to three nearest station candidates each.
- Observed nearest-station distance quantiles: **p10 4.5 km, median 14.8 km, p90 35.1 km, p95 44.4 km, p99 63.5 km**.
- **549 plants** were at least 50 km from their nearest catalog station; **3** were at least 100 km.
- A proposed geographically varied supplement of **150 previously unselected plants** with nearest station ≥50 km was generated. It is a purposeful remote-site oversample and is **not population-representative** by itself.
- Key audit files: `data/processed/station_audit_2025/all_plants_station_audit.csv`, `all_plant_station_top3.csv`, `proposed_remote_supplement.csv`, `audit_summary.txt`.

### Stage C — fixed 300-plant development sample

- The `build_dev300_v2.py` selector draws from the **2,000-plant pilot plus the 150-site remote supplement**, not directly from all 17,321 plants.
- Uses default seed `42`; nominally **75% seeded random sample and 25% grid-spread sample** from this combined pool. Geographic selection cycles through 5° latitude/longitude grid cells.
- Forces inclusion of Golden Hills plant `61315` as a QA anchor if in the pool.
- Saves `dev300_plants.csv`, `dev300_nearest_station.csv`, and `selection_metadata.json`; subsequent runs **reuse the fixed saved selection**, rather than resampling.
- Uses the **geographically nearest eligible catalog station** for each plant, even if that station later has missing observations. No automatic second- or third-choice station fallback is performed in this 300-plant dataset.
- Observed results: **300 plants**, **268 distinct nearest stations**, **23** plants at least 50 km from nearest station, **19** drawn from the deliberate remote supplement, **49** state labels.
- The final sample **oversamples some remote sites** and may cover geographic areas unequally. Do not interpret its unweighted prevalence as a national rate.

## 4. Station quality audits and what their results mean

- A first 18-plant / 35-distinct-station quality test fetched 2025 ASOS observations for two nearby candidates per selected plant.
- Audit evaluated **8,760 hourly timestamps** per full calendar year (including the final six hours, unlike the training rows).
- Observed median station hourly report coverage: **98.94%**; median temperature availability **98.58%**; median `mslp` availability **95.61%**.
- Original provisional screening requirements: report coverage ≥95%, temperature availability ≥90%, `mslp` availability ≥85%, and every month report coverage ≥90%. **15/35** stations passed *all* criteria.
- Failure diagnostic: 6 failed overall report coverage, 5 temperature, 15 `mslp`, and 13 monthly coverage; reasons overlap. Several otherwise good stations reported no `mslp` at all.
- Recommended reinterpretation: keep station coverage and **feature-specific missingness** separate; do not reject an otherwise useful station solely for absent `mslp`. Track `alti` separately because it is a different pressure quantity, not an interchangeable value.
- The later 300-plant collection reused prior station caches and downloaded missing nearest stations only. Final count: **268/268 stations cached** and **zero site assembly failures**.
- Neither the quality audit nor nearest-station distance certifies **meteorological representativeness** at the plant location.

## 5. Point-in-time weather matching

Implemented in `build_dev300_v2.py`, function `weather_at_snapshots`:

1. Parse each METAR `valid` observation time as UTC; retain observations in `[2025-01-01, 2026-01-01)`.
2. Sort by timestamp and keep the first report for any duplicate identical `valid_dt` within a station. This is a deterministic but potentially information-losing deduplication policy (e.g., corrections/special reports at the same timestamp).
3. Convert weather measurements to numeric, preserving missing values.
4. Define **assumed availability time** = `valid_dt + 60 minutes`.
5. For each hourly prediction timestamp `t`, backward `merge_asof` to the latest assumed-available report with tolerance **120 minutes in availability-time space**, equivalent to a maximum observation age of **180 minutes** (given the 60-minute lag).
6. If no eligible observation exists, retain the hourly row, set `has_station_report=0`, and keep its weather predictors missing.
7. Record `observation_age_minutes = t - valid_dt` where a report exists.

**Critical caveat:** The 60-minute lag is a conservative modeling assumption, **not** a verified reconstruction of real report dissemination latency. Retrospective corrections are still possible. Do not claim a fully point-in-time authenticated real-time backtest.

The build reports **2,464,975 / 2,626,200 site-hour rows (93.86%)** with an eligible station report. That percentage is not the percentage with every weather feature available.

## 6. Weather predictors and transformations

### Raw numeric weather fields

| Column | Interpretation / unit |
|---|---|
| `tmpf` | Air temperature, °F |
| `dwpf` | Dew point, °F |
| `relh` | Relative humidity, % |
| `drct` | Wind direction, degrees |
| `sknt` | Wind speed, knots |
| `gust` | Reported gust, knots, often absent when no gust was reported |
| `p01i` | One-hour precipitation, inches; trace is numerically 0 plus separate flag |
| `alti` | Altimeter setting, inches Hg |
| `mslp` | Mean sea-level pressure, hPa / millibars |
| `vsby` | Visibility, miles |

### Derived numeric/indicator features

- `precip_trace_reported`: whether the matched observation originally reported precipitation trace `T`; set to 0 for unmatched hours as a current implementation choice.
- `has_station_report`: 1 when a station report met the as-of criteria; otherwise 0.
- `observation_age_minutes`: age of the matched observation at prediction time.
- `gust_reported`, `mslp_reported`, `alti_reported`: binary indicators for corresponding numeric field present in a matched report.
- `mslp_change_3h`, `tmpf_change_3h`, `relh_change_3h`, `sknt_change_3h`: **current hourly aligned value minus value three hourly rows earlier**; if either is unavailable, the result is missing. These are differences of available snapshots, not necessarily exactly-three-hour-apart station measurements.
- `hour_sin`, `hour_cos`: cyclic UTC hour-of-day representation.
- `month_sin`, `month_cos`: cyclic month-of-year representation.
- `distance_km`: geographic distance to chosen station; should be treated as a proxy-quality and fairness/stratification variable, not evidence the station measurements equal plant weather.

**Observed CSV schema, in order:**

```text
plant_code, plant_name, plant_state, station_network, station_id,
distance_km, sampling_source, prediction_time_utc, valid_dt,
precip_trace_reported, tmpf, dwpf, relh, drct, sknt, gust,
p01i, alti, mslp, vsby, has_station_report,
observation_age_minutes, gust_reported, mslp_reported,
alti_reported, mslp_change_3h, tmpf_change_3h,
relh_change_3h, sknt_change_3h, hour_sin, hour_cos,
month_sin, month_cos, label_new_sv_warning_next_6h
```

`plant_code`, station IDs, names, state, timestamps, `valid_dt` and `sampling_source` are retained for identity, joins, auditing or split construction; they should **not be automatically used as predictive numeric features**. Missing values remain missing in the CSV. The first reported row (AK, station PABA, 2025-01-01 00:00 UTC) has no eligible report, so weather features are missing and `has_station_report=0`; this is consistent with the availability window.

## 7. Target / label construction

**Target:** `label_new_sv_warning_next_6h`.

At a plant and hourly UTC prediction time `t`:

\[
y(t) = 1\;\text{if any new qualifying polygon initially issued at time }s\text{ covers that plant and }t < s \le t+6\text{ hours};\;\text{else }0.
\]

- Qualifying warning events come from the processed EIA-plant / actual severe-thunderstorm-warning polygon matches (`SV`, `W`, polygon `P`).
- Initial issue timestamp source is **`INIT_ISS`** in `plant_warning_events_2025.csv`.
- The code deduplicates per-plant event timestamps and uses sorted timestamps/searchsorted to detect the presence of at least one event in the forward six-hour interval.
- It predicts **future warning issuance covering the site**, not lightning strikes, gust speed, severe weather occurrence independently of NWS warnings, or an overall urgency score.
- Hourly prediction times range from **2025-01-01 00:00 UTC through 2025-12-31 17:00 UTC**, inclusive, yielding **8,754** prediction rows/plant. Last six hours are omitted because the 2025-only event archive cannot resolve the forward window into 2026.
- Rows with no matched warning issuance in the source are labeled 0 **under the assumption that the source event archive is sufficiently complete**; archive completeness has not been independently certified.
- Observed positives **6,715 / 2,626,200 = 0.256%**.

## 8. Scope and explicit exclusions

The current dataset does **not** train a second urgency model. It also does not predict general thunderstorm formation or issue authoritative weather warnings. Live urgent alerts (e.g., flash flood, wildfire-related alerts) are intended to remain a **separate authoritative-alert rules layer**. Lightning stop-work logic (actual strike within 10 miles; 30-minute all-clear timer restarting after each new strike) is **outside this supervised target** and must use a suitable, verified real-time lightning source if implemented.

## 9. Validation and leakage precautions for subsequent ML

1. **Never random-split hourly rows.** Adjacent rows for the same plant and event share the six-hour future label window; warnings can also affect several sites. Prefer time-blocked evaluation, with a **≥6-hour embargo** at each split boundary.
2. Evaluate a **separate geographic/plant-held-out test**; avoid treating train and test plants within the same warning polygon as independent evidence.
3. Track **number of distinct warning events/episodes** per split as well as positive site-hours, especially when late-year storms are scarce.
4. Compare against an appropriate low-prevalence baseline, and report **average precision, precision/recall at explicit thresholds, calibration, and results by region and station distance**. Raw accuracy is misleading at 0.256% prevalence.
5. Determine whether an hourly *warning already active* at prediction time should be excluded, modeled explicitly, or evaluated separately. The present label asks only about **new issuances**, regardless of current active warning status.
6. Resist overclaiming causal predictive skill from pressure or weather trends until validated by geographically and temporally separated tests.
7. For eventual production, verify archive completeness, weather timestamp availability, clock/UTC handling, missing-data behavior, forecast input provenance, and operational failure modes.
8. Expand beyond 2025 for defensible season-to-season assessment. HRRR archived forecasts may be tested later, but only using forecast cycles that were available at the corresponding prediction times.

## 10. Reproducibility and exact local file map

Run commands from the Windows `risk_assessment` project folder using `py -3.13`.

| Script or file | Role |
|---|---|
| `match_eia.py` | Spatial match of EIA plant points to polygon warning geometries |
| `prepare_warning_events.py` | Prepare warning issuance events and deduplication |
| `build_training_labels.py` | Earlier 50-plant/hourly label prototype |
| `merge_weather_labels.py`, `build_weather_features.py` | Earlier Golden Hills single-site weather/label prototypes |
| `build_site_station_table.py` | Create geographically varied 2,000-plant pilot and nearest station candidates |
| `audit_station_coverage.py` | All-plant station-distance audit and proposed 150-site remote supplement |
| `audit_historical_station_quality.py` | Retrieve/cache IEM station-years and assess actual observation completeness |
| `build_dev300_v2.py` | Fixed 300-plant selection, cached station collection, hourly weather/label assembly |
| `data/processed/development_300_2025/dev300_plants.csv` | Frozen development sample |
| `data/processed/development_300_2025/dev300_nearest_station.csv` | Selected nearest-station mapping |
| `data/processed/development_300_2025/selection_metadata.json` | Selection seed, anchor, selection policy, creation time |
| `data/processed/development_300_2025/dev300_station_metrics.csv` | Station quality/collection assessment |
| `data/processed/development_300_2025/dev300_hourly_training_2025.csv.gz` | Final prototype modeling table |
| `data/processed/development_300_2025/dev300_dataset_summary.txt` | Actual build counts and caveats |

**Commands already used:**

```powershell
py -3.13 audit_station_coverage.py
py -3.13 audit_historical_station_quality.py
py -3.13 build_dev300_v2.py --stage plan
py -3.13 build_dev300_v2.py --stage collect
py -3.13 build_dev300_v2.py --stage check-events
py -3.13 build_dev300_v2.py --stage build
```

## 11. Known documentation uncertainties and decisions still open

- Exact primary-source URL/version, retrieval timestamps, archive checksums, and data-use/license terms for EIA and warning shapefile: **not captured in the supplied run outputs**. Record them before publication.
- Historical weather reports/alerts may have been revised after initial issue; true real-time availability **not reconstructed**.
- Station coverage is not local weather representativeness; terrain, microclimate, coast, and remoteness can matter.
- Sampling produces a development cohort, not a population-weighted national operational risk sample.
- Cross-year operational assessment, station-vs-HRRR comparisons, and geographic generalization evaluation **not yet performed**.
- No model has been trained or validated as part of the data-preparation run described here.

---

**Documentation provenance:** Produced from `build_dev300_v2.py`, `audit_historical_station_quality.py`, and `build_site_station_table.py` / `audit_station_coverage.py` as available in this conversation's working environment, plus the user's pasted EIA-station audit, station-quality audit, failure diagnostics, development-build summary and observed final CSV columns. Where an exact fact was not demonstrated, it is presented as a limitation or follow-up check, not as a verified result.
