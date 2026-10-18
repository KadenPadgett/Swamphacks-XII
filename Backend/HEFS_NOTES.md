# HEFS adapter and verification

HEFS describes a river variable at a forecast location. Its threshold probabilities
are separate from jobsite flood exposure, rule-based flooding levels, required
safety actions, and the teammate's trained 0–10 hazard index. No HEFS result sets
a site stop-work alert or supplies an ML score.

## Official sources and actual API contract

Reviewed on 2026-10-17:

- [NOAA API overview](https://water.noaa.gov/about/api): the HEFS API is experimental,
  is not supported 24/7, and may change without notice.
- [HEFS Swagger](https://api.water.noaa.gov/hefs/v1/docs/) and
  [OpenAPI schema](https://api.water.noaa.gov/hefs/v1/schema/).
- [NOAA map-quantiles notebook](https://github.com/NOAA-OWP/data-service-notebooks/blob/master/HEFS/4_map_quantiles_demo.ipynb),
  linked from NOAA's overview, explains exceedance-quantile ordering and maxima.
- [NWPS schema](https://api.water.noaa.gov/nwps/v1/docs/swagger.json) for gauge context.

Verified public requests, with no API key:

| GET path under `https://api.water.noaa.gov/hefs/v1` | Query |
| --- | --- |
| `/locations/` | `location_id=BIFM5` |
| `/map-quantiles/` | `location_id=BIFM5&parameter_id=SSTG` |
| `/hydrograph-quantiles/` | `location_id=BIFM5&parameter_id=SSTG`; adapter pins `forecast_datetime` to map issuance |

The locations response is a list with `location_id` and `parameters`. Forecast
responses observed live are **objects** with `metadata` and `value_set`, despite
parts of the experimental schema describing arrays. Live map metadata uses
`thresholds` (plural); the adapter also accepts the schema's `threshold` spelling.
Map quantile values were arrays; the adapter also accepts their documented JSON
string representation. `forecast_datetime`, not the notebook's historical
`start_datetime` query example, is the current schema's issuance selector.

Supported combinations are stage `SSTG`/`FT` with a supplied `stage` threshold,
and flow `QINE`/`CFS` with a supplied `flow` threshold. Other units stay unavailable;
there is no implicit stage-to-flow, datum, feet-to-metres, or CFS-to-KCFS conversion.
NOAA's `calculated_flow` is retained as raw evidence but is not substituted for a
missing direct `flow` threshold: the reviewed schema does not specify its rating
curve, conversion, and threshold-unit contract sufficiently for this adapter.

## Probability representation and time semantics

All bounds use **0–1**, not 0–100. `probability` stays `null` because these endpoints
provide quantiles rather than an exact probability for each flood threshold.
`probability_bounds` describes an interval, including endpoint inclusivity and
method `conservative_exceedance_quantile_bounds_v1`.

For a supplied exceedance quantile `Q(e)` and threshold `T`, the adapter takes:

- `Q(e) > T`: lower bound at least `e`.
- `Q(e) < T`: upper bound at most `e`.
- `Q(e) == T`: no bound from that pair, avoiding false precision from ties and rounding.

It intersects these constraints with `[0,1]`, checks finite values, matching lengths,
unique probabilities strictly between zero and one, and non-increasing quantiles.
It does not interpolate a percentage between quantiles or infer an exact zero
above the largest supplied quantile. For example, `[0,0.05]` means **at most 5%**,
not exactly 5% or zero. An interval `[0,1]` is explicitly uninformative.
These are bounds inferred from NOAA's published ensemble quantiles, not guarantees
of calibrated real-world event frequencies or jobsite inundation probabilities.

`individual_times` preserves each hydrograph `target_time`. The verified BIFM5
forecast uses six-hour steps; no hourly values are manufactured. `period_maxima`
uses each ensemble member's maximum during the first `forecast_length` days
starting at `metadata.start_datetime`. It preserves `period_start`, `period_end`,
and `issued_at`. It is an exceedance **anywhere in that whole period**, not a value
at the period end or a new period starting at retrieval. A period that has already
begun is not reinterpreted as the remaining time from now. Neither collection is
repeated into the application's 48 hourly samples.

Hydrograph thresholds come from map metadata only when location, parameter, and
issuance match. The adapter pins the hydrograph request to that issuance and rejects
a response from another cycle. Forecast lengths are read from actual metadata;
the overview's ten-day description is not used to invent or truncate horizons.

## Live results and recorded fixtures

On 2026-10-17 at approximately 20:49 UTC, the schema, BIFM5 locations, map quantiles,
and hydrograph quantiles all returned HTTP 200. BIFM5 advertised `SSTG` and returned
stage units `FT`, issuance `2026-10-17T00:00:00Z`, and actual minor/moderate stage
thresholds of **16/18 feet**. All supplied five- and ten-day map quantiles were
3.33 feet. The adapter produced `[0,0.10]` for those period-threshold events and
`[0,0.05]` at the supplied hydrograph times. The live normalizer found **110 fresh
threshold-bound rows**, with older target rows marked stale. This verifies usable
forecast probability information at BIFM5; it does **not** verify exposure of any
bundled power plant to that gauge.

Earlier live BLDA1 requests advertised `QINE`, not `SSTG`; `SSTG` map retrieval
returned 404. BLDA1/QINE map and hydrograph requests returned HTTP 200 with `CFS`
and issuance `2026-10-16T12:00:00Z`. Its direct flow thresholds were all null.
Stage and some `calculated_flow` fields existed, but the adapter honestly returns
`availability: "unavailable"`, `reason: "no_usable_threshold_probabilities"`, and
null probabilities/bounds. A successful HTTP response does not establish a usable
probability. Bankhead is not presented as a successful HEFS probability pilot.

The unmodified JSON responses are recorded in `tests/fixtures/hefs/` and exercised
offline. Mutated edge cases and site associations are confined to temporary test
databases. Response excerpts under `docs/examples/hefs-*.json` identify their
recorded-data provenance and serialization time. They are historical examples,
not currently fresh forecasts and never seed application sites.

Recheck explicitly, without touching the production database:

```bash
python -B scripts/probe_hefs.py --location BIFM5 --parameter SSTG
python -B scripts/probe_hefs.py --location BLDA1 --parameter QINE
```

The probe writes raw and normalized results under `.cache/hefs-<location>-<parameter>-*.json`.
Live provider contents can change; tests use pinned recorded responses instead.

## Site association and refresh

1. Have a supervisor establish river/gauge relevance from site-specific evidence.
   A nearest gauge or power-plant coordinate alone cannot confirm flood exposure.
2. `PUT /api/sites/{id}/river` associates the verified NWPS gauge. Setting
   `exposure_confirmed` now also requires a documented confirming `review`, with
   river name, evidence type/text, source URLs and reviewer. A boolean alone is
   rejected. The API preserves the review but does not authenticate the reviewer's
   identity or independently certify their hydraulic interpretation.
3. `PUT /api/sites/{id}/hefs` stores this explicit forecast association:

```json
{
  "location_id": "BIFM5",
  "gauge_id": "BIFM5",
  "parameter_id": "SSTG",
  "exposure_confirmed": false,
  "evidence": "Replace with the actual association evidence and reviewer reference"
}
```

This format example is not permission to associate BIFM5 with unrelated sites.
The initial adapter requires matching NWPS/HEFS IDs. Claiming confirmed exposure
also requires the matching river association to be confirmed. Unsupported
locations/parameters are stored with explicit unavailable state for diagnosis.
HEFS locations are not auto-assigned. Bankhead Dam (EIA plant 2) now has a
documented **facility-to-river connection** to BLDA1, based on the plant operator,
USACE and live NOAA gauge identity. The reviewed record is in
`data/river_exposure_reviews.json`; startup imports it only for that exact EIA
identity/utility/name/coordinates, once, while respecting existing manual mappings
and withdrawals. This is not a work-area floodplain determination or jobsite
inundation model. BLDA1/QINE still lacks usable direct flow thresholds, so the
Bankhead connection does not close the HEFS probability-data gap. No BIFM5
probability is reassigned to Bankhead to manufacture a successful demonstration.

`GET /api/sites/{id}/river/exposure` returns the persisted review and its validity.
Reviews become invalid if site coordinates or gauge change. Legacy confirmation
booleans without evidence are treated as unconfirmed. Details and map rows expose
`river_exposure`; HEFS exposes separate `site_relevance` and `applicability`:
`context_only` until a valid review exists, then `verified_river_context`.
Forecast availability remains independent of relevance. Historical briefings
preserve the relevance record captured at their generation time.

`GET /api/sites/{id}/hefs` reads saved state; `POST /api/sites/{id}/hefs/refresh`
refreshes that association. Details, ranked-map rows, and briefings expose the
separate `river_forecast`. The independent `hefs` background job refreshes only
associated sites every `PLANIT_HEFS_SECONDS` (minimum/default 900 seconds after
completion), with its own job/site leases and persistent diagnostics. Full weather
refreshes, alerts, reports, and briefings do not wait for HEFS or ML.

Locations cache for 24 hours; products cache for 15 minutes. Original retrieval,
issuance, target, and expiry timestamps remain visible. Serialization reevaluates
source expiry, passed target times, and an application issuance-age limit of
48 hours. That limit is a conservative application policy, not NOAA's update SLA.
Stale bounds remain historical evidence with `availability: "stale"`; they do not
become fresh when the assessment or briefing is regenerated. Outages retain saved
products even after provider-cache eviction, with original source timestamps.

## NWPS reliability findings

The original 38 failures among 61 saved sites lacked HTTP codes, so their exact
historical causes cannot be reconstructed. Live verification confirmed `bbox.*`
and `srid=EPSG_4326` against the NWPS schema. MEMT1 detail returned HTTP 200.
Collection calls returned 429 with a body advertising **10 requests / five minutes**
and no Retry-After; that limit was observed live, not specified numerically in the
reviewed OpenAPI. A burst of per-site discovery calls can exceed it.

Discovery now shares one daily national gauge catalogue and filters within 10 km
locally. The national-catalogue request was rate limited in the initial check and
timed out in the final verification attempt (12-second probe timeout), while the
schema and MEMT1 detail again returned 200. Successful retrieval of that full
catalogue remains unverified; nationwide automatic association is not claimed.
HTTP status, attempt time, and sanitized error codes distinguish `timeout`,
`network_error`, `rate_limited`, `http_error`, `invalid_schema`, `missing_data`,
and `request_in_progress` from a valid `no_gauge_within_10_km` result. Requests have
a four-second timeout and at most one short retry for transient errors. Retry-After
and provider-family cooldowns are respected; absent guidance on a 429 uses five
minutes. Credentials, URL query strings, response bodies, and raw exception text
are not persisted in source diagnostics. Unconfirmed or stale gauges cannot
independently trigger site-specific stop-work; NWS and rainfall rules remain active.
