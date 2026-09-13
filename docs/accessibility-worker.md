# Accessibility routing worker

## Purpose and proof-of-concept boundary

The accessibility worker creates reusable exact walking, cycling, and representative transit profiles for a deliberately small set of reviewed properties and verified destinations. It extends the existing provider-neutral accessibility models, resolver, PostgreSQL profile/sample tables, API, ranking inputs, and UI labels. It does not scrape listings, enrich listing content, approve pipeline runs, or import Stage 4 listing data.

All active listings are intentionally out of scope. A small reviewed run is needed first to audit routing accuracy, graph/version compatibility, cache behavior, transit completeness, database transactions, and local resource use.

The first real routing run must remain limited to 10–20 reviewed properties and 1–3 verified hotspots. Expansion requires review of the quality report and confirmation that caching, versioning, and profile accuracy behave as expected.

## Architecture

```text
reviewed property CSV + verified hotspot JSON + fingerprinted routing bundle
  -> bounded input validation
  -> cache identity lookup
  -> local OpenTripPlanner provider adapter
  -> normalized route/sample objects
  -> deterministic aggregation and quality checks
  -> transactional profile/sample persistence
  -> atomic manifest, quality report, and focused review CSV
```

The implementation is split across:

- `backend/accessibility_inputs.py`: configuration, selection, hotspot verification, and bundle fingerprints.
- `backend/routing_provider.py`: provider-neutral OTP adapter and deterministic fixture provider.
- `backend/accessibility_worker.py`: cache-first work units, retries, sampling, aggregation, and persistence.
- `backend/accessibility_quality.py`: plausibility checks and stable reason codes.
- `backend/accessibility_runs.py`: atomic run manifests, JSONL results, quality reports, and resume state.
- `scripts/run_accessibility_worker.py`: preflight, run, resume, status, and summary commands.
- `scripts/run_accessibility_batch.py`: deterministic, resumable orchestration of arbitrary reviewed inputs through worker batches of at most 20 properties.

OTP payloads do not enter API DTOs, ranking data, or frontend models. Only normalized measures, compact route itineraries, and a small structured provider metadata object are stored. Raw provider responses and authentication headers are not retained.

## Required local routing data

Create `data/routing/` and provide:

1. An OpenStreetMap `.osm.pbf` extract covering London, Ontario.
2. A licensed London Transit Commission GTFS static `.zip` whose service dates include the configured reference week.
3. A reviewed OTP `router-config.json` (the repository contains `docker/opentripplanner/router-config.example.json`).
4. The OTP graph files produced from exactly those inputs.
5. A generated `build-manifest.json` containing exact-byte SHA-256 fingerprints and explicit router, street-network, schedule, and build versions.

These files and `data/accessibility-runs/` are ignored. Do not commit downloaded OSM/GTFS data, graph files, local property selections, or manifests. The committed `config/accessibility-hotspots.json` contains only the Western coordinate already established by this repository. Add destinations only after independent coordinate and provenance review.

Copy the example configuration and property header:

```powershell
Copy-Item config\accessibility-worker.example.toml config\accessibility-worker.toml
Copy-Item config\accessibility-poc-properties.example.csv config\accessibility-poc-properties.csv
New-Item -ItemType Directory -Force data\routing
Copy-Item docker\opentripplanner\router-config.example.json data\routing\router-config.json
```

Populate the private property CSV with 10–20 database property IDs, normalized addresses, verified coordinates, and `approved`, `reviewed`, or `verified` status. The worker refuses missing/out-of-bounds coordinates, duplicate or unknown IDs, unreviewed rows in reviewed-only mode, unknown/unverified/inactive hotspots, and selections over 20 properties or 3 hotspots unless `--allow-larger-run` is explicitly supplied. It never discovers or selects all active listings.

## Input versions and fingerprints

Build the graph, then generate the manifest from the exact files used:

```powershell
.\.venv\Scripts\python.exe -m scripts.build_routing_manifest `
  --config config\accessibility-worker.toml `
  --router-version otp-2.6.0 `
  --network-version london-osm-2026-08 `
  --schedule-version ltc-gtfs-2026-fall `
  --graph-built-at 2026-08-04T12:00:00-04:00
```

Use identifiers supported by the actual input provenance; the examples above are illustrative and must not be copied without verification. Compatibility is based on recorded SHA-256 values and explicit versions, never filenames alone. Editing OSM, GTFS, router configuration, or hotspots invalidates preflight until the graph is rebuilt and the manifest is regenerated.

## Optional local OpenTripPlanner container

The compose file is an optional development harness; automated tests never start or contact OTP and never download routing data. Pin `OTP_IMAGE` to the reviewed OTP build used for the graph. Allocate about 8 GB RAM and sufficient disk for the input extract and graph.

```powershell
$env:OTP_IMAGE = "opentripplanner/opentripplanner:2.6.0"
docker compose -f docker-compose.routing-test.yml --profile build run --rm otp-graph-builder
docker compose -f docker-compose.routing-test.yml up -d otp-router
```

OTP is bound to `127.0.0.1:8080`. The worker accepts only a localhost or `127.0.0.1` HTTP base URL. Confirm the image's GraphQL endpoint and graph-build flags when upgrading OTP; rebuild the graph and change version metadata after an engine or input change.

Run preflight after startup:

```powershell
$env:ACCESSIBILITY_DATABASE_URL = "postgresql://<local-disposable-user>:<password>@127.0.0.1:<port>/<database>"
.\.venv\Scripts\python.exe -m scripts.run_accessibility_worker preflight `
  --config config\accessibility-worker.toml `
  --properties config\accessibility-poc-properties.csv `
  --hotspot-ids western-main-campus
```

Stop without deleting local input data:

```powershell
docker compose -f docker-compose.routing-test.yml down
```

Remove generated graph files only after resolving and reviewing the exact paths under `data/routing/`; the project does not provide a destructive cleanup command.

## Representative transit sampling

The worker reuses all six periods from `backend/accessibility_periods.py`: four weekday periods, Saturday daytime, and Sunday daytime. Each has three fixed departures. `reference_service_week` must be a Monday; weekday samples use that Monday and weekend samples use its Saturday/Sunday in `America/Toronto`. There is no “leave now” path.

Each sample retains nullable arrival, duration, walking, waiting, in-vehicle, transfer, distance, origin/destination stop, route identifier, and normalized itinerary fields, plus provider and schedule/network versions. Missing fields remain null. The deterministic aggregation computes the observed range and chooses the real valid sample closest to the duration median, with departure time as its stable tie-breaker. The profile's duration, walking duration, transfers, distance, timestamp, and displayed itinerary all come from that same sample; it never combines averages into a synthetic route. By default at least two valid samples are required. A one-sample or zero-sample aggregate normally remains stale and retryable. A fully sampled period containing at least one valid itinerary and only deterministic `no_route` or `walking_better_than_transit` alternatives is instead current for the normal transit TTL while retaining its partial quality status, reason codes, samples, and manual-review decision. Pure `no_route` results and transient technical failures remain stale and retryable.

## Normalized route geometry

Migration `20260809000200_add_accessibility_route_itineraries.sql` adds a JSONB itinerary to profiles and samples, plus the representative transit sample timestamp. OTP's polyline5 geometry is retained once per leg rather than expanded into coordinate arrays or duplicated as GeoJSON. Every leg records its mode, duration, distance, point count, and encoded geometry. Transit legs may additionally record public route number/name, boarding and exit stops with coordinates, and scheduled timestamps. The domain model verifies transfer count against the number of transit legs and rejects malformed or implausibly large encoded geometry.

Direct walking and cycling profiles store their actual route itinerary on the profile. Transit samples each store their actual itinerary; the profile points to the selected representative sample. `walking_better_than_transit` and `no_route` outcomes retain their reason but do not fabricate bus geometry. A raw OTP query, response, internal plan object, or decoded duplicate geometry is never persisted.

## Cache and database behavior

A cache match requires property/origin identity and coordinate fingerprint, hotspot and hotspot fingerprint, mode and transit period, provider/profile, network version, transit schedule version, expiry, and non-stale status. A compatible current result skips the provider. A compatible partial result reuses exact departure samples and requests only missing departures. Changed coordinates, hotspot configuration, graph, provider profile, street network, or transit schedule create a distinct identity.

Expired current rows are archived as stale and replaced in the same database transaction. Incompatible rows are retained as historical records. A profile and all samples for one property/hotspot/mode/period unit commit together. A failure rolls the unit back and does not disturb its prior current profile. Worker run IDs, input fingerprints, quality state, normalized itineraries, and safe provider version metadata are stored in the profile/sample tables; no raw payload column is present.

The cache identity includes origin/property and hotspot fingerprints, provider profile, OTP/network version, and the transit schedule version. The route representation is also versioned through the provider profile (`otp-local-transport-v2`). A change to graph inputs or route parsing cannot silently reuse a v1 geometry-free row. Historical incompatible profiles remain queryable but are not returned as current.

Use a dedicated local database environment variable configured by `persistence.database_url_env`. The example uses `ACCESSIBILITY_DATABASE_URL`; the PostgreSQL test harness uses only `TEST_DATABASE_URL`. No API key is required for self-hosted OTP.

Property accessibility belongs to the durable property record, not an advertisement, so removing a listing does not delete its historical profile. Existing resolver, API, and ranking rules continue to decide whether exact, reused, estimated, stale, or unavailable accessibility may be shown.

## Dry run and bounded execution

Dry run validates and fingerprints local inputs, validates selection, resolves cache identities, and estimates work. It makes no routing-provider calls and no database writes, and creates no run directory:

```powershell
.\.venv\Scripts\python.exe -m scripts.run_accessibility_worker run `
  --config config\accessibility-worker.toml `
  --properties config\accessibility-poc-properties.csv `
  --property-ids 101,102,103,104,105,106,107,108,109,110 `
  --hotspot-ids western-main-campus `
  --modes walking,cycling,transit `
  --dry-run
```

After preflight and review of the dry-run estimate, run the same bounded selection without `--dry-run`:

```powershell
.\.venv\Scripts\python.exe -m scripts.run_accessibility_worker run `
  --config config\accessibility-worker.toml `
  --properties config\accessibility-poc-properties.csv `
  --property-ids 101,102,103,104,105,106,107,108,109,110 `
  --hotspot-ids western-main-campus `
  --modes walking,cycling,transit
```

Replace the illustrative IDs with reviewed local IDs. Do not use `--allow-larger-run` for the initial proof of concept.

## Run artifacts, resume, and recovery

Each real run writes an ignored `data/accessibility-runs/<run_id>/` directory containing `manifest.json`, immutable selection snapshots, normalized `route-results.jsonl`, JSON/CSV quality reports, and `review-required.csv`. Writes use temporary files and atomic replacement. The manifest includes Git state, versions, fingerprints, counts, cache/provider metrics, and completed work-unit keys; it contains no database URL, API key, `.env` data, authorization header, or raw OTP response.

```powershell
.\.venv\Scripts\python.exe -m scripts.run_accessibility_worker status --run-id <run_id>
.\.venv\Scripts\python.exe -m scripts.run_accessibility_worker summary --run-id <run_id>
.\.venv\Scripts\python.exe -m scripts.run_accessibility_worker resume `
  --config config\accessibility-worker.toml `
  --run-id <run_id>
```

Resume refuses changed bundle fingerprints or modes, skips recorded units, uses current profile cache hits, and reuses compatible transit samples. Only timeouts, connection resets, temporary router failures, HTTP 429, and server errors receive bounded exponential retries. Invalid inputs/responses, missing routes/graphs, version mismatches, database failures, and constraints are not retried indefinitely.

Each durable outcome also carries the worker-counter snapshot recorded immediately after that unit. If Windows allows the atomic outcome replacement but temporarily locks the manifest replacement, finalization and resume reconstruct counters from the outcome instead of losing accounting or repeating provider work.

## Bounded multi-batch orchestration

Use the batch runner when a reviewed CSV contains more than the worker's 20-property safety limit. The runner never passes `--allow-larger-run`; it validates unique property IDs, snapshots and fingerprints the source CSV, splits it deterministically, creates one worker run per batch, stops on failure by default, and aggregates manifests, quality rows, warnings, review rows, cache metrics, provider calls, and database writes.

Always estimate first. Dry run invokes the existing worker estimator separately for every bounded batch and makes no routing calls or database writes:

```powershell
.\.venv\Scripts\python.exe -m scripts.run_accessibility_batch dry-run `
  --properties <reviewed-properties.csv> `
  --hotspots western-main-campus `
  --modes walking,cycling,transit `
  --batch-size 20 `
  --output data\accessibility-scale-readiness\full-dataset-dry-run.json
```

After reviewing that estimate, execute the identical selection:

```powershell
.\.venv\Scripts\python.exe -m scripts.run_accessibility_batch run `
  --properties <reviewed-properties.csv> `
  --hotspots western-main-campus `
  --modes walking,cycling,transit `
  --batch-size 20
```

The command prints the parent `batch_run_id`. Inspect or recover it with:

```powershell
.\.venv\Scripts\python.exe -m scripts.run_accessibility_batch status --batch-run-id <batch_run_id>
.\.venv\Scripts\python.exe -m scripts.run_accessibility_batch summary --batch-run-id <batch_run_id>
.\.venv\Scripts\python.exe -m scripts.run_accessibility_batch resume --batch-run-id <batch_run_id>
```

Resume skips completed batches. For an interrupted worker batch, it discovers the exact selected-property snapshot even though the worker canonicalizes property order, then resumes only missing work units. Resume refuses a changed or missing source CSV. Use `--continue-on-failure` only when independent later batches should proceed despite a recorded failure; stop-on-failure is the safe default.

## Quality and review workflow

Checks cover positive/ranged duration and distance, walking/cycling speed, route-to-straight-line detour, transit completeness/transfers/walking share, provider mode/status, and snap distance when the provider supplies it. Stable reason codes include `duration_outlier`, `distance_outlier`, `impossible_speed`, `excessive_detour`, `insufficient_samples`, `unexpected_no_route`, `origin_snap_too_far`, `destination_snap_too_far`, `high_walking_share`, and `version_mismatch`.

Results are separated into `accepted`, `accepted_with_warning`, `manual_review_required`, and `failed`. Not every outlier is rejected and successful profiles need no manual review. Review `quality-report.json`, compare questionable trips in OTP or another independently reviewed local method, resolve every row in `review-required.csv`, and document whether warnings represent network topology, source coordinates, schedule coverage, or adapter errors before expanding the selection.

The collection listing API performs one batched profile read and exposes only a compact `transportation` summary without geometry. `GET /api/listings/{listing_id}/accessibility` exposes the six-period overview. Supplying `mode=walking`, `mode=cycling`, or `mode=transit&time_period=...` returns the exact persisted route on demand. The UI displays those normalized fields and never sees OTP payloads.

## Provenance, licensing, security, and limitations

Record the download URL, publisher, retrieval timestamp, licence/version, coverage, and SHA-256 for OSM and GTFS in local run notes. OSM-derived routing requires compliance with OpenStreetMap/ODbL attribution obligations. GTFS use must follow the transit publisher's terms. Record the OTP image digest/version and the reviewer/source for every hotspot. Stored fields are normalized durations, distances, transfers, stop identifiers, sample timestamps, compact encoded route legs, and provider/version metadata. The frontend retains OpenStreetMap attribution and presents transit times as static scheduled estimates.

The localhost adapter has no API-key path. Keep database credentials in the named environment variable or ignored `.env`, never TOML or run artifacts. Do not expose OTP or the disposable database beyond loopback. The worker does not validate real-world accessibility, construction closures, safety, fare, live vehicle data, wheelchair routing, or every GTFS service exception. Static schedules can diverge from observed trips.

Expansion beyond 20 properties requires a follow-up audit confirming: all first-run review rows are resolved; route plausibility is acceptable across near/far origins; transit periods match active service; sample completeness meets threshold; cache and partial resume avoid duplicate calls; graph and feed changes create historical replacements; transaction rollback and idempotency pass against the disposable PostgreSQL harness; run artifacts are secret-free; resource usage is measured; and OSM/GTFS/OTP provenance and licence obligations are documented.
