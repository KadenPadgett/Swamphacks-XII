# Verification record — 2026-10-17

## Database repair completed

The damaged root file was restored from the integrity-checked recovery after
confirming its hash still matched the original manifest and no backend writers
were running. A fresh original backup and audit were saved. The two unreadable
assessments (sites 14 and 26) were regenerated from live public providers, and the
healthy result was copied to the previously absent configured runtime path.

Both `sites.db` and `data/runtime/sites.db` now pass `PRAGMA integrity_check` and
`PRAGMA foreign_key_check`, with **61 sites and 61 JSON-readable assessments**.
API smoke checks passed for health, sites, portfolio and both regenerated details.
The site 26 river lookup timed out; its source is unavailable rather than invented.
The other saved assessments retain their original history/freshness.

Full Windows suite: **83 tests passed in 14.624s**, zero failures/errors/skips.
Two new recovery tests verify backup preservation and refusal to overwrite newer
user data. Earlier WSL results below precede the repair utility; the repaired live
files were verified using Windows SQLite and the application API with writers stopped.
See [DEPLOYMENT.md](../DEPLOYMENT.md) for exact backup/audit paths and safe storage.

## Latest: Xweather access and adapter

Current full-suite result: **81 tests passed**, zero failures/errors/skips,
Windows **17.258s**, WSL Ubuntu **9.319s**. Nine new isolated Xweather tests cover
credentials/provider selection, documented requests and empty warnings, measured
CG/IC parsing, duplicate/reset/restart behavior, continuous coverage warmup and
outage gaps, pagination failure, API/schema failure, rate-limit cooldown,
authenticated-URL log redaction, and provider switching.

Live access succeeded with the user's locally stored credential: HTTP 200,
`success: true`, empty `response`, `warn_no_data`, at 25.75/-80.7 within 10 miles.
The production adapter's exact bounded-time request also returned available with
zero validated events and a 240-second coverage window
(2026-10-17T21:48:48Z–21:52:48Z). No production state was written by these probes,
and no actual nearby strike detection is claimed. No broad recurring live polling
was started. These results supersede earlier notes that no lightning credential
was available; Weather Company entitlement remains unverified and unnecessary
when Xweather is selected.

The local credential is now in `PLANIT_XWEATHER_API_KEY`, the legacy Weather
Company setting is empty, and `PLANIT_LIGHTNING_PROVIDER=xweather` is selected.
The real credential was never printed or put into the public template. Git
`check-ignore` confirms `.env` is ignored, `ls-files` confirms it is untracked,
and an in-memory check found no credential or either component in staged or
unstaged diffs. `.env.example` contains only blank credential placeholders.

Xweather's short history does not authorize an immediate all-clear. The adapter
accumulates overlapping successful windows in SQLite and resets coverage on a
gap, while retaining the existing ten-mile/30-minute stop-work state machine.
Incomplete pagination can emit stop-work from validated events but never clearance.
Setup, official documentation links and quota guidance are in
[DEPLOYMENT.md](../DEPLOYMENT.md).

## Follow-up: river connection and lightning verification

Final full-suite results: **72 tests, zero failures/errors/skips** on both
Windows (**16.943s**) and WSL Ubuntu (**9.414s**), using discovery with `-q`.
Eight new tests cover documented reviews, Bankhead import/idempotence, legacy
booleans, coordinate/gauge changes, withdrawals, context-only HEFS data, missing
keys, safe entitlement errors, preserved holds and empty-feed verification.
Nine response examples now include the actual Bankhead connection record.

Bankhead Dam (EIA plant 2) now has a documented facility connection to the Black
Warrior River, supported by operator/USACE documents and a fresh **HTTP 200** NOAA
BLDA1 response identifying the same dam/river (observation at 2026-10-17T19:30:00Z).
This is evidence of river relevance beyond proximity; it does not establish a
work-area floodplain, inundation probability or local damage threshold. The
record is imported once for the exact plant identity and coordinates, respecting
manual choices and withdrawals. BLDA1 still lacks usable direct HEFS flow thresholds.

Public confirmation requires persisted evidence, sources and reviewer. Old
booleans cannot alone confirm relevance or escalate flooding. Reviews invalidate
on changed site coordinates/gauge. Responses expose review validity and HEFS
`context_only` applicability. Assessment revision 5 invalidates older assessments.

The new lightning verification POST checks the real adapter with a shared per-site
lease and exposes feed access separately from observed strikes. Removed or missing
credentials force unknown coverage immediately; saved holds/alerts survive.
An empty successful response verifies access only. All new lightning tests use
isolated mocked data: no entitled key or live Weather Company strike is claimed.

This follow-up supersedes the earlier test totals and the unverified Bankhead
connection in the previous verification record below.

## Previous verification

Preserved the current backend implementation and fixed the failing model-route
test by setting its serialization clock to the fixture target time. The same test
then advances six minutes and verifies the POST response has null current score,
stale availability, and the original 7.5 in retained history. Prediction expiry
was not disabled, lengthened, or bypassed to make the test pass.

## Complete suite

| Environment / command | Exact result |
| --- | --- |
| Windows `.venv\Scripts\python.exe -B -m unittest discover -s tests -v` | **64 tests in 11.733s — OK**, zero failures/errors/skips |
| WSL Ubuntu `.venv-wsl/bin/python -B -m unittest discover -s tests -v` | **64 tests in 7.109s — OK**, zero failures/errors/skips |

The prior 49 tests all pass. Fifteen new HEFS tests cover recorded real API payloads,
conservative quantile bounds/ties, malformed quantiles, missing thresholds and units,
BLDA1's missing flow thresholds, separate six-hour/period targets, explicit
associations, issuance pinning/mismatch, source expiry, saved-data retention after
outage/cache eviction, unsupported parameters, and independent scheduled HEFS jobs.
All database mutations and synthetic edge cases are confined to temporary tests.

`python -B tests/export_examples.py` regenerated eight labeled JSON examples.
All eight parse successfully. OpenAPI contains the HEFS read/association/refresh,
briefing-preview, and nearby-report paths documented in the handoff. Git whitespace
checks pass. Starlette emits a test-client/httpx deprecation warning; it did not
affect these results. Docker build and public deployment were not performed.

## Live provider verification

- **BIFM5/SSTG:** HEFS schema, location list, map quantiles and hydrograph quantiles
  returned HTTP 200. Actual FT units and NOAA minor/moderate thresholds 16/18 FT
  yielded conservative bounds `[0,0.10]` over the supplied five-/ten-day periods
  and `[0,0.05]` at the supplied six-hour hydrograph times. The live normalizer
  reported 110 fresh bound rows at capture, with elapsed targets stale. Point
  probabilities remain null. These bounds describe river thresholds, not a
  jobsite flood probability or an ML hazard index.
- **BLDA1:** location lists QINE but no SSTG; SSTG map request returned 404.
  QINE products returned 200/CFS, but direct flow thresholds are null. The adapter
  returns unavailable probability information. No stage-to-flow conversion,
  calculated-flow threshold assumption, percentage or model score was fabricated.
- **NWPS:** schema and MEMT1 detail returned 200, including actual units, categories
  and observation timestamps. Collection requests initially returned 429 (body
  advertised 10 requests/five minutes); the final full-catalogue probe timed out.
  A successful full national catalogue fetch remains unverified. The old 38 generic
  failures lack HTTP codes, so they cannot all be retrospectively attributed to 429.
- **Measured lightning:** no entitled provider credentials are configured. Adapter,
  coverage, persistence and countdown are tested; live strikes are not verified.

Raw recorded HEFS responses are under `tests/fixtures/hefs/`; the probe's newer
captures are under ignored `.cache/`. Reproduction, source links, methods and
time semantics are in [HEFS_NOTES.md](../HEFS_NOTES.md).

## Remaining requirements and local state

The ML teammate must provide a trained model and settle the target/horizon,
preprocessing, calibration and feature contract. The API still expects 0–10;
under the proposed likelihood interpretation the model service converts 0.70 to
7.0. Rule levels, alerts, reports, briefings and ranking work without that model.

A usable HEFS parameter tied to a validated site remains necessary before claiming
a site-specific probability pilot. BIFM5 verifies the provider/adapter calculation
only; Bankhead's connection is now documented but its threshold gap remains.
Lightning entitlement and hosting/production
frontend origin remain pending; no public backend URL or teammate sign-off is claimed.
Full-catalogue NWPS access still needs a successful live check.

The root database's corruption and the two unreadable assessments were repaired
in the later operation documented at the top of this record. The damaged original
and older recovery remain preserved for audit. See [DEPLOYMENT.md](../DEPLOYMENT.md)
for current healthy paths and WSL-native storage startup.

See [FRONTEND_HANDOFF.md](../FRONTEND_HANDOFF.md) for ranking, timestamps, null
handling, events, report history, preview/manual delivery and response examples;
[MODEL_HANDOFF.md](../MODEL_HANDOFF.md) documents the unchanged 49-sample boundary.
