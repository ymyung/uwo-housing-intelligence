# Disposable PostgreSQL integration testing

For routine local development, prefer `.\scripts\test.ps1 full`. It starts this
Compose service, constructs and validates `TEST_DATABASE_URL` without printing
credentials, runs the PostgreSQL suite, and executes Compose `down` in a
`finally` block. The manual commands below remain available for diagnosis.

The optional PostgreSQL suite executes the real Stage 4 migration and importer
against a local disposable database. It does not start automatically and the
Compose file contains only PostgreSQL 15, the minimum version supported by the
`security_invoker` compatibility view.

The committed credentials below are intentionally limited to the disposable
local container. Never reuse them for another database.

## Install dependencies

From the repository root in Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-database.txt
```

`requirements-dev.txt` contains the offline test dependency. The separate
database requirements add the Psycopg 3 runtime used by both connected imports
and PostgreSQL integration tests.

## Start the disposable service

Docker Desktop must already be running. The repository never launches it.

```powershell
docker compose -f docker-compose.postgres-test.yml up -d
docker compose -f docker-compose.postgres-test.yml ps
docker compose -f docker-compose.postgres-test.yml exec postgres-test `
  pg_isready -U uwo_housing_test -d uwo_housing_test
```

The default host port is `55601` and is bound only to `127.0.0.1`. To use a
different port, set it before the first `up` command:

```powershell
$env:UWO_POSTGRES_TEST_PORT = "55602"
docker compose -f docker-compose.postgres-test.yml up -d
```

## Configure the test URL

Set the URL only in the current PowerShell process. Do not put it in `.env`:

```powershell
$env:TEST_DATABASE_URL = "postgresql://uwo_housing_test:uwo_housing_test_only@127.0.0.1:55601/uwo_housing_test"
```

Use the changed port in this value if `UWO_POSTGRES_TEST_PORT` was changed.
Tests read only `TEST_DATABASE_URL`; they never fall back to `DATABASE_URL` and
never print either value.

Before connecting, the test harness requires a PostgreSQL URL whose database
name contains `test`, rejects production/staging name components, rejects an
equal `DATABASE_URL`, rejects Supabase and every non-loopback host, and does not
provide an unsafe override. After connecting, it verifies both
`current_database()` and `current_schema() = 'public'` before cleanup. Cleanup
drops only these known Stage 4 objects:

```text
active_housing_listings
housing_accessibility_reuse_history
housing_accessibility_samples
housing_accessibility_profiles
housing_review_items
housing_listing_observations
housing_listings
housing_properties
housing_geocode_results
housing_pipeline_runs
```

It never drops a database, schema, or legacy `public.listings` table.

## Run tests

Run the database-independent suite without PostgreSQL:

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not postgres"
```

Run only the tests whose external dependency is PostgreSQL:

```powershell
.\.venv\Scripts\python.exe -m pytest -m "postgres and not r5"
```

If `TEST_DATABASE_URL` is absent, PostgreSQL tests skip before a connection
attempt. Once a URL is present, an unsafe target, missing Psycopg, connection
failure, migration failure, or SQL failure fails the suite rather than being
reported as skipped.

One cross-service lifecycle test requires both PostgreSQL and the separately
managed, loopback-only R5 surface service. Run it explicitly only after the
local `r5-surface` container is healthy:

```powershell
.\.venv\Scripts\python.exe -m pytest -m "postgres and r5" -v
```

With `TEST_DATABASE_URL` configured and R5 running, this command runs the true
full suite:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Without that variable, the same command runs offline tests and reports the
PostgreSQL tests as skipped. Running the marker commands separately gives the
clearest CI and local results and prevents an ordinary database test run from
silently depending on R5.

## Manual migration and fixture import

The container does not apply migrations at startup. Apply the repository
migrations in timestamp order with an installed `psql` client:

```powershell
psql "$env:TEST_DATABASE_URL" -v ON_ERROR_STOP=1 `
  -f supabase\migrations\20260718000200_create_housing_stage4_schema.sql
psql "$env:TEST_DATABASE_URL" -v ON_ERROR_STOP=1 `
  -f supabase\migrations\20260804000100_create_accessibility_profile_store.sql
```

The PostgreSQL pytest fixture resets and reapplies both files automatically
for each marked test. To import the committed one-row synthetic fixture after a
manual migration:

```powershell
.\.venv\Scripts\python.exe -m pipeline.database_importer `
  --run-dir tests\fixtures\runs\postgres_valid `
  --database-url $env:TEST_DATABASE_URL `
  --missing-run-threshold 2
```

This fixture contains no scraped or real listing data. Do not substitute a
`data\runs` directory when validating the disposable setup.

The marked accessibility tests also verify migration order, required tables,
foreign keys, partial uniqueness, and the exact `pg_indexes` definitions. A
real lifecycle test marks an advertisement removed without deleting its
property or profile, associates a later advertisement with the same property,
and verifies exact-property retrieval and reuse-history auditing. Additional
cases prove nearby walking estimation, compatible same-stop transit reuse,
rejection of geographically close transit origins without shared stop evidence,
stale retention, and idempotent active-profile upserts.

The Stage 4 migration is still unapplied to any meaningful database according
to the current project boundary. PostgreSQL compatibility fixes found before
that first deployment should therefore update the existing migration in place;
after a staging or production application, schema changes must use a new
follow-up migration. Do not create a pre-deployment migration chain solely to
amend an unapplied file.

Inspect only the disposable database:

```powershell
psql "$env:TEST_DATABASE_URL" -c "select count(*) from public.housing_pipeline_runs;"
psql "$env:TEST_DATABASE_URL" -c "select count(*) from public.housing_listings;"
psql "$env:TEST_DATABASE_URL" -c "select * from public.active_housing_listings limit 5;"
```

## Transaction and lock test mechanisms

The production CLI exposes no failure-injection option. PostgreSQL tests call
the internal `_test_failure_injector` argument on `import_validated_run`; its
single `after_domain_writes` checkpoint runs inside the domain transaction and
immediately before the success update. The rollback test verifies that domain
writes and lifecycle changes disappear while the separate sanitized failure
status remains.

The advisory-lock integration test uses multiple real Psycopg connections and
`pg_try_advisory_xact_lock(hashtext(source))`. It proves same-source exclusion,
different-source independence, and lock release after commit and rollback
without timing-sensitive threaded imports. A load/stress test with concurrent
importer processes remains a separate staging task.

## Stop, destroy, and recreate

Stop the service while retaining its test-only volume:

```powershell
docker compose -f docker-compose.postgres-test.yml stop
```

Remove the container/network while retaining the volume:

```powershell
docker compose -f docker-compose.postgres-test.yml down
```

Delete the disposable database and its named test-only volume:

```powershell
docker compose -f docker-compose.postgres-test.yml down --volumes
```

That last command permanently destroys only
`uwo_housing_postgres_test_data`. Recreate from scratch with:

```powershell
docker compose -f docker-compose.postgres-test.yml down --volumes
docker compose -f docker-compose.postgres-test.yml up -d
```
