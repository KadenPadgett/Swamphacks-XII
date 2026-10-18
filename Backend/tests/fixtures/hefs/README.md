# Recorded public NOAA responses

These are unmodified provider JSON payloads, not synthetic forecasts or seeded
application data. They are used offline by `tests/test_hefs.py`.

- `BIFM5-SSTG-*`: HTTP 200 responses retrieved 2026-10-17 approximately 20:49 UTC,
  issuance 2026-10-17T00:00:00Z, stage units FT.
- `BLDA1-QINE-*`: HTTP 200 responses retrieved earlier on 2026-10-17,
  issuance 2026-10-16T12:00:00Z, flow units CFS; direct flow thresholds missing.

Source base: `https://api.water.noaa.gov/hefs/v1/`. The filename suffix identifies
`locations/`, `map-quantiles/`, or `hydrograph-quantiles/`. Queries use the filename's
location_id and parameter_id (parameter omitted for locations).
See [HEFS_NOTES.md](../../../HEFS_NOTES.md) for semantics, limitations, and reproduction.
Tests explicitly mutate copies only for malformed-data and missing-data scenarios.
