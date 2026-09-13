# MVP review triage

`scripts.triage_mvp_reviews` is a read-only evidence audit. It reads a captured
Stage 0–3 run, the exported City of London shadow-match audit, and current local
PostgreSQL product metadata. It cannot scrape, call AI, geocode, approve/import
a run, or write to PostgreSQL.

Review items have two independent dimensions:

- Evidence/disposition: `RESOLVED_DETERMINISTIC`, `LEGITIMATE_UNKNOWN`,
  `SOURCE_AMBIGUOUS`, `SOURCE_CONFLICT`, `GEOCODE_CONFIDENCE_REVIEW`,
  `PIPELINE_DEFECT`, `MVP_BLOCKING`, or `POST_MVP_REVIEW`.
- MVP severity: P0 is a false or unusable core product claim, P1 is important
  before a demo, P2 is an honest quality limitation, and P3 is post-MVP work.

A review flag is not proof that data is wrong. Unknown is preserved when local
source evidence does not establish a value. City coordinates remain shadow
evidence only and are never promoted by this command.

Run against the local development database after loading its established
environment configuration:

```powershell
python -m scripts.triage_mvp_reviews `
  --run-dir data/runs/<run-id> `
  --database-url-env ACCESSIBILITY_DATABASE_URL
```

Outputs are written beneath `data/mvp-review-triage/<run-id>/` and are ignored
by Git. `summary.json` contains aggregate findings; the CSV files contain the
full review inventory, bounded deterministic samples, prioritized map checks,
and the demo shortlist.

The generated map disposition is an internal validation boundary, not a City
coordinate promotion. Its equivalent product meanings are:

- `clearly_safe` (`DEMO_SAFE`): canonical coordinates pass current map-ready
  checks without a known high-risk provider/City disagreement.
- `displayable_with_known_limitation` (`DISPLAY_WITH_LIMITATION`): a coordinate
  exists, but current QC or shadow distance warrants a visible limitation.
- `exclude_from_map_demo` (`SUPPRESS_MAP_AND_ROUTE`): provider result type or
  bounded shadow evidence identifies a high-risk origin; retain the listing in
  non-map contexts.
- `missing_location` (`LOCATION_UNAVAILABLE`): no coordinate is available.

City non-match alone never makes a property unsafe, and these outputs never
overwrite Geoapify coordinates.

## Captured-source semantic rebuild

`scripts.rebuild_listing_fidelity` rebuilds Stage 1 and Stage 2 from captured
Western artifacts, reuses Stage 3 rows only when listing URL and normalized
address are unchanged, and compares the result with an explicitly selected
approved baseline. New AI and scrape calls are always zero. The default
external geocode cap is zero; a reviewer may explicitly set a small cap only
for civic addresses newly recovered by deterministic parsing:

```powershell
python -m scripts.rebuild_listing_fidelity `
  --source-run data/remote-runs/<captured-run-id> `
  --baseline-run data/runs/<approved-run-id> `
  --candidate-dir data/runs/<candidate-run-id> `
  --validation-dir data/mvp-fidelity-validation/<candidate-run-id> `
  --max-new-geocodes 1
```

The command loads the repository `.env` for the normal Geoapify abstraction,
writes provider results through the existing cache, records the exact call
count, and rejects unrelated address, identity, property-candidate, or geocode
changes. The candidate remains unapproved until the standard approval flow is
run separately.
