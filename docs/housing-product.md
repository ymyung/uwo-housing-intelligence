# Housing discovery product architecture

## Current architecture

The product remains separate from the Stage 0–4 pipeline and review workflow:

```text
Stage 0–3 pipeline → approved canonical artifact → Stage 4 importer
                                                   ↓
                                normalized PostgreSQL/Supabase tables
                                                   ↓
            ListingRepository → FastAPI service → React product UI
                     │                 │                  │
                     │                 ├─ ranking         └─ Map adapter
                     │                 ├─ hotspots             └─ Leaflet/OSM
                     │                 └─ travel provider
                     │                       └─ straight-line estimate
                     └─ fixture CSV for offline development
```

The existing Stage 4 boundary is `product_housing_listings`, a fail-closed view
over the latest normalized observation, ranking, and reviewed location policy.
No pipeline, review, approval, import, or migration code is called by the
product API.

The backend is FastAPI. `backend.repository.ListingRepository` separates API
behavior from Supabase and supports a CSV fixture for offline work. Supabase is
initialized lazily, so importing `backend.main` performs no network I/O and is
safe without credentials.

The frontend is React 19 with Vite. `ListingMap.jsx` is the only component that
imports Leaflet or React Leaflet. Product components exchange plain listing,
hotspot, coordinate, and route-shaped objects.

## Provider boundaries

`backend.domain` defines coordinates, locations, hotspots, route requests,
route results, travel-time results, geocode results, and provider metadata.
Core models never contain Geoapify, Google, Leaflet, or PostGIS objects.

`backend.providers` defines `GeocodingProvider`, `RoutingProvider`, and
`TravelTimeProvider` protocols. Current offline implementations are:

- `StraightLineEstimateProvider`: Haversine distance plus configurable walking
  and cycling speeds (defaults 4.8 km/h and 15 km/h).
- `UnavailableTransitProvider`: explicit `pending_provider` results.
- `FixtureRoutingProvider`: fixed results for tests and local development.

Current walking/cycling values are estimates based on straight-line distance.
They are not turn-by-turn route results. Transit and driving durations are null,
not zero, and use `pending_provider`.

Travel results carry origin, destination, mode, distance, duration, provider,
calculation type, timestamp, expiry, estimate flag, confidence, and status.
Statuses are `available`, `estimated`, `unavailable`, `pending_provider`,
`invalid_origin`, and `invalid_destination`.

## Map boundary

`frontend/src/map/MapAdapter.js` documents the product contract and provides a
mock for architecture tests. `ListingMap.jsx` is the current Leaflet adapter and
owns map initialization, tiles, listing/hotspot markers, deterministic
coordinate-bucket clustering, fit-to-results, selection, zoom state, optional
straight-line display, and React Leaflet cleanup.

No Google global or type leaks into the app. Public map configuration lives in
`frontend/src/config.js`: tile URL, attribution, centre, zoom, provider name,
travel speeds, and feature flags. It contains no secrets.

## Hotspots

`backend/hotspots.py` stores destinations separately from listings. The Western
main-campus point reuses the coordinate already established in this repository
and is active. Requested campus-building, Masonville, Richmond Row, and grocery
destinations are inactive placeholders with null coordinates and
`coordinate_verification_required` as their source. No coordinates were
invented.

Only active hotspots are returned normally. Inspect placeholders with
`GET /api/hotspots?include_inactive=true`. A placeholder needs a verified source
and coordinates before activation.

## API

Primary endpoints:

- `GET /api/health`
- `GET /api/listings`
- `GET /api/listings/{listing_id}`
- `GET /api/listings/{listing_id}/history`
- `GET /api/hotspots`
- `GET /api/listings/{listing_id}/accessibility`

Legacy `/health`, `/listings`, `/listings/map`, `/listings/{listing_id}`, and
`/stats` routes remain compatible.

Search supports text, min/max monthly price, exact bedroom count, housing type,
lease type, explicit sublet status, summer availability, gender, furnished,
utilities, parking, laundry, pet policy where normalized data exists, map
readiness, maximum straight-line hotspot distance, and data-quality status.
Sublet reads only `is_sublet`; summer reads a separate normalized field/category.
The API never mines descriptions at request time.

### Western-demo discovery intelligence

`roommates_wanted=true` is a deterministic convenience filter over normalized
`house_to_share` and `apartment_to_share` inventory. It never searches free
text. When combined with `housing_type`, both conditions apply, so an exact
share type can narrow the shortcut while `house` or `apartment` produces no
intersection.

`max_walk_minutes` and `max_transit_minutes` accept 1–180 minutes. Both filter
against current, unexpired, complete exact accessibility profiles for the
verified `western-main-campus` destination. Walking uses its all-day
representative duration. Transit uses the established
`weekday_morning_commute` representative duration. A missing, stale, partial,
or unavailable profile does not satisfy a threshold. With no threshold, those
listings remain discoverable under the existing listing and map-readiness
rules.

PostgreSQL evaluates the threshold with the indexed property/profile identity
before pagination. Supabase first obtains the matching property identities
from the persisted profile table and applies them to the listing query. The
frontend never filters the full profile cohort and ordinary search performs no
OTP or R5 work. The product presets are 15, 20, 30, and 45 minutes, chosen from
the current approved data distribution.

Results are paginated (default 50, maximum 200), bounded to 3,000 current
summaries, and deterministically sorted by `recommended`, `price_low`,
`price_high`, `distance`, or `newest`. List results omit descriptions. The detail
endpoint fetches full text only on selection. At larger inventory, the repository
adapter should push filtering/counting into SQL without changing the API or UI.

### Observation freshness and listing history

Collection rows include only truthful platform observation bounds:
`freshness.first_observed_at` and `freshness.last_observed_at`. They mean when
the platform first and most recently saw the stable source advertisement, not
when its landlord edited it. They are already columns of the batched product
view, so ordinary discovery makes no per-card history queries.

Selecting a listing performs one bounded history read (at most 51 observations)
and returns at most 20 newest, deterministically ordered, whitelisted source
events. `GET /api/listings/{listing_id}/history` exposes the same projection.
It never returns raw observation JSON, run IDs, fingerprints, parser rules,
confidence, AI evidence, or review artifacts. Ambiguous old history and pipeline
reinterpretations are omitted, and `last_meaningful_source_change_at` stays null
until a source change is provable.

The detail drawer shows first/last platform observation dates and renders a
small listing-history section only when public source events exist. There is no
card-level “Updated” badge in the current baseline because the retained local
history does not yet prove a landlord-origin update. This is intentional rather
than a missing-data fallback.

Stable identity remains `(source, source_listing_id)`. A complete authoritative
snapshot that no longer sees an ID advances the existing two-run removal policy;
the row, property, and observations remain. Reappearance of the same ID restores
the same identity and appends a reactivation observation. This boundary is ready
for periodic complete snapshots or deltas from a future authorized Western feed
without coupling that feed to change detection.

The disposable PostgreSQL demonstration is:

```powershell
$env:TEST_DATABASE_URL = "postgresql://uwo_housing_test:uwo_housing_test_only@127.0.0.1:55601/uwo_housing_test"
.\.venv\Scripts\python.exe -m pytest `
  tests\test_postgres_importer_integration.py::test_disposable_listing_change_removal_and_reactivation_demo `
  -m postgres -v
```

It imports listing `900001` at $850, imports the same identity at $800, applies
the established two-complete-absence removal threshold, and reactivates the same
identity. The test verifies current price, immutable observations, property
retention, active-search exclusion, and public price/reactivation history. It
uses only the disposable PostgreSQL test database and makes no source, AI,
geocoding, Supabase, or routing calls.

## Ranking

`backend/ranking.py` implements a deterministic, explainable score:

| Component | Weight |
|---|---:|
| Price within housing-type/bedroom comparables | 30% |
| Distance to selected destination | 25% |
| Documented amenities | 15% |
| Convenience/transit data | 10% |
| Data completeness and review status | 10% |
| Freshness | 10% |

Responses label the score subjective and include component scores, weights, and
an explanation. Missing values receive conservative partial scores and cannot
be perfect. Monthly prices below $300 or above $10,000 receive no price-value
credit. Thresholds and weights should eventually move to deployment config.

## Product behavior

Desktop has synchronized results/map panes and an on-demand detail drawer.
Mobile has a labeled list/map toggle. Marker selection selects and scrolls to
the card; card selection highlights and moves the map. Map movement does not
change filters.

Filters are represented in the URL and requests are debounced. Loading, empty,
validation, and API errors are explicit. Listings without coordinates remain in
the list and show “Address not map-ready.” A selected destination updates
straight-line distance and estimates. Transit/driving state that live routing is
not configured.

Saved IDs use a versioned local-storage adapter. No authentication is required.
Up to three loaded listings can be compared across price, bedrooms, type,
distance, amenities, availability, and quality. The storage module is
replaceable by an account repository later.

Cards and controls are keyboard accessible; Leaflet provides marker keyboard
support. Every listing is usable without the map. Focus is visible, state uses
text in addition to color, and reduced-motion preferences are respected.

## Local fixture demonstration

From the repository root, run the API with the committed listing and
accessibility fixtures:

```powershell
$env:HOUSING_FIXTURE_CSV = "tests/fixtures/accessibility_demo/listings.csv"
$env:ACCESSIBILITY_FIXTURE_PATH = "tests/fixtures/accessibility_demo/profiles.json"
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --port 8000
```

In a second terminal:

```powershell
Set-Location frontend
npm run dev
```

Open `http://127.0.0.1:5173`. These commands do not initialize Supabase or
PostgreSQL and do not invoke the scraper, enrichment, geocoder, importer, or a
routing service. The browser requests OSM tiles only when the UI is opened;
tile availability is not part of automated test success.

The fixture is schema-versioned JSON loaded through the same repository
interface used by the resolver. Its provider, provider profile, schedule
version, and network version are explicit. Fixture mode takes precedence over
`DATABASE_URL`, so using both fixture paths cannot initialize the persistent
repository accidentally.

| Listing | Request | Expected result |
|---|---|---|
| `demo-exact` | walking or cycling | Cached exact route |
| `demo-exact` | weekday-morning transit | Cached exact route with a 21–29 minute range and three samples |
| `demo-nearby` | walking or cycling | Nearby-location estimate referencing the exact source profile |
| `demo-same-stop` | weekday-morning transit | Same-stop transit estimate with a replaced walking connection |
| `demo-stale` | walking | Stale route profile, retained but not current |
| `demo-fallback` | walking | Straight-line estimate |
| `demo-fallback` | weekday-morning transit | Transit profile pending a provider |
| `demo-no-coordinates` | transit | Explicit unavailable result; listing remains visible |

The demo accessibility durations are deterministic fixture values for development and testing. They are not verified live routing results.

Smoke the provider-neutral API directly with:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
Invoke-RestMethod http://127.0.0.1:8000/api/listings
Invoke-RestMethod http://127.0.0.1:8000/api/hotspots
Invoke-RestMethod http://127.0.0.1:8000/api/accessibility/time-periods
Invoke-RestMethod "http://127.0.0.1:8000/api/listings/demo-exact/accessibility?mode=walking"
Invoke-RestMethod "http://127.0.0.1:8000/api/listings/demo-exact/accessibility?mode=transit&time_period=weekday_morning_commute"
```

For normal persistent development, clear both fixture variables and use the
existing Stage 4 environment configuration.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not postgres"
.\.venv\Scripts\python.exe -m compileall pipeline scripts backend
Set-Location frontend
npm test
npm run lint
npm run build
```

Tests inject in-memory repositories and deterministic providers. They do not
contact Google, Geoapify, Supabase, SSH, the scraper, AI, geocoding, or imports.
The API smoke test deliberately configures unusable database and Supabase
addresses and verifies that fixture mode initializes neither adapter.

## Persistent accessibility profile store

The accessibility profile store is durable location intelligence, not a
listing transit cache. A listing is an advertisement; a property is the stable
physical identity assigned by Stage 4; an origin is the exact property,
entrance, coordinate, or transit-stop access used by a route calculation.

Profiles reference `housing_properties`, not `housing_listings`. Stage 4 never
deletes properties when advertisements become inactive, and the accessibility
foreign key uses restrictive behavior rather than cascading. A removed listing
therefore leaves its property profiles intact, and a later advertisement matched
to that property can reuse them.

The disposable PostgreSQL lifecycle test proves this boundary with real foreign
keys: it marks the original listing `removed`, verifies the property and profile
remain, inserts a later advertisement for the same property, resolves the
existing profile, and verifies the reuse-history row. Profile upserts use a
partial unique cache identity, so repeated current writes update one row while
stale rows remain auditable.

The resolver applies this precedence:

1. A current, compatible profile for the same property.
2. A current profile for the same entrance or coordinate within three metres.
3. For transit, a compatible same-stop profile with a valid new walking link.
4. For walking/cycling, a current nearby profile within the configurable
   125-metre default radius.
5. Straight-line walking/cycling fallback, or pending/unavailable transit and
   driving.

Provider profile, destination, mode, network version, and—where relevant—
schedule version and representative period must match. A property profile is
also rejected if its origin moves materially. Expired or stale profiles remain
stored for audit but are not returned as current exact results.
When no safer current exact, same-stop, or nearby result exists, the API may
return the retained profile with `result_type=stale`, `freshness=stale`, and an
explicit refresh warning. It never labels that response exact or current.

Nearby walking/cycling estimates add a deterministic connector to the cached
route. The connector is Haversine distance multiplied by a conservative 1.35
detour factor, then divided by the configured mode speed. The source profile,
connector distance, method, and reduced confidence are recorded. A replaceable
barrier policy can reject reuse when barrier data becomes available.

Nearby geographic distance does not guarantee equivalent transit access.
Transit profiles are reused only when stop-access and schedule compatibility rules pass.

Same-stop reuse replaces only the origin-to-stop walking component and retains
the cached stop-to-destination component. Geographic closeness without the same
stop is never sufficient for transit reuse.

Student-facing labels deliberately distinguish evidence quality: `Exact route`,
`Cached exact route`, `Same-stop transit estimate`, `Nearby-location estimate`,
`Straight-line estimate`, `Stale route profile`, and `Transit profile
unavailable`. Raw enum values are not displayed. Transit duration is phrased as
a typical trip with an observed range and representative departure count, never
as a guarantee. Provider and calculation freshness remain visible.

### Representative transit periods and aggregation

There is intentionally no live “leave now” feature. Configured periods are:

- weekday morning: 07:30, 08:00, 08:30;
- weekday midday: 11:30, 12:00, 12:30;
- weekday evening: 16:30, 17:00, 17:30;
- weekday late evening: 21:30, 22:00, 22:30;
- Saturday daytime: 11:30, 12:00, 12:30;
- Sunday daytime: 11:30, 12:00, 12:30.

Valid samples use median duration, walking component, transfer count, distance,
and stop-to-destination component. Minimum, maximum, valid sample count, and
completeness-derived confidence are retained. Failed or missing samples remain
null and never become zero. Profiles describe a typical range, not a guarantee.

### Database schema and freshness

Migration `20260804000100_create_accessibility_profile_store.sql` adds:

- `housing_accessibility_profiles`: current and stale aggregates by durable
  origin/cache identity;
- `housing_accessibility_samples`: idempotent departure samples;
- `housing_accessibility_reuse_history`: every exact, estimated, fallback, or
  unavailable resolution decision.

Indexes cover property, coordinate bounding box, origin zone, hotspot/mode,
expiry, provider/version, nearest stop, samples, and audit lookup. No PostGIS or
spatial extension is required. Grid zones only accelerate lookup; Haversine
distance remains the reuse decision.

Walking/cycling profiles default to 180 days. Transit defaults to 30 days and
becomes stale when schedule/GTFS, network, stop, hotspot, material-origin, or age
compatibility fails. These are initial defaults, not universal guarantees.
`profiles_due_for_refresh` returns stale/expiring rows without deleting them.

### Accessibility API and ranking

```text
GET /api/accessibility/time-periods
GET /api/listings/{listing_id}/accessibility
    ?hotspot_id=western-main-campus
    &mode=transit
    &time_period=weekday_morning_commute
```

Walking/cycling do not require a period. Transit requires a configured period.
Responses contain representative/minimum/maximum duration, distance, walking,
transfers, provider/version, source profile, estimate method, confidence,
freshness, and an explanation. UI labels distinguish exact, cached, same-stop,
nearby, straight-line, stale, and unavailable results.

Ranking accepts optional accessibility results. Current exact profiles receive
normal convenience credit; same-stop, nearby, and straight-line results receive
successively reduced credit; stale or missing results remain conservative and
never behave like zero travel time.

### Applying the additive migration later

After review and securely setting `DATABASE_URL`, apply the new migration to a
database that already has Stage 4:

```powershell
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 `
  -f supabase\migrations\20260804000100_create_accessibility_profile_store.sql
```

For a fresh database, apply both timestamped migrations in filename order. The
approved loopback-only test harness does this automatically:

```powershell
$env:TEST_DATABASE_URL = "postgresql://uwo_housing_test:uwo_housing_test_only@127.0.0.1:55601/uwo_housing_test"
.\.venv\Scripts\python.exe -m pytest -m postgres -v
```

The harness applies `20260718000200_create_housing_stage4_schema.sql` first and
`20260804000100_create_accessibility_profile_store.sql` second, then inspects
the tables, constraints, and `pg_indexes` definitions. It accepts only an
explicit loopback `TEST_DATABASE_URL` naming a test database and never falls
back to `DATABASE_URL`.

## Adding a provider

For routing, implement `RoutingProvider`, `TravelTimeProvider`, or
`TransitProfileProvider`, map responses
into core results, and inject it into `create_app`. Keep credentials server-side.
Cache keys should include normalized coordinates, mode, departure-time bucket,
provider, and configuration/version. Respect that provider’s current retention
terms rather than assuming indefinite storage.

For a map renderer, implement the `MapAdapter` behavior and replace the
`ListingMap` import. Listing and hotspot DTOs do not change.

A future routing worker should claim due profiles, calculate route samples,
aggregate them with `aggregate_transit_profile`, upsert the aggregate and
samples transactionally, and record provider/schedule/network versions and
expiry. It should process a bounded property/hotspot/mode queue, use the
persistent cache before every external request, retry transient failures with
backoff, and leave missing results null rather than zero.

A future Google integration could add `GoogleGeocodingProvider`,
`GoogleRoutesProvider`, `GoogleTransitProfileProvider`, and `GoogleMapAdapter`.
OpenRouteService could implement network walking/cycling; OpenTripPlanner can
produce deterministic samples from a versioned local GTFS graph. Before
enabling a hosted provider, review the
then-current map-load and route billing, quotas, attribution, display rules,
allowed caching/storage, and restrictions on mixing results with another map.
This milestone intentionally has no Google key or API dependency.

## Limitations and next milestone

- Only the existing Western campus coordinate is verified and active.
- Straight-line and nearby connectors ignore street topology, crossings,
  gradients, and barriers until route/barrier data is configured.
- Transit remains unavailable until exact profiles are populated by a future
  offline or live provider.
- Saved lists are browser-local; comparison uses the current bounded response.
- Summary filtering currently follows a bounded repository read.
- The Stage 4 view does not expose every provenance/confidence JSON value, so
  quality labels fall back conservatively when field metadata is absent.
- Image optimization, accounts, alerts, analytics, and moderation are out of
  scope.

The recommended next milestone is to verify hotspots and import a versioned
local GTFS feed into an offline OpenTripPlanner experiment. Populate a small set
of Western transit periods and exact walk/cycle routes, inspect reuse decisions,
then usability-test labels and ranges before selecting a hosted provider.
