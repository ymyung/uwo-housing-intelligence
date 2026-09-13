# City of London reference data

This local-only reference system keeps factual City of London GIS evidence
separate from listings, search, ranking, routing, and accessibility. It does
not merge advertisements, replace Geoapify coordinates, infer rental legality,
ownership, unit counts, or change product/API behaviour.

The City is the sole source. The endpoints are public ArcGIS REST services; no
API key is used. Consult the City's [Open Data catalogue and
terms](https://london.ca/government/council-civic-administration/open-data) for
attribution and reuse conditions.

## Source catalogue

The committed, machine-readable catalogue is
[`config/london-reference-data.toml`](../config/london-reference-data.toml).
Each entry records the official URL, ArcGIS layer ID, expected geometry,
identity field, selected factual attributes, target table, and refresh policy.

| Dataset | Official City layer | Geometry | Reference use |
| --- | --- | --- | --- |
| Municipal addresses | [OpenData Community / 0](https://maps.london.ca/server/rest/services/OpenData/OpenData_Community/MapServer/0) | point | Existing conservative address-resolution evidence |
| Building footprints | [OpenData BaseMaps / 3](https://maps.london.ca/server/rest/services/OpenData/OpenData_BaseMaps/MapServer/3) | polygon | Existing physical-building evidence |
| Parcels | [OpenData BaseMaps / 53](https://maps.london.ca/server/rest/services/OpenData/OpenData_BaseMaps/MapServer/53) | polygon | Existing parcel evidence; not legal-survey or ownership truth |
| Bicycle routes | [OpenData Transportation / 20](https://maps.london.ca/server/rest/services/OpenData/OpenData_Transportation/MapServer/20) | line | Physical route network data and City route classifications |
| Multi-use paths | [Bike Routes and Walking Trails / 7](https://maps.london.ca/server/rest/services/OpenData/BikeRoutesAndWalkingTrails/MapServer/7) | line | Recreation path geometry and source category |
| Thames Valley Parkway | [Bike Routes and Walking Trails / 8](https://maps.london.ca/server/rest/services/OpenData/BikeRoutesAndWalkingTrails/MapServer/8) | line | Dedicated Thames Valley Parkway geometry |
| Unpaved walking trails | [Bike Routes and Walking Trails / 9](https://maps.london.ca/server/rest/services/OpenData/BikeRoutesAndWalkingTrails/MapServer/9) | line | Recreation path geometry and source category |
| Sidewalks | [OpenData Transportation / 4](https://maps.london.ca/server/rest/services/OpenData/OpenData_Transportation/MapServer/4) | line | Sidewalk geometry and City-provided material/width fields |
| Walkways | [OpenData Transportation / 6](https://maps.london.ca/server/rest/services/OpenData/OpenData_Transportation/MapServer/6) | line | Walkway geometry and City-provided identifiers/names |
| Parks | [Basemap Information / 2](https://maps.london.ca/server/rest/services/Basemap_Information/MapServer/2) | polygon | Park boundary, name, City category, and selected factual attributes |
| Pedestrian crossovers | [OpenData Transportation / 12](https://maps.london.ca/server/rest/services/OpenData/OpenData_Transportation/MapServer/12) | point | City-designated pedestrian crossover locations |
| Signalized intersections | [OpenData Transportation / 14](https://maps.london.ca/server/rest/services/OpenData/OpenData_Transportation/MapServer/14) | point | City signalized-intersection locations and source status/type |

Only source attributes with clear factual meaning are promoted into columns.
The original City attributes remain in `source_attributes` JSONB for audit. The
system deliberately derives no safety, comfort, accessibility, routing, or
ranking score.

## Storage, versioning, and validation

All geometry remains in the City's native CRS, EPSG:26917 (NAD83 / UTM zone
17N). Each snapshot is validated before import:

- ArcGIS pagination is count-checked and ordered.
- Required fields and source identity are present.
- Geometry family, exact City SRID, validity, non-emptiness, duplicate source
  IDs, and conservative London UTM bounds are checked.
- Snapshot content and source schema receive deterministic SHA-256 fingerprints.

`reference_data.dataset_runs` stores source URL/layer identity, retrieval time,
source-update metadata when published, counts, validation summary, importer
version, content/schema fingerprints, and the current-run pointer. Import is
idempotent for the same dataset and fingerprint. A successful replacement
promotes a new run only after validation; older feature rows remain available
as history. A bad download or import never removes the previous current run.

Mobility and recreation rows live in versioned PostGIS tables:

- `reference_data.london_bicycle_routes`
- `reference_data.london_recreation_paths`
- `reference_data.london_sidewalks`
- `reference_data.london_walkways`
- `reference_data.london_parks`
- `reference_data.london_pedestrian_crossovers`
- `reference_data.london_signalized_intersections`

Every table has a current dataset-run foreign key, per-run/source-ID uniqueness,
geometry/SRID constraints, GIST geometry index, and run/source lookup indexes.
They are reference evidence only: no property links, denormalized listing
values, persisted distance calculations, or product-visible fields are written.

## Local operations

Use the project virtual environment and a local development database. Raw
snapshots are written beneath ignored `data/reference/london/`; do not commit
them. `refresh_london_reference_data` does not run the scraper, AI enrichment,
geocoder, ranking, OTP, or product migrations.

```powershell
# Inspect official metadata/counts without downloading features or changing PostgreSQL.
.\.venv\Scripts\python.exe -m scripts.refresh_london_reference_data refresh `
  --dataset bicycle_routes --dry-run

# Download and validate one source without importing it.
.\.venv\Scripts\python.exe -m scripts.refresh_london_reference_data download `
  --dataset sidewalks
.\.venv\Scripts\python.exe -m scripts.refresh_london_reference_data validate `
  --dataset sidewalks --snapshot data/reference/london/sidewalks/<fingerprint>.json

# Download, validate, and versioned-import selected City mobility sources.
.\.venv\Scripts\python.exe -m scripts.refresh_london_reference_data refresh `
  --dataset bicycle_routes --dataset recreation_paths_multi_use `
  --dataset thames_valley_parkway --dataset walking_trails_unpaved `
  --dataset sidewalks --dataset walkways --dataset parks `
  --dataset pedestrian_crossovers --dataset signalized_intersections

# Report current source versions, fingerprints, counts, coverage, lengths, and categories.
.\.venv\Scripts\python.exe -m scripts.refresh_london_reference_data status

# Read-only spatial proof of concept for already-geocoded local properties.
.\.venv\Scripts\python.exe -m scripts.refresh_london_reference_data poc `
  --property-id 4 --property-id 167
```

For the established address/building/parcel workflow, keep using
`scripts/sync_london_reference_data.py`. Its matching remains shadow-only:
normalization handles capitalization, whitespace, punctuation, common suffixes,
directions, and explicit units; `ST_Covers` is used for building/parcel evidence
with no nearest-building fallback. Audit outputs remain ignored under
`data/london-reference-validation/`.

## Future use, deliberately deferred

The versioned geometries can support future reviewed features such as a
mobility context panel, local analysis, source-backed map layers, or clearly
labeled research indicators. Any proposal must first define product wording,
refresh policy, evidence limits, accessibility impact, source attribution,
validation, and opt-in UI/API contract. It must not claim official conditions,
safety, route suitability, accessibility, or housing quality from these layers
alone.

## Mobility Context V1 research contract

Mobility Context V1 is an opt-in, local, shadow-only study combining an exact
OTP bicycle route with current City reference geometry. R5 bicycle numerical
surfaces remain disabled because their earlier OTP parity gate did not pass;
that result does not invalidate independently checked point-to-point OTP routes.
The verified destination remains `western-main-campus` at `43.0096,-81.2737`.

The conservative research label is **route geometry associated with
City-recognized cycling routes and multi-use pathways**. Eligibility is explicit:

- Include City on-street bicycle features only when `status=Existing` and the
  official facility type is `Separated`, `Designated`, or `Shared`.
- Include the official Other Multi-use Pathways layer while retaining its
  source category, and include the Thames Valley Parkway layer as its own
  category.
- Exclude `Future` and `Removed` bicycle features, unclassified bicycle
  features, and the unpaved walking-trails layer. Sidewalks, walkways,
  crossovers, signals, and parks remain outside the cycling-overlap metric.

`mobility-context-v1` decodes the normalized OTP bicycle itinerary, verifies
mode, positive measures, endpoints, London bounds, segment continuity, and
agreement between decoded and itinerary distance. It transforms the route to
EPSG:26917. Eligible City lines are buffered, unioned, and intersected with the
route. The overall numerator is the length of the unique route intersection,
so overlapping City layers cannot double-count it; the denominator is the
projected OTP route length. Per-category intersections are factual but
non-additive because categories can overlap.

Research compared 5, 8, 10, and 15 metre tolerances. Eight metres is the
smallest useful research default: 5 metres visibly misses some offset but
apparently corresponding lines, while 15 metres produces large jumps where
parallel or crossing facilities may be captured. The remaining sensitivity is
material, so percentages are not approved for product display. On a
geographically distributed sample of 80 route-eligible properties, 40/40
bounded local OTP reroutes succeeded and passed geometry checks. Seventy-five
routes had complete evidence; five non-live sample rows lacked a compatible
cached v2 itinerary. At 8 metres, route-overlap share was 39.0% at p25, 74.2%
at the median, 84.7% at p75, and 91.7% at p90. At 5 metres the median was 68.8%;
at 15 metres it was 76.3%, with several individual routes changing far more.
Manual GeoJSON/SVG review confirmed both genuine alignment and false-match
risk from nearby lines.

No result is persisted. The compatibility identity nevertheless includes the
canonical property origin, destination identity/coordinate, OTP provider and
profile, router/network versions, current content/schema fingerprints for all
included City datasets, the tolerance, and algorithm version. Any changed
dependency makes prior evidence incompatible. `ready` requires an approved
location, a valid exact route, and all current City dependencies; a missing City
dependency is `limited`; unsafe location or invalid/no route is `unavailable`.
Missing values never become zero.

Run the ignored diagnostic study only against local services and already
imported data:

```powershell
.\scripts\dev.ps1 start
.\.venv\Scripts\python.exe -m scripts.analyze_mobility_context `
  --sample-size 80 --live-route-limit 40 --diagnostic-count 6
```

The run writes normalized summaries and bounded overlays under ignored
`data/mobility-context-validation/`; it retains no raw OTP response and writes
no property metric. In the observed run, City mask construction took about
2.3 seconds, local OTP calls had a 0.04-second median, an 8-metre overlap query
had a 16.8-millisecond median, and the 80-property batch took about 10.7 seconds
after startup.

Product gates currently resolve as follows: exact bicycle routing passes the
bounded sample; the conservative City inclusion terminology passes; overlap
stability does not yet pass; factual wording is possible but therefore not
shown; shadow-analysis performance passes. Consequently there is no Mobility
Context API, listing-detail UI, search filter, comparison row, persisted cache,
ranking input, safety claim, comfort claim, or composite score.

## Coordinate selection V1 shadow policy

`coordinate-selection-v1` is a deterministic, read-only candidate policy. For
exact London civic-address matches, the prototype can reconcile listing
locations against official City of London spatial reference data, with
third-party geocoding retained as fallback. City data is not universally
preferred. Selection requires a current accepted dataset, an exact and
high-confidence address match without review reasons, resolved explicit units,
an unshared municipal point, accepted address status, valid EPSG:26917 geometry
inside London bounds, one containing parcel, and no ambiguous or contradictory
building evidence. A City point must also agree with Geoapify within the
versioned tolerance or have stronger physical-geometry support. All other
conflicts retain the current safe coordinate and are identified for review.

Automatic City selection is capped at 100 m of movement. Larger moves always
remain review candidates, even when the City evidence otherwise passes. The
versioned policy also treats parcels above 6,937.72 m² as large (the current
exact-match parcel-area P90) and records the number of intersecting City
buildings and covered municipal address points on each matched parcel. A large,
multi-building, or multi-address parcel requires a uniquely resolved containing
building before it can be selected automatically; building containment never
bypasses the movement cap.

Geoapify is not assumed wrong merely because it differs from the City point.
When a materially different Geoapify point is inside the same matched parcel,
or is no more than 25 m outside it, the placement remains a conflict. The 25 m
threshold covers the reviewed 5.42 m and 20.35 m near-edge cases and stops
before the next reviewed outside-parcel distance at 46.18 m. These facts can
indicate two plausible physical placements, but do not prove that either point
is an entrance.

The selector chooses only a canonical physical/civic coordinate. The City
geometries do not establish an entrance or routing snap, landlord, ownership,
occupancy, rental legality, or listing legitimacy. A pedestrian routing origin
or network snap is a separate future policy and is neither created nor changed
by coordinate-selection-v1.

The generator writes only ignored artifacts below
`data/london-reference-validation/coordinate-selection-v1/`; normal product
queries never read them. It fingerprints protected tables before and after the
single set-based candidate query and must run in a read-only transaction. A
future promotion requires separate approval and an atomic migration that keeps
property coordinates and the map's latest-observation projection consistent.
It must then invalidate and recompute coordinate-dependent accessibility,
route/surface, campus-distance, mobility-context, and ranking outputs before
visibility can be reassessed. Rollback must restore the prior property and map
coordinates together and rebuild the same derived outputs; this policy does
not perform any of those actions.

A future human-review mechanism should store `APPROVE_CITY` or
`RETAIN_GEOAPIFY` separately from automatic policy configuration. Each decision
must be versioned and auditable, keyed by property plus policy and candidate
fingerprints, and invalidated whenever the City dataset, Geoapify result,
canonical property evidence, or selector policy fingerprint changes. The
current shadow selector does not implement or apply such overrides.

## Coordinate promotion V1 dry-run design

`coordinate-promotion-v1` is a migration design and disposable-database proof,
not authorization to change a coordinate. It byte-pins selector policy
`coordinate-selection-v1` and candidate `20260825T021255Z`, then admits only
fresh `CITY_SELECTED_SHADOW` rows at or below 100 m. It never re-runs the
selector during freshness checking. Current property/Geoapify coordinates, the
accepted City dataset run and content fingerprint, municipal match, and spatial
building/parcel evidence must equal the frozen evidence fingerprint. Drifted
rows become `STALE_CANDIDATE_REVIEW`; review, fallback, no-coordinate, prior
manual-override, and over-100 m rows cannot enter the first cohort.

The current location flow contains one deliberate duplication:

```text
housing_properties.latitude/longitude
  -> exact route and walking-surface origin lookup

latest active housing_listing_observations.latitude/longitude
  -> active_housing_listings
  -> ranked_housing_listings
  -> product_housing_listings
  -> collection, map-marker, and listing-detail coordinates
```

Therefore the smallest safe cutover is one transaction that locks and snapshots
the cohort; updates `housing_properties` and only each active listing's latest
observation projection; recalculates that projection's deterministic
`distance_to_western_km`; and invalidates current visibility, accessibility,
exact-route cache profiles, walking surfaces, and ranking. Older observations
and every observation's `raw_data` remain immutable. The latest observation's
`provenance_data` receives the promotion run reference because these coordinate
columns are the current derived product projection. A later refactor may derive
the public projection directly from the property, but that is not required for
an atomic first migration.

The proposed immutable provenance store has a versioned promotion run plus one
item per property. It records policy/candidate versions and fingerprints, old
property state, every latest projection changed, the new coordinate/source,
movement, City dataset run/fingerprint, municipal/building/parcel identifiers,
reason, timestamps, and rollback/supersession links. The schema and exact
set-based action order are emitted as ignored dry-run artifacts; no production
migration is installed by the generator.

Cutover invalidations are synchronous and fail closed:

- supersede current location visibility so product map/routing remains hidden
  until a new reviewed decision exists;
- mark current accessibility profiles stale, which also hides their stored
  exact/cached route results through existing current-profile checks;
- mark ready/computing walking surfaces failed with
  `origin_coordinate_changed`;
- supersede current ranking rows so the public ranking projection has no stale
  score; and
- update straight-line campus distance for the latest projection in the atomic
  transaction using the Stage 3 haversine constants and rounding.

No OTP, R5, surface, ranking, or visibility computation belongs in that short
transaction. After commit, recompute targeted walking, cycling, transit, and
exact-route profiles for affected property coordinate identities; rebuild only
affected walking surfaces; recompute ranking after accessibility is current;
and reassess visibility last. Mobility Context V1 is shadow-only and has no
persisted product artifact to invalidate. Canonical civic coordinates remain
distinct from future pedestrian entrance/network-snap coordinates.

Rollback is a linked immutable run. In one transaction it restores property and
latest-projection state from the promotion items and invalidates all derived
state again. It does not revive claims computed for either coordinate. Targeted
recomputation then runs against the restored coordinate, with visibility again
last.

For a future real execution, use a maintenance/offline window: verify branch and
HEAD, schema, frozen fingerprints/counts, zero stale candidates and zero moves
over 100 m; take a database snapshot; require the disposable cutover,
failure-injection, and rollback tests to pass; confirm OTP/R5 and other required
recompute services are ready; execute the atomic cutover; recompute in the order
above; run API/frontend/E2E regressions and manual outlier review; then finalize
the promotion run. The public API schema does not change.

Generate only the ignored read-only plan against the loopback development
database:

```powershell
.\.venv\Scripts\python.exe -m scripts.generate_coordinate_promotion_dry_run
```
