# Routing data and GTFS lifecycle

## Active bundle

OpenTripPlanner 2.6 plans every route locally from the ignored files under `data/routing/`. OpenStreetMap supplies the street/path network and London Transit static GTFS supplies stops, routes, trips, calendars, and scheduled stop times. The application does not call OTP from the browser and does not support GTFS-Realtime.

The active graph must be treated as one versioned bundle:

- `ontario.osm.pbf`
- `london-transit.gtfs.zip`
- `router-config.json`
- `graph.obj`
- `build-manifest.json`

The manifest records exact-byte input hashes plus explicit router, network, and schedule versions. Accessibility cache identities include the network version, transit schedule version, provider profile, origin/hotspot fingerprints, and coordinates. A changed OSM file, GTFS file, router version, route-contract version, property coordinate, or destination invalidates the compatible cache identity; old profiles remain historical.

## Current inspected feed

As inspected on 2026-08-09, the configured feed reports:

- Publisher: London Transit
- Feed version: `2026 Spring - Fanshawe_20260421`
- Active service: 2026-05-03 through 2026-06-27
- Configured reference week: 2026-06-15
- Monday, Saturday, and Sunday reference dates: supported
- Feed SHA-256: matches the GTFS fingerprint in the graph manifest
- Freshness: expired as of 2026-08-09
- Product data kind: static scheduled estimate, never live departure information

The deterministic status command is:

```powershell
.\scripts\routing.ps1 gtfs-status `
  -Output data\transport-product-validation\gtfs-freshness-report.json
```

The feed's publisher metadata is not a verified current ZIP download endpoint. No official replacement source was established during this milestone, so the operational state is `CURRENT_GTFS_REFRESH_BLOCKED_BY_SOURCE`. Do not guess a URL, scrape an unofficial mirror, or overwrite the active feed merely to make freshness green.

## Safe candidate staging

After a human verifies an official London Transit GTFS URL or local ZIP and records its publisher, licence, retrieval time, and source URL, stage it without changing the active bundle:

```powershell
.\scripts\routing.ps1 gtfs-stage `
  -GtfsSource '<verified-official-url-or-local-zip>' `
  -DryRun

.\scripts\routing.ps1 gtfs-stage `
  -GtfsSource '<verified-official-url-or-local-zip>'
```

Staging rejects corrupt ZIPs, unsafe archive paths, missing core tables, invalid calendars/dates, feeds over 100 MB, and unsupported source schemes. It computes the exact SHA-256, effective service range, reference-week coverage, and whether a graph rebuild is required. The resulting directory stays below ignored `data/routing/staging/`; it is never promoted automatically.

Before building, choose a Monday actually covered by the candidate and create a candidate worker TOML whose OSM, GTFS, router config, and manifest paths all point to one isolated build directory. Copy the reviewed OSM and router config there; do not move the active files. Build and serve the candidate on a different loopback port. For example, after resolving `$candidate` to that reviewed directory:

```powershell
$env:OTP_IMAGE = 'opentripplanner/opentripplanner:2.6.0'
docker run --rm `
  -v "${candidate}:/var/opentripplanner" `
  --memory 8g `
  $env:OTP_IMAGE --build --save /var/opentripplanner

docker run --rm -d `
  --name uwo-otp-candidate `
  -p 127.0.0.1:8081:8080 `
  -v "${candidate}:/var/opentripplanner:ro" `
  --memory 8g `
  $env:OTP_IMAGE --load /var/opentripplanner --serve
```

Generate `build-manifest.json` using `scripts.build_routing_manifest` and the candidate TOML, with reviewed version labels rather than filename-derived claims. Then validate all of the following against port 8081:

1. Candidate `graph.obj` exists and is non-empty.
2. OTP starts cleanly and its GraphQL endpoint responds.
3. One reviewed property produces actual walking and cycling geometry.
4. The same property produces a transit itinerary with London stops/routes.
5. All six configured periods are valid for the new reference week.
6. The candidate manifest hashes match every candidate input.
7. A bounded worker smoke run (at most 20 properties) passes geometry and quality audit.

Stop and remove only `uwo-otp-candidate` after validation. Do not use the repository's active-directory graph-builder Compose command for a candidate because it mounts `data/routing/` read-write.

## Promotion and recovery

Promotion is a deliberate maintenance action, not part of `gtfs-stage`:

1. Stop the active OTP container.
2. Copy the entire working active bundle to a timestamped ignored backup directory.
3. Copy the already validated candidate bundle into `data/routing/` as one unit.
4. Start OTP in load-only mode and repeat GraphQL plus bounded walk/bike/transit checks on port 8080.
5. Run worker preflight and a dry run; confirm the new network/schedule versions cause expected cache misses.
6. Run focused, then bounded full recomputation only after review.
7. Run an identical second pass and require compatible cache hits with zero provider calls/writes.

If any post-promotion validation fails, stop OTP, restore the timestamped complete bundle, and restart load-only mode. Keep the prior graph until the new bundle and recomputed profiles have passed product review. Never commit `graph.obj`, GTFS/OSM inputs, manifests, staging directories, or backups.
