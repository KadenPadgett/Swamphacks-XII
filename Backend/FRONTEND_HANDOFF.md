# PlanIT backend contract

Local API: `http://localhost:8000`. Interactive docs: `/docs`; OpenAPI: `/openapi.json`.
There is no deployed public URL yet. Hosting and the frontend production origin
are pending. Setup and persistent-storage requirements are in [DEPLOYMENT.md](DEPLOYMENT.md).

## Ranked map

`GET /api/risks` returns saved assessments immediately. Each site includes ID,
name, latitude/longitude, `current_score`, `ml_availability`, `upcoming_peak`,
`active_alert_status`, `required_safety_actions`, `lightning_countdown`, source
timestamps, `generated_at`, `stale`, and `rank`.

The temporary ordering is **rule-based**: active stop-work alerts first, then
known elevated rule/alert severity (severe, high, watch), then missing, stale or
incomplete/unknown assessments, then complete low assessments. Site ID breaks ties.
`ranking.version` is `stop-work-then-coverage-v2`; use `rank`, `ranking_reason`,
`coverage_status` and `assessment_status`. An incomplete assessment whose
`highest_known_level` is low remains unknown for ordering. Unknown precedes low so missing data does not
imply safety. The response's `ranking` object documents this contract. The frontend
should display a “Rule-based priority” label and use the provided rank.
This contract is ready for teammate review; no external teammate sign-off has been claimed.

ML never determines this ordering. `current_score` is null when unavailable or
retained/stale. A genuine model score of zero remains zero. Retained model output
is separately available with its original target times and stale label. Do not
turn null into zero. `upcoming_peak.rule_based` and `.ml_prediction` are different
objects; tomorrow's forecast does not become today's condition.

## Site detail

`GET /api/sites/{id}/detail` reads saved state without provider/ML waits.
`GET /api/sites/{id}/risk` preserves the existing refresh-on-demand behavior and
returns the same combined fields plus legacy risk fields.

`POST /api/sites` now returns promptly after saving, including `initial_assessment`
with `status: pending`. A separate worker starts the assessment immediately. The
same object appears in `/api/risks` rows and `/api/sites/{id}/detail` with
`pending`, `running`, `completed`, or `failed`, plus `queued_at`, `started_at`,
`finished_at`, `attempts`, `next_retry_at`, `error`, and `source_failures`.
`completed` means an assessment was saved; check `source_failures` and `sources`
for partial coverage. Failed attempts retry automatically with bounded backoff
when background refresh is enabled. `POST /api/sites/{id}/assessment/retry`
immediately requeues a failed attempt. The connected frontend polls `/api/risks`
and selected detail every three seconds while any initial assessment is pending
or running, then resumes its usual cadence. Display “Assessing…” during that time.

The separate `weather_model` object is the experimental probability of a **new
NWS Severe Thunderstorm Warning covering the site within six hours**. Show
`probability` as a percent only when `availability=available`; label it
experimental and state that calibration is not established. Show `model_version`,
`prediction_time`, `outcome_start`, `outcome_end`, `current_observed_at`,
`prior_observed_at`, `station`, `input_availability`, and `input_reason`. Check
the parallel `metar` object even when a scored result exists: stale observations
remain visible. `availability=stale` with `reason=prediction_expired` has null
display probability. Distinguish pending, missing METAR input, unavailable model,
and expired output. `weather_model.score` is 10 × this probability and is **not**
`current_score`, a site hazard index, or a ranking input. Stop-work and lightning
clearance remain rule based. The connected frontend implements this contract.

Example creation response (times and ID illustrative):

```json
{"id": 62, "name": "North site", "latitude": 30.0, "longitude": -81.0,
 "initial_assessment": {"site_id": 62, "status": "pending", "queued_at": "2026-10-17T12:00:00+00:00",
 "started_at": null, "finished_at": null, "attempts": 0, "next_retry_at": null,
 "error": null, "source_failures": []}}
```

| Field | Meaning |
| --- | --- |
| rule_based_assessment | Current levels/evidence, hourly rules, highest upcoming rules |
| required_safety_actions | Active application-alert actions, including stop_work and basis |
| ml_predictions / ml_assessment | Actual model output only, availability/stale/version/target times |
| conditions / hazard_evidence | Environmental data and reasons behind each hazard |
| alerts | Active persistent application alerts |
| active_crew_reports | Unresolved reports with map pins and photo URLs |
| lightning_countdown | Persisted latest strike/deadline plus computed remaining seconds |
| freshness | Assessment generation time and original source timestamps |
| river_forecast | Separate HEFS river-threshold bounds at original target times/periods |
| river_exposure | Documented river relevance, reviewed identity/coordinates, sources, and invalidation status |
| timezone | Available/unknown IANA timezone and its source |
| notifications_url | Site-specific resumable event stream |

Use `lightning_countdown.status`: `stop_work`, `clear`, or `unknown`. Display the
absolute deadline and count down in the browser using `server_time`; reconnect or
poll to update it. An expired timer with unknown coverage is **not an all-clear**.
Measured-strike coverage and thunderstorm model availability are separate. Even
if thunderstorm conditions look quiet, the strike feed may remain unknown.

The required-action field is a rule-based operational screen, not an ML output.
Cached data retain their original source times; a newly generated assessment
does not make an old weather observation new. Alert records preserve evidence,
creation/update times, active/resolved state and revision.

The default assessment display lifetime is 900 seconds; normal refresh becomes
due after 300 seconds. Missing providers can make retry due earlier without
expiring the assessment after 60 seconds. `freshness.refresh_due` and `.stale`
are different states. Source observation, retrieval and expiry remain independent.
ML score expiry is recalculated on each read, including model-output POST responses:
expired/stale display scores are null, while `retained_predictions` and row
`original_score` preserve historical values. See the exact temporary target-window
policy in [MODEL_HANDOFF.md](MODEL_HANDOFF.md).

## Notifications and reports

Subscribe to `GET /api/sites/{id}/events` using Server-Sent Events. Events include
`alert_created`, `alert_updated`, `alert_resolved`, `crew_report_created`,
`crew_report_resolved`, and `briefing`. Each event has an integer ID and site_id.
Store the highest processed ID; reconnect with `?after=<id>` or Last-Event-ID.
Deduplicate by event ID because network delivery is at least once. Events are
durable in SQLite and only the requested site's events are returned.

```javascript
const events = new EventSource(`${api}/api/sites/${siteId}/events?after=${lastSeenId}`);
for (const kind of ['alert_created', 'alert_updated', 'alert_resolved',
                    'crew_report_created', 'crew_report_resolved', 'briefing']) {
  events.addEventListener(kind, event => {
    const notification = JSON.parse(event.data);
    // Upsert by event/report/alert ID; persist event.lastEventId per site.
    showNotification(notification);
  });
}
// Close when the viewer leaves this site, then subscribe to the next site's URL.
```

`GET /api/sites/{id}/notifications?after=0&limit=100` supports inbox/replay with
`next_cursor`. In-app delivery means an event is stored and offered to viewers;
it does not claim the user read it or that a closed browser received a push.

To submit a crew report:

1. `POST /api/sites/{id}/photos` with raw JPEG, PNG or WebP bytes and its image
   Content-Type (not multipart). Maximum 5 MiB/20 million pixels. The backend
   validates/re-encodes the image and returns `photo_reference` and `photo_url`.
2. `POST /api/sites/{id}/reports` with the JSON below. The report timestamp is
   assigned by the server; coordinates are the crew's actual map pin.
3. Other viewers receive `crew_report_created` automatically through SSE.

```json
{
  "hazard_type": "wildlife",
  "description": "Alligator beside the equipment access road",
  "latitude": 25.75,
  "longitude": -80.7,
  "photo_reference": "REPLACE_WITH_UPLOAD_REFERENCE"
}
```

This is a format illustration, not a seeded report. The photo reference is optional;
when supplied it must belong to this site. Photo URLs are relative to the API base.
`GET /api/sites/{id}/reports?status=active` lists open reports. Omit status for history.
`POST /api/sites/{id}/reports/{report_id}/resolve` preserves the original pin/photo
and marks it resolved; resolving twice does not send duplicate notifications.

To discover observations from previous or neighboring projects, use
`GET /api/reports/nearby?latitude=25.75&longitude=-80.7&radius_km=10&limit=50`.
This searches report map pins across all sites and includes both active and
resolved reports by default. Add `status=active` or `status=resolved` to filter.
Coordinates must be finite and within their usual latitude/longitude bounds;
`radius_km` must be greater than zero and at most 100, and `limit` must be 1–100.
Results are ordered by distance, then newest report time. Each report includes
`distance_km`, `original_site`, `provenance`, its original `site_id` and map pin,
`report_time`, `status`, `resolved_at`, `photo_reference`, and `photo_url`.
These are crew observations, not verified current hazards; retrieval assigns
neither a severity nor an ML probability. Existing site-specific endpoints and
notifications remain scoped to the original site.

## Briefings, gauges, and the model boundary

`PUT /api/sites/{id}/timezone` accepts `{"timezone":"America/New_York"}`; it can
also be supplied during `POST /api/sites`. Otherwise the weather provider discovers
it. Explicit configuration overrides weather discovery.

`POST /api/sites/{id}/briefings/preview` previews tomorrow's briefing in the site
timezone, using the latest saved assessment. Optional
`{"target_date":"2026-11-01"}` selects a calendar date. A preview creates no saved
briefing or notification and never consumes scheduled delivery, even if a report
has already been delivered for that date. Its response includes
`preview: true`, `delivery_mode: "preview"`, and
`briefing.delivery.consumes_scheduled_delivery: false`.

`POST /api/sites/{id}/briefings` is **actual manual delivery**, not a preview.
It stores the briefing and publishes its in-app notification once per site/date;
the response says `delivery_mode: "actual_delivery"` and
`briefing.delivery.consumes_scheduled_delivery: true`. Automatic delivery runs at
18:00 local. An earlier actual manual delivery intentionally consumes that date's
delivery; an early preview does not, so the evening report uses updated saved data.
Repeated actual delivery requests return the saved report with `created: false`.

`GET /api/sites/{id}/briefings` returns saved reports. Briefings include the next
local calendar day's rule forecasts, highest-risk periods, available model scores,
environmental forecasts, active safety actions, unresolved reports, and explicit
missing-data notices. ML absence never blocks them. On reads, source freshness
and ML target validity are reevaluated; stale or expired display scores are null,
while original predictions remain in `ml_predictions.retained_predictions` and
per-hour `original_score`. Generating or reading a briefing does not refresh
weather, and the original assessment/source, prediction target, generation, and
delivery timestamps remain visible. HEFS river-threshold probabilities appear
separately in `river_forecast`, retaining their original target times or periods;
they are not repeated across hourly site forecasts or interpreted as site-flood
probabilities.

`GET /api/sites/{id}/river` exposes the nearby gauge and evidence.
`PUT /api/sites/{id}/river` requires a confirming evidence review when
`exposure_confirmed` is true. A body containing only the boolean now returns 422.
For a genuinely reviewed site, use this format (the content below is illustrative):

```json
{
  "gauge_id": "MEMT1",
  "exposure_confirmed": true,
  "review": {
    "decision": "confirmed",
    "river_name": "Mississippi River",
    "evidence_type": "hydraulic_review",
    "evidence": "Replace with site-specific evidence establishing connection and its limitations.",
    "source_urls": ["https://example.org/replace-with-actual-reviewed-document"],
    "reviewed_by": "Replace with actual reviewer"
  }
}
```

Allowed evidence types are `official_facility_connection`, `hydraulic_review`,
and `floodplain_review`. This is not a verified Memphis-site example. The API
checks gauge existence and stores the review tied to site coordinates and gauge;
it cannot independently certify the reviewer's judgement or identity. Existing
manual choices are retained. `GET /api/sites/{id}/river/exposure` exposes the
current record. Changing coordinates/gauge invalidates it; setting
`exposure_confirmed: false` withdraws confirmation. Old booleans without supporting
reviews are unconfirmed. Bankhead's bundled operator/USACE/NOAA evidence validates
that real facility's river connection, with inundation and HEFS-data limits explicit.

Unconfirmed nearby gauge flood categories remain evidence only and cannot alone
create a site stop-work alert. Flood evidence distinguishes `direct_site_rule_level`,
`river_gauge_relevance`, and `nearby_river_conditions.used_for_site_escalation`.
Independently applicable NWS/rainfall rules still generate alerts.

`GET /api/sites/{id}/hefs`, `PUT /api/sites/{id}/hefs`, and
`POST /api/sites/{id}/hefs/refresh` expose the separate forecast adapter.
Associations require matching gauge/location IDs and explicit evidence; no
proximity-based exposure confirmation is performed. `river_forecast` contains
`individual_times` and `period_maxima`. Each row includes issuance, target/period,
category, threshold value/units, original quantiles, availability and freshness.
`probability` is null for quantile-derived results; render `probability_bounds`
as an interval on the **0–1 scale**. `[0,0.05]` means **at most 5%**, not exactly 5%.
Do not copy a period probability into hourly map/chart points or label it the
probability that the jobsite floods. Missing thresholds/bounds remain unavailable.
Stale bounds are historical evidence. [HEFS_NOTES.md](HEFS_NOTES.md) documents
the live BIFM5/SSTG verification and BLDA1/QINE limitation.

Use `river_forecast.applicability` and `.site_relevance` independently of forecast
availability. `context_only` cannot be presented as a relevant site flood forecast.
`verified_river_context` establishes documented connection, not an inundation
probability or a site-specific flood damage threshold.

`GET /api/sites/{id}/lightning/strikes` returns the measured feed/countdown state.
`POST /api/sites/{id}/lightning/verify` makes a real provider request using the
server environment key. It does not accept credentials or test strikes in its body.
`provider_verification.provider` identifies the selected Xweather or Weather Company
adapter. `provider_verification.feed_access` distinguishes `not_configured`, `not_verified`,
`previously_verified` and `verified_current`; `.strike_detection` separately says
`observed` or `not_yet_observed`. A successful empty feed verifies access only.
Missing credentials, including removing them after success, make coverage unknown
immediately; an outstanding deadline and alert survive. An expired deadline with
unknown coverage is still not clearance. Check availability/status, not HTTP 200
alone: the verification endpoint returns diagnostic state even when access fails.
For Xweather, a successful first poll has short coverage and normally returns
`status: "unknown"` until overlapping polls cover 30 minutes. A qualifying strike
still stops work immediately. Display `coverage_complete` independently of
`feed_access`; an entitled endpoint is not a completed all-clear window.

`GET /api/sites/{id}/ml-input` and `POST /api/sites/{id}/ml-output` remain the
teammate adapter boundary. See [MODEL_HANDOFF.md](MODEL_HANDOFF.md). No endpoint
generates sample scores or simulated strikes. Xweather feed access is verified
with a valid empty query; actual nearby strike detection remains unobserved live.
Tests exercise the countdown using clearly labeled isolated fixtures.

## Response examples and validation

[Normal](docs/examples/normal.json), [dangerous](docs/examples/dangerous.json),
[stale](docs/examples/stale.json), and [unavailable](docs/examples/unavailable.json)
contain **test-only illustrative** excerpts from the actual serializers (duplicate
legacy fields omitted and hourly arrays shortened for readability). All
four work without ML. “Normal” means no known high hazard, not complete lightning
coverage. Regenerate with `python -B tests/export_examples.py`.

[Expired model target](docs/examples/expired-model.json) shows null display scores
with original synthetic test output retained. Recorded NOAA excerpts show
[BIFM5 stage bounds](docs/examples/hefs-bifm5-recorded.json),
[the same forecast after source expiry](docs/examples/hefs-stale.json), and
[BLDA1 missing flow thresholds](docs/examples/hefs-blda1-unavailable.json).
Each file identifies the data provenance and example clock. The forecast examples
verify provider/adapter behavior, not exposure of any application site.
[Bankhead connection](docs/examples/bankhead-river-connection.json) separately shows
the actual plant's documented river link and the remaining HEFS-data limitation.

Tests cover ML absence, alerts, photo reports and history, site-scoped notifications,
briefing delivery and DST dates, coordinate validation, source failure, duplicate
strikes, hold resets, persistence, and the rule-based map order.
