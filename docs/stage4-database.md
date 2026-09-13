# Stage 4 PostgreSQL persistence

Stage 4 imports one explicitly selected, reviewed Stage 0-to-3 run. It never
searches for the newest directory and does not recognize a `latest.csv` alias.
The input boundary is:

```text
data/runs/<run_id>/manifest.json
data/runs/<run_id>/stage0/listing_links.csv
data/runs/<run_id>/stage3/canonical.csv
```

The manifest, Stage 0 identities, and canonical CSV must agree before any
database connection is opened.

## Existing persistence compatibility

The legacy scripts write mutable flat rows to `public.listings`, with a bare
`listing_id` conflict key. The old image importer and earlier backend also
assume that table. It has no run, property, observation, lifecycle, or review
model. Its parsed original amount is `price_numeric`, which legacy consumers
treated as monthly even when the source period was not monthly.

Stage 4 leaves `public.listings` and both old importers untouched. The migration
uses collision-safe names:

```text
housing_pipeline_runs
housing_geocode_results
housing_properties
housing_listings
housing_listing_observations
housing_review_items
active_housing_listings (view)
```

The backend now reads `active_housing_listings` by default and uses
`price_monthly` for filtering and statistics. Set
`HOUSING_LISTINGS_RELATION` only if a different compatibility relation is
deliberately provisioned with the complete new view contract; the legacy
`public.listings` table is not a drop-in rollback value. Apply the migration
before deploying this backend change so the default view exists.

The compatibility view is intentionally restricted to `source=uwo_offcampus`,
which keeps its legacy bare `listing_id` unambiguous. It exposes amenities as a
JSON array rather than the legacy text representation. The current frontend
accepts either representation. The legacy image/transit importers still write
only `public.listings`; their updates do not flow into this view. Until those
workflows receive normalized Stage 4 persistence, image and transit fields are
present in the API contract but may be null unless they already exist in the
selected Stage 3 row.

The backend pages map and statistics reads in 500-row requests so PostgREST's
deployment row limit does not silently truncate the known dataset. The migration
enables row-level security on every normalized base table and intentionally adds
no anonymous/authenticated policies. FastAPI's service-role client can read the
security-invoker view; deployment-specific direct-client policies remain a
manual decision.

## Relationships and identity

```text
housing_pipeline_runs 1 --- * housing_listing_observations * --- 1 housing_listings
          |                           |                                |
          |                           * --- 1 housing_properties <-----*
          |                                        |
          * --- * housing_review_items             * --- 0..1 housing_geocode_results
```

`housing_listings` is the stable advertisement. Western identity is exactly:

```text
(source="uwo_offcampus", source_listing_id=<numeric URL ID>)
```

The importer canonicalizes the ID from
`offcampus.uwo.ca/Listings/Details/<id>` and rejects missing, malformed,
duplicate, or disagreeing IDs. Address, landlord, description, price, postal
code, and coordinates are never primary listing identity. Different source IDs
remain different advertisements even when they share a property.

## Property matching

Automatic property reuse is deliberately narrow:

- Whitespace, casing, cautious punctuation, and safe standalone street suffixes
  are normalized.
- Explicit `unit`, `apt`, `suite`, `room`, and `#` identifiers are stored
  separately and included in the match key.
- A complete exact address/unit/locality match can reuse a property.
- Geocoded locality is complete only with an `ok` result, a coordinate pair,
  and confidence of at least 0.8. A fully stated raw
  London/Ontario/Canada address can establish locality without geocoding.
- The same stable listing can retain an unchanged incomplete-address property.
- An exact match already planned earlier in the same import can be reused.

A new property is created when no exact, explainable match exists. Incomplete
addresses have no globally unique match key. Coordinates, postal code, or fuzzy
similarity never cause an automatic merge. Ambiguous unit/address cases create
`property_match` review items. Missing-unit and explicit-unit properties remain
separate pending review. A later incomplete or missing address cannot replace a
reliable property already attached to the same listing; the existing property
is retained and a review item records the discrepancy.

The inverse case has one narrow enrichment path. When the same stable listing
is already linked to a property with no normalized address and no match key, a
later complete deterministic address may enrich that property in place. The
candidate must have an unambiguous unit and exact match key, and no other
stored or same-run property may own that key. The guarded PostgreSQL update
rechecks those conditions transactionally and records
`existing_incomplete_property_enriched`; partial identities and competing exact
identities continue through the conservative matching/review path.

## Immutable observations and prices

Every imported run adds at most one immutable observation per listing. Reimport
of the same listing/run cannot update the historical row. The importer stores:

- Typed canonical values
- The full canonical CSV row in `raw_data`
- `*_source`, deterministic-rule, and manual fields in `provenance_data`
- AI/geocode confidence and evidence fields in `confidence_data`
- Parsed flags and generated review items
- A deterministic SHA-256 over meaningful normalized fields

Comparison ignores database/import IDs, scrape timestamps, manifest timestamps,
and JSON key order. It classifies observations as `new`, `updated`, `unchanged`,
or `relisted`, and records changed field names.

That broad importer classification is not a claim that the advertiser changed
the opportunity. `backend.listing_history.compare_listing_observations` applies
a narrower, normalization-aware product contract over adjacent observations:

- `PRICE_CHANGED`, `AVAILABILITY_CHANGED`, `LEASE_CHANGED`,
  `HOUSING_TYPE_CHANGED`, `UTILITIES_CHANGED`, `FURNISHING_CHANGED`,
  `SUBLET_CHANGED`, `GENDER_PREFERENCE_CHANGED`, and `ADDRESS_CHANGED` describe
  semantic differences only. Provenance, confidence, parser rules, review
  metadata, ranking versions, run timestamps, and geocode-cache metadata are
  never semantic changes.
- `$850`, `850`, and `850.00` compare as the same normalized monthly value.
- A semantic difference is `SOURCE_CHANGE` only when independent stored source
  evidence supports it. Unchanged source title/description/availability text is
  `PIPELINE_REINTERPRETATION`; missing or ambiguous evidence is
  `UNKNOWN_CHANGE_ORIGIN`.
- Only proven source changes are eligible for the student history projection.
  Old parser corrections therefore cannot become “listing updated” claims.

`first_seen_pipeline_run_id` and `last_seen_pipeline_run_id` provide platform
observation bounds using the corresponding run `started_at`. `last_seen_at` is
not a landlord-update timestamp. A last meaningful source-change time is
derived only from a proven source event and remains null when history cannot
support one.

`price_monthly` is the comparison/filter/statistics value. Historical rows that
lack it are converted only when both `price_numeric` and a recognized period are
available: month unchanged, week multiplied by `52/12`, and day by `365/12`.
Unknown periods stay null and create a review. The original numeric value, text,
and period are always retained. `month_per_bedroom` remains distinct from a
whole-unit `month` period even though both use a monthly conversion factor of
one.

## Lifecycle and removal detection

Stage 0 discovery IDs—not only canonical rows—are the sighting set. Therefore a
detail-page failure cannot make an advertisement appear missing.

Positive sightings reset `missing_run_count`. A previously removed listing is
marked `relisted`; a possibly removed listing returns to `active`. Relisted rows
are included in the compatibility view. `Possibly_removed` rows also remain
visible while they await the second qualifying absence; only `removed` rows are
hidden.

Absence transitions run only when discovery is plausibly complete:

- The manifest and all Stage 0-to-3 row counts validate.
- Stage 0 and Stage 1 completed successfully.
- Stage 0 discovered at least one identity.
- Stage 1 had no row limit.
- Stage 0 was not the one-page smoke configuration.
- Stage 0 recorded trustworthy discovery-count and maximum-page metrics, and did
  not reach its configured maximum.
- Discovery warnings do not report zero/below-minimum/substantial-drop/maximum
  page-limit conditions.
- The run is newer than the latest lifecycle-applying import.
- `--skip-lifecycle-updates` was not supplied.

A repeated-page warning alone does not block lifecycle processing because that
is currently a normal pagination stop. AI, geocoding, and review warnings do not
imply incomplete discovery.

The default missing threshold is two eligible runs. The first absence marks
`possibly_removed`; the second marks `removed`. Removed rows are excluded from
`active_housing_listings`.

The stable advertisement row, its property relationship, and all observations
are retained across removal. A later complete-run sighting of the same source ID
sets `relisted`, resets the missing count, appends a `relisted` observation, and
restores discovery visibility. The product history contract calls the terminal
transitions `LISTING_BECAME_INACTIVE` and `LISTING_REACTIVATED`; absence itself
does not fabricate an observation row.

Connected imports must be chronological for a source. A run at or before the
latest successfully imported run is rejected before domain writes; this avoids
rewinding current properties, first/last-seen pointers, or lifecycle state.
Import historical runs oldest-to-newest. `--skip-lifecycle-updates` suppresses
both positive status resets/relisting and absence transitions, but still records
new identities and immutable observations for a chronologically newer run.

## Reviews and change summary

Review items are generated for AI/manual flags, consensus/evidence blocks,
geocode failures and QC flags, missing coordinates, suspicious or unresolved
prices, invalid recoverable optional fields, ambiguous properties, and strong
potential duplicate-ad signals. A deterministic key prevents duplicates on
reimport, and the importer never resolves or reopens an existing item.

Each successful import stores and prints:

```text
new_listings, updated_listings, unchanged_listings, relisted_listings,
possibly_removed_listings, removed_listings, observations_inserted,
properties_created, properties_reused, possible_property_duplicates,
review_items_created, rows_rejected, rows_with_warnings
```

Inspect it in `housing_pipeline_runs.change_summary`. Import execution status is
separate from pipeline status.

## Idempotency and transactions

The importer uses psycopg 3 with parameterized SQL. Install the database-specific
requirements in the intended execution environment:

```powershell
python -m pip install -r requirements-database.txt
```

Before connecting, the importer validates the whole run and computes canonical
and manifest fingerprints. A successful identical reimport returns the stored
summary without replaying observations, reviews, or lifecycle transitions. The
same `run_id` with changed content is rejected.

The selected `missing_run_threshold` and `skip_lifecycle_updates` values are
stored in `housing_pipeline_runs.import_configuration`. Reimport with different
Stage 4 settings is rejected rather than returning a summary produced under a
different lifecycle policy. Repeat CLI output reports the originally stored
lifecycle-applied result.

Connected execution first records `import_status=running`. The main transaction
uses a source-scoped transaction advisory lock, resolves properties/geocodes,
upserts stable listings, inserts immutable observations and reviews, applies
lifecycle transitions, stores the summary, and marks success as its final
statement. Any failure rolls the domain transaction back; a separate short
transaction records a sanitized failure without changing pipeline success.

## Applying the migration later

No migration is applied automatically. After reviewing the SQL and setting
`DATABASE_URL` in the process environment:

```powershell
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f supabase\migrations\20260718000200_create_housing_stage4_schema.sql
```

The migration is additive. Diagnose a failed application from the first psql
error before retrying; do not drop the legacy table. Because there is no
destructive down migration, rollback means restoring the database backup or
dropping only the newly created `housing_*` objects after confirming they contain
no required imports.

The compatibility view uses PostgreSQL 15's `security_invoker` option;
PostgreSQL 15 or newer is the supported baseline. Versioned migrations use plain
`CREATE` statements intentionally, so an unexpected partial schema fails
immediately instead of being hidden by `IF NOT EXISTS`.

For the optional local-only PostgreSQL 15 Compose service, strict
`TEST_DATABASE_URL` safety policy, real migration/importer tests, synthetic
fixture import, and cleanup commands, see
[Disposable PostgreSQL integration testing](postgres-integration-testing.md).

## Dry run and selected import

Run finalization intentionally leaves `canonical_for_import=false`. Inspect and
approve a completed run explicitly before Stage 4:

```powershell
python -m pipeline.run_context approval-status `
  --run-dir "data\runs\<run_id>"

python -m pipeline.run_context approve `
  --run-dir "data\runs\<run_id>" `
  --approved-by "reviewer-name" `
  --note "Reviewed AI and geocode queues" `
  --confirm
```

If approval status lists material warning conditions, review the relevant
queues and add `--acknowledge-warnings` to the confirmed approval command. Fatal
conditions cannot be acknowledged away.

The importer requires both `canonical_for_import=true` and valid approval
metadata. It recomputes the approval-relevant manifest and canonical CSV
fingerprints before any database connection. Changed or unapproved runs are
rejected until deliberately reviewed and reapproved.

Dry-run an approved run without a database connection:

```powershell
.\.venv\Scripts\python.exe -m pipeline.database_importer `
  --run-dir data\runs\<run_id> `
  --dry-run
```

After migrations are applied and `DATABASE_URL` is set, import exactly one run:

```powershell
.\.venv\Scripts\python.exe -m pipeline.database_importer `
  --run-dir data\runs\<run_id> `
  --missing-run-threshold 2
```

`--allow-noncanonical-run` remains available only for exceptional reviewed
development or historical-backfill cases. It bypasses missing/invalid approval,
requires a nonblank reason, prints a warning, and stores both
`import_override_used=true` and the sanitized reason in the existing
`import_configuration.noncanonical_override` audit object. It never changes the
manifest, creates approval metadata, or becomes equivalent to approval:

```powershell
.\.venv\Scripts\python.exe -m pipeline.database_importer `
  --run-dir data\runs\<run_id> `
  --dry-run `
  --allow-noncanonical-run `
  --override-reason "Reviewed exceptional recovery import"
```

Successful import records are immutable by `run_id` plus the exact canonical
and full-manifest fingerprints. Unapproving an imported run changes its manifest
audit state but does not delete database records. A later import attempt under
that run ID is rejected because the manifest fingerprint differs. Reapproval
also does not authorize silently replacing imported contents. Changed canonical
data normally requires a new run ID and chronological import; exceptional repair
requires an independently reviewed database procedure, not the importer
override.

Use `--skip-lifecycle-updates` for a chronologically ordered observation import
that must not change existing lifecycle state. Prefer `DATABASE_URL` over
`--database-url` so credentials
do not appear in shell history or process arguments. Neither value is printed or
stored in the manifest.

## Local development and SSH execution

### Public location visibility

Migration `20260812000100_create_location_visibility_contract.sql` adds a
versioned property-level decision table and the fail-closed
`product_housing_listings` API view. A property without a current reviewed row
remains searchable but receives `location_status=unavailable`, no map marker,
and no route authorization. `limited` permits an approximate map marker but not
routing; `available` permits both.

The read-only MVP triage command now writes ignored
`location-visibility.csv` evidence without modifying a run or database. After
review, sync the complete active-property artifact transactionally:

```powershell
.\.venv\Scripts\python.exe -m scripts.sync_location_visibility `
  --input data\mvp-review-triage\<run_id>\location-visibility.csv
```

The sync rejects partial property sets, inconsistent flags, duplicate property
IDs, mixed source fingerprints, and malformed reason codes. Repeating identical
input is idempotent; changed decisions supersede the current row while retaining
history. The API defaults to `product_housing_listings`. Internal reason codes
are not included in student-facing responses.

Local development owns code changes, migrations, fixtures, dry runs, and offline
tests. The SSH server is an execution environment only: code should be committed,
reviewed, and pulled before applying migrations or importing a selected full run.
Do not develop divergent migration files on the server.

After reviewed code is committed and pulled to a POSIX SSH execution
environment, run the migration from the repository root with a securely exported
`DATABASE_URL`:

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f supabase/migrations/20260718000200_create_housing_stage4_schema.sql
```

Then import exactly one selected completed run (replace `<run_id>`):

```sh
./.venv/bin/python -m pipeline.database_importer \
  --run-dir "data/runs/<run_id>" \
  --missing-run-threshold 2
```

These commands assume the SSH environment uses the repository's `.venv` and a
POSIX shell. If it uses PowerShell, use the equivalent Windows commands above.

Default offline validation:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m compileall pipeline scripts backend
git diff --check
```

## Remaining manual Supabase setup

- Review and apply the committed migration.
- Install `requirements-database.txt` in the execution environment.
- Set `DATABASE_URL` securely for the importer.
- Confirm the database role used by FastAPI can select
  `product_housing_listings` and its underlying relations; add grants/RLS
  policies appropriate to the deployment.
- Keep normalized base-table RLS closed to anonymous/authenticated users unless
  a reviewed direct-client policy is deliberately required.
- Set or confirm `HOUSING_LISTINGS_RELATION=product_housing_listings` for FastAPI.
- Review run queues and explicitly approve or override one selected run.
- Keep the old flat table/importers until their image/transit workflow has a
  deliberate normalized replacement; writes from those importers are not read
  by `active_housing_listings`.

If an import process dies after recording `running`, inspect the run hashes,
`import_configuration`, and domain rows before retrying. A normal retry with the
same content and configuration is safe. If failure-status recording itself
failed, reconcile the stale status manually rather than changing the run ID.
