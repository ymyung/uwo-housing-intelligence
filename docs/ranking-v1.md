# Explainable listing ranking v1

## Scope and boundary

Ranking v1 is a deterministic, versioned, listing-level batch calculation. It
uses normalized Stage 4 listing fields and the current trusted accessibility
profile already stored in PostgreSQL. It does not call the scraper, AI models,
geocoders, OpenTripPlanner, or any other external service.

The stored result is intended to be the ranking boundary for the next API
phase. API query integration and frontend presentation are deliberately out of
scope for this version. The earlier in-memory API ranking implementation is not
the persistence contract described here.

Scores are calculated at listing grain because separate advertisements at one
property can have different rents, room counts, lease contexts, or listing
identity. Property-level walking, cycling, and transit evidence is shared only
as an input. Two listings are never merged merely because they share a
property or address.

## Configuration and versioning

The complete scoring policy is in `config/ranking-v1.toml`. The configuration
declares the ranking version, eligible price range, comparable cohort minimum,
component weights, curve parameters, transit period weights, exact trusted
accessibility profile, database environment variable, and artifact directory.

Every run and listing score stores:

- `ranking_version`
- a SHA-256 configuration fingerprint
- a SHA-256 input fingerprint
- a stable output fingerprint at run level
- a structured explanation
- the calculation timestamp and ranking-run identity

Timestamps do not participate in stable output fingerprints. The same inputs
and configuration therefore produce the same scores and fingerprints.

## Eligibility and missing data

Only `active` and `relisted` listings are considered. A listing must have a
linked property and the exact configured, complete accessibility profile to
receive accessibility components. The statuses are:

- `ranked`: all required inputs are present and an overall score is available.
- `partial`: trusted accessibility is available, but a required listing input
  such as normalized monthly rent is missing or implausible. Available
  components remain visible, but the overall score is null.
- `excluded`: the listing is inactive, has no property, or lacks the complete
  trusted accessibility profile. Overall and component scores are null unless
  the required trusted inputs genuinely exist.

Missing required components never cause weight redistribution. This prevents
an incomplete listing from gaining an advantage simply because a weak or
unknown component disappeared.

Neighborhood amenity/POI inputs are not available in the current database.
`amenity_score` is therefore null, has a configured weight of zero, and is
reported as system-unavailable. Listing amenity checkboxes are not substituted
for neighborhood access. `data_quality_score` is also null and unweighted:
completeness controls eligibility and confidence metadata rather than being
treated as housing quality.

## Value score

The market population contains current, plausibly priced listings with rental
structure, housing type, and bedroom count. The rental structure distinguishes
per-bedroom advertisements from whole-unit advertisements.

For each listing, the narrowest cohort with at least the configured minimum
count and a non-zero interquartile range is selected in this order:

1. rental structure, housing type, bedrooms, and lease context
2. rental structure, housing type, and bedrooms
3. rental structure and housing type
4. rental structure
5. the relevant overall market

The selected cohort and fallback level are stored in the explanation. Given
monthly price `p`, cohort median `m`, cohort IQR `q`, and configured points per
IQR `k`, the unclipped value score is:

```text
50 - k * ((p - m) / q)
```

The score is clipped to the configured 5–95 interval. The explanation also
stores cohort count, P25, median, P75, price delta from median, and empirical
price percentile. Original and normalized prices remain in the Stage 4 source
tables and are not rewritten by ranking.

## Campus-access score

Campus access uses routed walking and cycling duration from the exact trusted
accessibility profile. Each mode uses this bounded inverse curve:

```text
100 / (1 + (minutes / midpoint_minutes) ^ shape)
```

The configured walking and cycling component weights are then applied. The
component is intentionally independent from transit usefulness.

## Transit score

Transit is calculated separately for all six configured periods:

- weekday morning commute
- weekday midday
- weekday evening commute
- weekday late evening
- Saturday daytime
- Sunday daytime

An available itinerary combines bounded duration, walking share, transfer
count, and observed duration-range reliability. Available samples,
walking-better-than-transit samples, and deterministic no-route samples are
averaged over the number of requested departures. Walking-better-than-transit
receives the small configured transit credit; deterministic no-route receives
zero. Valid service in other periods remains independently scored.

Provider reason codes and quality status remain in the explanation. Review
warnings are not silently converted into high confidence, and technical
failures are not relabeled as deterministic no-route outcomes. The six period
scores are combined with explicit weights from the configuration file.

## Overall score and explanation

For `ranked` listings, the overall score is the configured weighted sum:

```text
0.45 * value + 0.35 * campus_access + 0.20 * transit
```

All scores are bounded to 0–100 and rounded to two decimal places. Each stored
explanation includes the status, overall and component scores, weights, value
cohort signals, routed campus durations, period-level transit evidence,
eligibility reasons, accessibility warnings, listing review flags, unavailable
component declarations, fingerprints, and timestamp. It excludes raw provider
responses, credentials, environment data, and listing-description sentiment.

## Persistence and idempotency

Migration `20260809000100_create_listing_ranking_store.sql` adds:

- `housing_ranking_runs` for execution state, fingerprints, counts, timing
  summary, and errors.
- `housing_listing_scores` for current and historical listing-level results.

There is one current row per `(listing_id, ranking_version)`. Current ranked
lookups, status filters, listing history, and run history have dedicated
indexes. Foreign keys preserve Stage 4 listing and run identity, and row-level
security is enabled consistently with the existing private pipeline tables.

Persistence compares each new listing input fingerprint with the current
versioned row:

- unchanged input: no score-row write
- changed or new input: supersede the previous current row and insert one new
  current row in the same transaction
- removed/inactive source listing: not silently deleted; lifecycle remains a
  Stage 4 concern and historical scores remain available

A repeated run is still recorded for auditability, but an identical input set
does not churn listing score history.

## Operator workflow

Initialize the repository's documented local environment first, then apply the
reviewed additive migration through the existing local migration workflow.
The ranking commands are:

```powershell
.\.venv\Scripts\python.exe -m scripts.compute_listing_rankings dry-run
.\.venv\Scripts\python.exe -m scripts.compute_listing_rankings run
.\.venv\Scripts\python.exe -m scripts.compute_listing_rankings summary `
  --run-id <ranking-run-id>
```

Dry-run performs feature extraction and scoring, writes only the ignored local
validation artifact, and reports expected database changes. `run` records the
database run and writes ignored artifacts under `data/ranking-runs/<run-id>/`.
`summary` reads a specific immutable run manifest without recomputing scores.

Relevant validation commands are:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_ranking_v1.py `
  tests\test_ranking_migration.py -v
.\.venv\Scripts\python.exe -m pytest tests\test_ranking_v1_postgres.py `
  -m postgres -v
.\scripts\test.ps1 fast
.\scripts\test.ps1 normal
.\scripts\test.ps1 full
git diff --check
```

Ranking run artifacts and validation reports are intentionally ignored. They
must not be committed with real listing data.
