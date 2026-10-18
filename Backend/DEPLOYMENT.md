# Local WSL startup

Use Python 3.14 for the verified frozen-model environment. Windows and WSL need separate virtual environments.
From this Backend directory in Ubuntu/WSL:

```bash
python3 -m venv .venv-wsl
source .venv-wsl/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
set -a
source .env
set +a
mkdir -p "$HOME/.local/share/planit/photos"
export PLANIT_DATABASE_PATH="$HOME/.local/share/planit/sites.db"
export PLANIT_UPLOAD_DIR="$HOME/.local/share/planit/photos"
python -B scripts/serve.py
```

If Ubuntu lacks venv support, install its `python3-venv` package first.
The server is at http://localhost:8000; interactive endpoint documentation is
http://localhost:8000/docs and the machine-readable contract is `/openapi.json`.
Existing root `sites.db` remains the default when PLANIT_DATABASE_PATH is unset.
The example environment selects a new database under `data/runtime`; either point
it at the existing file or copy that file while the old backend is stopped.
Use a WSL-native Linux directory for the live database and photos, as above,
even if the source checkout is under `/mnt/c`. Do not open one live SQLite file
from both Windows and WSL, or place it on a network/shared filesystem. Stop every
writer before copying an existing database. Windows can use its own separate
database with its Windows virtual environment.

The corruption was repaired on 2026-10-17 with all writers stopped. Both root
`sites.db` and the path currently configured in `.env`, `data/runtime/sites.db`,
now pass full integrity/foreign-key checks and contain 61 sites and 61 readable
assessments. The unreadable assessments for sites 14 and 26 were regenerated from
live public providers; site 26's river request timed out and is explicitly unavailable.
The other 59 historical assessments were retained; repair does not make them fresh.

The damaged original is preserved at
`.cache/sites.pre-repair.20261017T223034516719Z.db`. Repair and refresh audits are
`.cache/database-repair.20261017T223034516719Z.json` and
`.cache/database-repair.20261017T223227418754Z.json`. The older 59-assessment
recovery candidate remains available, but use the repaired 61-assessment database
for a new migration. Stop all writers before copying it to a WSL-native storage
path. These local runtime databases/backups are ignored by Git and may not be
included in a shared archive.

`scripts/repair_database.py` documents the guarded restoration used here. It
refuses to restore over a source changed since its recovery manifest or overwrite
an existing configured database. Do not rerun `--restore` on the now-healthy source.

Run checks with `python -B -m unittest discover -s tests -v`. Tests use temporary
databases and photos and mock provider responses; they need no ML or lightning key.
The fixture hazards and sample model outputs are test-only and never seed live sites.

# Hosting requirements

Hosting has not been selected and no public backend URL has been deployed.
Use an always-running Linux service/container with a persistent writable disk;
request-only serverless processes and services that sleep cannot perform scheduled
work. Run one application instance for this SQLite deployment. Leases coordinate
multiple processes on the same local SQLite file, not replicas on separate disks.
Keep the system clock synchronized and permit outbound HTTPS to the configured
weather, NWS, NASA EONET, iNaturalist, NOAA NWPS/HEFS, lightning and optional ML providers.

1. Build this Dockerfile, or install requirements in a managed Python service.
2. Mount persistent storage at `/data`. Set `PLANIT_DATABASE_PATH=/data/sites.db`
   and `PLANIT_UPLOAD_DIR=/data/photos`. Ensure the process can write both paths.
3. Set `PLANIT_ALLOWED_ORIGINS` to comma-separated frontend origins including the
   scheme and port, e.g. `https://frontend.example`. Localhost:5173 stays allowed.
4. Keep `PLANIT_BACKGROUND_REFRESH=1`. Run `python scripts/serve.py`; the host can
   set PORT. Configure restart-on-failure and health checks at `/api/health`.
5. Enable HTTPS at the host proxy. Permit long-lived SSE responses and disable
   proxy buffering for `/api/sites/*/events`. Clients reconnect with Last-Event-ID.
6. Set a strong `PLANIT_WRITE_TOKEN` for public writes. The frontend sends it as
   a bearer token on POST/PUT. This shared-token option is suitable for the demo;
   it is not individual user authentication, and GETs/photos remain public.
7. Back up the database with SQLite's backup API and back up the photo directory.
   Keep both on persistent storage through rebuilds/restarts.
8. Verify `/api/refresh/status`, create a site, watch the automatic assessment,
   submit a report/photo, observe its site event, and trigger a briefing.
   Restart the service and check that records, photo URLs, and countdown deadlines survive.

With Docker Compose installed, `docker compose up --build -d` uses a persistent
named volume and restart policy. `docker compose down` retains it; avoid `down -v`
unless intentionally removing saved data. The container listens on port 8000.

# Runtime settings and schedules

| Variable | Meaning/default |
| --- | --- |
| PLANIT_DATABASE_PATH | SQLite file, defaults to Backend/sites.db |
| PLANIT_UPLOAD_DIR | Photo directory, defaults to Backend/data/uploads |
| PLANIT_ALLOWED_ORIGINS | Additional frontend origins; localhost development remains enabled |
| PLANIT_BACKGROUND_REFRESH | 1 enables all scheduled jobs; 0 disables them |
| PLANIT_REFRESH_SECONDS | Full assessment delay after completion, 300 seconds |
| PLANIT_LIGHTNING_SECONDS | Independent thunderstorm/NWS delay, 30 seconds |
| PLANIT_STRIKE_SECONDS | Independent measured-strike poll delay, 15 seconds |
| PLANIT_HEFS_SECONDS | Associated-location HEFS delay, default/minimum 900 seconds |
| PLANIT_LIGHTNING_API_KEY | Weather Company lightning entitlement; blank means unknown coverage |
| PLANIT_LIGHTNING_PROVIDER | auto, xweather, or the_weather_company |
| PLANIT_XWEATHER_API_KEY | Xweather combined client_id_client_secret, server-side only |
| PLANIT_XWEATHER_CLIENT_ID / PLANIT_XWEATHER_CLIENT_SECRET | Optional separate credentials instead of combined setting |
| PLANIT_WRITE_TOKEN | Optional bearer token for all POST/PUT requests |
| PLANIT_ML_URL / PLANIT_ML_TOKEN / PLANIT_ML_MODEL_VERSION | Optional teammate inference adapter |
| PLANIT_USER_AGENT | Provider request identification/contact string |
| PORT / HOST | 8000 / 0.0.0.0 |

Briefings check every 30 seconds and deliver at the first tick at/after 18:00 in
the site's IANA timezone. A restart after 18:00 catches up that evening; it does
not send past days' reports. A unique site/target-date key prevents duplicates.
`POST /api/sites/{id}/briefings/preview` creates no saved delivery or notification.
Actual manual delivery through `POST /api/sites/{id}/briefings` intentionally
consumes the once-per-date key. An early preview cannot freeze the evening report.
A site with unknown
timezone is explicitly skipped until weather discovers it or an owner sets it.

Each job has a persistent lease, run history and failures. A successful processing
run may use cached data; source retrieved_at/observation times remain unchanged.
The assessment's generated_at and the monitor's checked_at are processing times.

Assessment display validity is `max(900, 2*PLANIT_REFRESH_SECONDS + 120)` seconds,
independent of missing-source retry timing. The default is 15 minutes, while normal
refresh is due after five minutes. Source TTLs/observation times remain independent.
Old ML targets are reevaluated on reads and become null/stale, with originals
retained; regeneration alone does not make forecasts valid for a new target time.
See [MODEL_HANDOFF.md](MODEL_HANDOFF.md) for the exact temporary target windows.

## HEFS operation and verification

No HEFS key is required by the public endpoints verified on 2026-10-17. The API
is experimental and may fail or lack data. Configure explicit gauge/forecast
associations before relying on the independent `hefs` job; other features run
without any association. Job status/failures appear in `/api/refresh/status`.
Forecast products retain their original 15-minute source TTL and issuance age;
availability is reevaluated on saved reads. NOAA outages retain old bounds as stale.

`python -B scripts/probe_hefs.py --location BIFM5 --parameter SSTG` verified live
HTTP 200 responses and usable stage-threshold bounds. BLDA1/QINE returned forecasts
but lacked usable direct flow thresholds; it is explicitly unavailable for
probabilities. Neither probe confirms exposure of a bundled jobsite. Supervisor
validation of work-area inundation remains pending. Bankhead's actual facility
connection to BLDA1 is now documented from operator/USACE/NOAA sources and imported
once on startup for the exact EIA plant. That proves river relevance, while missing
HEFS flow thresholds still prevent usable probabilities there. Other sites need
their own documented review. See [HEFS_NOTES.md](HEFS_NOTES.md)
for exact requests, bounds semantics, NWPS failure findings, and observed limitations.

To run offline verification and regenerate labeled examples:

```bash
python -B -m unittest discover -s tests -v
python -B tests/export_examples.py
```

Useful local checks: `GET /api/health`, `GET /api/refresh/status`, `GET /api/risks`,
`GET /api/sites/1/detail`, `GET /api/sites/1/hefs`,
`POST /api/sites/1/briefings/preview` (requires that site's timezone), and
`GET /api/reports/nearby?latitude=25.75&longitude=-80.7&radius_km=10&limit=50`.
Use `/docs` for association writes and the existing ML endpoints.

# Lightning providers

Xweather access was verified live on 2026-10-17: HTTP 200 with a valid empty
response at 25.75/-80.7 within 10 miles. The adapter's fixed four-minute query also
returned available. This verifies credentials/endpoint access, not an actual
nearby strike. The local credential was moved to `PLANIT_XWEATHER_API_KEY`;
`PLANIT_LIGHTNING_API_KEY` is reserved for Weather Company.

Set `PLANIT_LIGHTNING_PROVIDER=xweather` and the combined `client_id_client_secret`
in `PLANIT_XWEATHER_API_KEY`, or use `PLANIT_XWEATHER_CLIENT_ID` and
`PLANIT_XWEATHER_CLIENT_SECRET`. Default `auto` selects Xweather when its settings
exist, otherwise Weather Company. Source ignored `.env` before starting Python
as above; restart/reload that environment after changes. Credentials are never
included in API output or the public example environment file.

The [Xweather lightning endpoint](https://www.xweather.com/docs/weather-api/endpoints/lightning)
returns individual measured CG/IC records. Standard access has five minutes of
history; the adapter requests a fixed four-minute interval within 10 miles,
validates coordinates/timestamps, and reads at most three 1,000-record pages.
Truncation or a failed page keeps coverage unknown; validated events from earlier
pages still trigger stop-work. HTTP 429 honors Retry-After via process cooldown.
Authenticated query strings are redacted from httpx logs; raw provider errors
are not logged.

Overlapping successful windows accumulate in SQLite. An initial empty response
cannot supply a 30-minute all-clear: after four minutes of initial history, roughly
26 more minutes of continuous polling are needed. A gap beyond the available
window resets warmup. Restarting retains coverage only if the next query overlaps
it; switching providers cannot reuse old coverage for clearance. Existing distance,
countdown, duplicate/newer-strike and outage safety rules remain unchanged.

Polling consumes account quota. Xweather documents a standard lightning cost
multiplier; choose `PLANIT_STRIKE_SECONDS` for your entitlement and site count.
Broad live bulk polling was not started during verification.
`python -B scripts/probe_xweather.py --adapter` reads local `.env` and prints only
sanitized diagnostics without writing production state. Use the existing
`POST /api/sites/{id}/lightning/verify` for a saved operational check.

## Optional Weather Company adapter

The adapter uses The Weather Company's actual-coordinate API at
`https://api.weather.com/v3/wx/lightning/60minute/desktop`, with all strike types.
Obtain an account/key entitled to that endpoint before setting the environment
variable. Its documented desktop window is 55 minutes. Distances are recomputed
from coordinates using a 16.09344 km (10 mile) radius. Each newer qualifying strike
sets deadline = strike time + 30 minutes; duplicate/older events cannot reset it.
No synthetic production ingestion endpoint exists.

If the feed fails, the previous deadline and stop-work alert remain saved; unknown
coverage is never clearance. Clearance needs an expired deadline plus a fresh,
successful feed covering the preceding 30 minutes. Polling, provider latency,
network outages and backlog affect detection delay; this is not a zero-latency feed.
The adapter is contract-tested but live entitlement cannot be verified until a key
is supplied. See [provider contract](https://developer.weather.com/docs/openapi/lightning-3-0/get-v3-wx-lightning-duration-platform-by-geocode).

After installing an entitled key in the server's `PLANIT_LIGHTNING_API_KEY`
environment variable, restart and call `POST /api/sites/{id}/lightning/verify`
(with the write bearer token if configured). Inspect `provider_verification`,
availability and reason; HTTP 200 means diagnostic state was returned, not that
entitlement succeeded. A 401/403 provider response remains unknown coverage and
does not disclose the key. Read saved state at `GET /api/sites/{id}/lightning/strikes`.
Successful empty data verifies feed access; actual observed strikes are reported
separately. Restarting preserves deadlines; removing the key immediately disables
clearance even if a previous successful check is still within its freshness window.
The verification route and recurring strike job share a per-site lease.

NOAA river discovery uses a 10 km search and [the NWPS API](https://api.water.noaa.gov/nwps/v1/docs/).
Nearby-gauge proximity is labeled separately from supervisor-confirmed river
exposure; it does not prove hydraulic connectivity or floodplain membership.

# METAR model service

The supplied geographic frozen model is loaded locally by default. Keep
`PlanIT-ML/` next to `Backend/` for Ubuntu/WSL source startup. The Compose file
mounts its geographic artifact read-only into the container. Other container
deployments must mount the same artifact and set `PLANIT_METAR_ML_ARTIFACT` to
its in-container path. The optional remote model boundary uses
`PLANIT_METAR_ML_URL`, `PLANIT_METAR_ML_TOKEN`, and
`PLANIT_METAR_ML_MODEL_VERSION` in `.env`. See
[METAR_MODEL_HANDOFF.md](METAR_MODEL_HANDOFF.md) for the frozen-model service
request and response and the Ubuntu/WSL setup steps.

# Initial site assessments

With `PLANIT_BACKGROUND_REFRESH=1` (the default), a newly saved site is assessed
immediately in a separate task and failed initial attempts are retried after 30,
60, 120 seconds and so on, capped at 15 minutes. The initial status is persisted
in SQLite, so a worker restart can resume pending or due failed work. Full-site
periodic refresh continues independently. With background refresh disabled,
new sites still queue an immediate assessment and failed attempts can be retried
through `POST /api/sites/{id}/assessment/retry`; automatic retries require the
background worker. The frontend polls while initialization is pending.
