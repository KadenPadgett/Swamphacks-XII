# Backend handoff and startup

Read [FRONTEND_HANDOFF.md](FRONTEND_HANDOFF.md) for alerts, crew reports/photos,
in-app notifications, rule-based map ordering, ML-independent briefings, and
example responses. Read [DEPLOYMENT.md](DEPLOYMENT.md) for WSL/container startup,
persistent storage, environment settings, and lightning provider configuration.
The model adapter remains documented in [MODEL_HANDOFF.md](MODEL_HANDOFF.md).
HEFS endpoints, probability bounds, live verification, and limitations are in
[HEFS_NOTES.md](HEFS_NOTES.md). WSL startup should use a Linux-native storage path;
see the database-recovery note in the deployment guide before using root `sites.db`.

River confirmation now requires an evidence review bound to site coordinates and
gauge; a boolean alone cannot establish relevance. A documented operator/USACE/NOAA
record connects the actual Bankhead plant to BLDA1 on the Black Warrior River.
This establishes river connection, not work-area inundation, and BLDA1 still lacks
usable HEFS flow thresholds. Source evidence is in `data/river_exposure_reviews.json`.
Use `GET /api/sites/{id}/river/exposure` to inspect review validity.

Xweather access is now verified live (a valid empty query, not an observed strike).
Configure `PLANIT_XWEATHER_API_KEY` and `PLANIT_LIGHTNING_PROVIDER=xweather` in ignored
`.env`; Weather Company credentials remain in their separate legacy setting.
Once an entitled lightning key is configured, use
`POST /api/sites/{id}/lightning/verify` to check actual feed access; successful empty
data does not claim an observed strike. `GET /api/sites/{id}/lightning/strikes`
exposes access verification separately from coverage/countdown. Missing credentials
force unknown coverage even after a previous success; saved holds remain intact.
Xweather's short history accumulates across overlapping polls; initial success
does not establish the full 30-minute clearance window. Setup and quota guidance
are in [DEPLOYMENT.md](DEPLOYMENT.md).

The backend runs without a configured ML model. Rule levels, required actions,
and actual model predictions are separate; unavailable scores are null.

# Automatic background updates

Start the backend with `.\.venv\Scripts\python.exe -m uvicorn main:app`.
It now runs recurring refresh jobs while the backend is running, even with no
dashboard or browser open. Keep the backend process running; closing the dashboard
does not stop updates, but stopping the backend does.

- **Full risk refresh:** every 300 seconds after the previous run finishes.
- **Independent lightning monitor:** every 30 seconds after its previous run finishes.
- **Measured-strike adapter:** independently every 15 seconds; coverage stays unknown
  until a real provider key is configured. Countdown state persists across restarts.
- **Briefings:** check every 30 seconds for 18:00 in each site's stored timezone.
- **HEFS:** refresh explicitly associated forecast locations every 900 seconds,
  independently of weather, alerts, and ML.
- All jobs start when due on backend startup and read all saved sites on each run,
  including newly created custom sites. A new site enters the next cycle automatically.
- SQLite leases and heartbeats prevent duplicate runs across workers, the manual
  refresh endpoint, and the CLI. Per-site leases also coordinate on-demand requests.
  A crashed worker's lease expires after 60 seconds. Clean shutdown cancels work
  and releases its locks. A duplicate manual full refresh returns HTTP 409.

The fast monitor requests only current thunderstorm model conditions and NWS
alerts, using its own client, cache keys, and locks. It never waits for wildfire,
wildlife, ML inference, or the full refresh job. It **does not supply measured
lightning strikes**: those remain explicitly unknown. Thunderstorm forecasts and
NWS alerts alone cannot support a strike-distance countdown. The separate measured
strike adapter implements the persistent 10-mile/30-minute rule once access is available.
The monitor caches NWS results for 30 seconds and model conditions for 5 minutes.
Polling faster does not turn those modeled conditions into strike observations.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/refresh/status` | Job configuration, recent runs/failures, active leases, last successful jobs, and per-site success/failure times |
| `GET /api/sites/{site_id}/lightning` | Read the independently updated lightning monitor without triggering a full refresh |
| `GET /api/risks` | Portfolio with source timestamps and each site's latest `lightning_monitor` |
| `GET /api/sites/{site_id}/risk` | Full assessment plus the independent `lightning_monitor` |
| `POST /api/risks/refresh` | Manual full refresh of all monitored sites; optional `limit` and `offset` select a page |

`generated_at` (assessment) and `checked_at` (lightning monitor) describe processing
times. Source `retrieved_at`, `expires_at`, and `cache_hit` remain visible; cache hits
preserve the original retrieval time. Weather observation timestamps remain in
the weather payload and monitor. A new assessment **does not mean new source data**.
The independent monitor's timestamp can be newer than the full assessment's
lightning card; consume `lightning_monitor` for the latest check.

Job/site `last_success_at` means the configured feed-processing check succeeded,
including valid cached responses. It does not claim that source observations are
new or that measured strike coverage exists. Partial provider failures and exceptions
are recorded without aborting other sites. Last-success timestamps survive later
failures; the latest 1,000 job runs are retained. Last-success/failure state persists
separately in SQLite. Refresh locks and status use the same `PLANIT_DATABASE_PATH`.
Failed, partial, or interrupted jobs retry after at most 30 seconds. Existing ML
predictions survive a refresh without replacement results, keeping their original
timestamps and an explicit `stale` label.

Optional environment settings (set before starting the server):

| Setting | Default |
| --- | --- |
| `PLANIT_REFRESH_SECONDS` | `300` (minimum 10) |
| `PLANIT_LIGHTNING_SECONDS` | `30` (minimum 5) |
| `PLANIT_BACKGROUND_REFRESH` | `1`; use `0` to disable automatic jobs for tests or maintenance |

`scripts/refresh_risks.py` remains a one-shot manual command, coordinated with the
same locks. It no longer needs a separate cron/scheduler because the backend owns
the recurring jobs. Refresh intervals are delays after completion, so a slow run
cannot cause another run of the same kind to overlap. The lightning monitor is
marked stale after 90 seconds without a completed check.

Run the regression tests with
`.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`.

# Database

Sites are stored in SQLite. Starting the FastAPI app creates `sites.db` beside
`database.py` and adds the original demo site if it is absent. Existing sites
are preserved across restarts. No separate database server is required.

Optionally set `PLANIT_DATABASE_PATH` before starting the server to use another
database file. Its parent directory must exist. Use an absolute path for a
consistent location regardless of the working directory. Keep this file on
persistent storage when deploying the backend.

`POST /api/sites` accepts `name`, `latitude`, and `longitude` as JSON and returns
the saved site with its database-assigned `id` and status 201. Listing sites,
retrieving a site, and retrieving site weather all read from the database.

# Combined site risk API

The backend imports **60 distinct operating power plants** from the official
[EIA-860M August 2026 workbook](https://www.eia.gov/electricity/data/eia860m/xls/august_generator2026.xlsx).
The bundled `data/eia_plants.json` records the workbook URL, report month,
retrieval timestamp, SHA-256, selection method, and original plant IDs.
Selection rotates through states/territories, then ascending EIA plant ID.
Multiple generators at one plant become one site with combined technology names.
These are actual plant locations; the inventory does not establish that active
construction is occurring at them.

## Running and refreshing

Start the existing application with `.\.venv\Scripts\python.exe -m uvicorn main:app --reload`.
Startup creates/migrates SQLite tables and imports the bundled catalog without
network requests. Existing custom sites and site IDs are preserved. `GET /api/sites`
returns the original fields plus `eia_plant_id`, `utility`, `county`, `state`,
`plant_type`, `source`, `source_url`, and `source_period`. Custom sites have null
EIA metadata. The original demo is retained and is not counted toward the 60 plants.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/sites/{site_id}/risk` | One combined assessment; fetches feeds when the saved assessment has expired |
| `GET /api/risks` | Fast portfolio overview of all saved assessments, with `pending` and `stale` flags |
| `POST /api/risks/refresh?limit=60` | Refresh a bounded page of monitored sites; omit limit to refresh every saved site |

The batch request waits for completion and can take several minutes on a cold
cache. Configure the frontend's request timeout accordingly. Assessments persist
across restarts. The overview never initiates hundreds of remote requests.

For a local batch with progress output:

```powershell
.\.venv\Scripts\python.exe -B scripts/refresh_risks.py
```

The bundled catalog is a recorded EIA snapshot. This archive does not include a
catalog-download command; do not assume it continuously tracks EIA releases.
When a reviewed replacement catalog is supplied, restart the backend. Plants are
matched by original EIA plant ID and existing custom sites are retained.

## Response contract

A site risk response contains:

- `site`: name, coordinates, internal ID, and EIA metadata.
- `generated_at`, `method_version`, `forecast_hours`, `cached`, and `stale`.
- `overall`: `level`, `highest_known_level`, `drivers`, `coverage_complete`,
  `coverage_gaps`, and an explanation of the aggregation method.
- `indicators`: seven named cards, each with `level`, `basis`, `evidence`,
  provider keys in `sources`, and `coverage` (`available`, `partial`, `unavailable`).
- `sources`: feed URLs, retrieval timestamps, availability, and failure reasons.
- `weather`: current conditions, 48-hour forecasts, and recent rainfall, or null
  when weather is unavailable.

The portfolio response contains `count` and a `sites` array. Each entry has the
site, assessment status, timestamp, stale flag, overall result, and `risk_url`.
An old result is retained for reference but always marked stale; `pending` means
that the site has never been assessed.

## Sources and interpretation

| Indicator | Inputs | Interpretation |
| --- | --- | --- |
| `weather` | Open-Meteo gusts and hourly precipitation; NWS storm/wind alerts | Planning screen over current conditions and the next 48 hours |
| `extreme_heat` | Open-Meteo apparent temperature; NWS heat alerts | Temperature screen, not worker-specific heat exposure |
| `lightning` | Open-Meteo thunderstorm codes 95, 96, 99; NWS thunderstorm alerts | Forecast proxy; measured coverage/countdown come from the separately configured strike adapter |
| `flooding` | NWS flood alerts, recent rainfall, maximum rolling 24-hour forecast precipitation | Rainfall/alert screen; no floodplain or inundation calculation |
| `wildfire` | NWS fire alerts and NASA EONET open wildfire points reported in the last 14 days | Proximity within 50 km; not fire perimeters or an exhaustive fire inventory |
| `soil_saturation` | Open-Meteo volumetric soil moisture at 3–9 cm and recent rainfall | Soil wetness proxy; actual saturation requires soil-specific porosity |
| `wildlife_sightings` | iNaturalist research-grade, non-captive Animalia observations in the last 30 days within 10 km | Ecological context; not species danger, absence, or protected-species clearance |

Wildlife evidence includes a total matching count and up to 20 recent sightings,
with species names, dates, source links, and an obscuration flag. Obscured public
locations can shift search membership. Exact sighting coordinates and observer
identities are not returned. Empty search results do not establish absence.

Wildfire distances use the latest reported point for each open event and the
haversine formula. Unsupported non-point geometries are counted and mark fire
coverage partial rather than being silently treated as no fire.

Provider documentation:
[EIA-860M](https://www.eia.gov/electricity/data/eia860m/),
[Open-Meteo](https://open-meteo.com/en/docs),
[NWS](https://www.weather.gov/documentation/services-web-api),
[NASA EONET](https://eonet.gsfc.nasa.gov/docs/v3),
[iNaturalist](https://api.inaturalist.org/v1/docs/).

## Transparent screening rules

These are application screening thresholds, not official hazard probabilities
or engineering determinations. Threshold lists below correspond to **watch / high /
severe**, inclusive at each boundary:

| Metric | Thresholds |
| --- | --- |
| Maximum gust | 30 / 45 / 65 mph |
| Maximum hourly precipitation | 0.30 / 0.75 / 1.00 inches |
| Maximum apparent temperature | 90 / 100 / 110 °F |
| Maximum rolling 24-hour forecast precipitation or last 24-hour rain | 1 / 2 / 4 inches |
| Maximum modeled soil water content at 3–9 cm | 0.30 / 0.40 / 0.50 m³/m³ (not calibrated to local soil) |

Thunderstorm codes trigger `high`. Wildfire points within 10 km trigger `high`;
within 25 km trigger `watch`. Relevant NWS alerts raise a card to at least the
alert's mapped severity: Extreme → severe, Severe → high, Moderate/Minor → watch.
Warnings are at least high. Expired, cancelled, and test alerts are excluded.

Overall severity is the **maximum** of environmental cards; wildlife is `info`
and never raises severity on sighting count alone. Missing data is never scored
as zero. When coverage is incomplete and all known signals are low, overall is
`unknown`. A known high/severe hazard remains visible even when other feeds fail.
`coverage_complete` describes the selected feeds/fields, not complete knowledge
of all hazards at the site.

## Caching and failures

- Assessments refresh after 5 minutes and remain valid for 15 minutes by default, including incomplete assessments. Failed-source retry timing is separate from display expiry.
- Open-Meteo: 15 minutes; NWS: 5 minutes; EONET: 30 minutes; wildlife: 24 hours.
- Provider errors: cached for 1 minute to avoid repeated failing calls.
- NOAA NWPS/HEFS: bounded four-second requests, one transient retry, provider
  cooldown/Retry-After handling and retained stale data. Gauge discovery shares
  a daily catalogue. HEFS products cache for 15 minutes and locations for 24 hours.
- Timeout per upstream request: 15 seconds, with 5 seconds for connection setup.
- Batches use four concurrent sites, one request per provider at a time, and at
  least 1.1 seconds between wildlife requests. The global EONET feed is shared.

The caches live in SQLite; no API keys are needed for the configured public
endpoints. `PLANIT_USER_AGENT` can set the NWS application/contact identifier.
`PLANIT_DATABASE_PATH` can select a persistent database file.
Provider failures produce partial risk responses with explicit reasons instead
of concealing other available signals. A missing site still returns 404.

Current ML scores and hourly predictions expire on reads according to their
target times. Display scores become null/stale; original predictions remain
archived. Map priority is explicitly rule-based: stop-work and known elevated
hazards first, incomplete/unknown next, complete-low last. HEFS probability bounds
are separate river information, not site flood probabilities or ML scores.

Briefing previews use `POST /api/sites/{id}/briefings/preview` and do not consume
the evening delivery. The existing briefing POST performs actual manual delivery.
Historical crew reports can be discovered across sites using
`GET /api/reports/nearby` with bounded coordinates, radius and limit.

Run checks with `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`.
# METAR warning-model integration

See [METAR_MODEL_HANDOFF.md](METAR_MODEL_HANDOFF.md) for the IEM observation contract,
the six-hour NWS warning target, frozen local model, limitations, and Ubuntu/WSL startup.
