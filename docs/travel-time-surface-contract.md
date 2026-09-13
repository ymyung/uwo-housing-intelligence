# R5 travel-time-surface acceptance contract

The current evidence permits a **walking-only** numerical travel-time surface.
It does not make R5 an exact-route provider, enable bicycle or transit
surfaces, or add a ranking input. The reviewed settings are in
[`config/travel-time-surface.example.toml`](../config/travel-time-surface.example.toml);
the reproducible, ignored audit outputs live under
`data/travel-time-surface-validation/acceptance/`.

## Operational boundary

- The output is a rounded numeric map estimate, not an itinerary.
- Exact local OpenTripPlanner (OTP) routes remain authoritative for route
  details, geometry, and user-triggered routing.
- The maximum time is 120 minutes and display values round to 60 seconds.
- Walking excludes cells whose destination snap is farther than 400 m. The
  acceptance sample retained 85.368% of grid cells at that threshold.
- Bicycle and transit are explicitly disabled. A consumer must reject rather
  than silently expose either mode until a later acceptance run updates this
  contract.

## Evidence

The acceptance runner uses the fixed 14,803-cell 200 m EPSG:26917 grid, three
representative origins, and a deterministic 72-cell sample. It compares R5
against exact local OTP:

- Walk and bicycle: one exact direct OTP route per origin/cell.
- Transit: the median of seven exact OTP departures every 10 minutes across
  each inclusive 60-minute local window.
- Both engines apply the 120-minute surface cap before reachability is
  compared.

The six transit windows are weekday morning, midday, evening, late evening,
Saturday daytime, and Sunday daytime. R5 uses a separate compatibility
derivative only because its parser requires explicit `transfers.txt`
`transfer_type` values. The canonical GTFS is never modified. The derivative
changes only blank values to the GTFS default `0`, preserves extended
`24:00:00+` times, and validates the late-evening window against OTP without a
modulo-24 or service-date rewrite.

The current report concludes
`SURFACE_CONTRACT_READY_WITH_MODE_LIMITATIONS`: walking passed the 400 m
candidate threshold with no sampled one-sided reachability cases and p90
absolute error of 4.25 minutes. Bicycle and transit did not pass and are not
safe defaults.

## Revalidation and promotion

Run the isolated R5 study, then local OTP parity, after a change to OSM, GTFS,
R5/r5py, grid, snapping policy, or transit window semantics. Review all
ignored artifacts before changing this policy. The production implementation
must continue to:

1. Read this policy explicitly and reject disabled modes.
2. Persist source version, grid fingerprint, snap policy, mode, period, and
   calculation type with every surface artifact.
3. Keep exact OTP separate from numerical-surface estimates in API contracts.
4. Perform a fresh acceptance run and make a separately reviewed migration
   before exposing any new mode.

The validation harness is intentionally isolated from production code. See
`scripts/r5_surface_acceptance.py` and
`scripts/r5_acceptance_validate_otp.py` for the reproducible local workflow.
