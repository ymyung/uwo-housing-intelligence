# Local development workflow

The repository provides thin PowerShell wrappers around the existing Python CLIs, Docker containers, pytest suite, and frontend commands. They resolve the repository root automatically and call `.venv\Scripts\python.exe` directly, so they work from any current directory without activating the virtual environment.

The scripts do not contact SSH, Supabase, hosted routing providers, scraping targets, Ollama, Geoapify, or Google Maps. They never rebuild or download routing data automatically.

## First-time canonical Compose setup

From Windows PowerShell:

```powershell
Copy-Item config\local-dev.example.env config\local-dev.env

# Set a local-only POSTGRES_PASSWORD in config\local-dev.env.
# Never use a Supabase or production password here.

docker volume inspect uwo-postgres-dev-data 2>$null
if ($LASTEXITCODE -ne 0) {
    docker volume create uwo-postgres-dev-data
}

.\scripts\dev.ps1 doctor
.\scripts\dev.ps1 start
```

`config/local-dev.env` is ignored. Existing process environment variables take precedence; loading the file never silently overwrites them. The parser ignores comments and blank lines, accepts values containing `=`, rejects malformed or duplicate entries, and reports key names only under `-Verbose`.

The local file supplies the canonical Compose defaults:

```text
OTP_BASE_URL=http://127.0.0.1:8080
POSTGRES_USER=uwo
POSTGRES_DB=uwo_housing_dev
POSTGRES_PASSWORD=<local-only-password>
JAVA_TOOL_OPTIONS=-Xmx8g
```

When `ACCESSIBILITY_DATABASE_URL` is absent, the tooling reads the development-only `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` values from the named `uwo-postgres-dev` container, discovers its mapped host port, constructs the URL in memory, validates it, and passes it only to the worker process. The URL and password are never printed or logged.

An explicitly configured development URL is accepted only when it uses PostgreSQL, loopback, and the exact database name `uwo_housing_dev`. Supabase and unknown remote hosts are rejected. OTP must use loopback HTTP.

## Daily development

```powershell
.\scripts\dev.ps1 start
.\scripts\test.ps1 fast
```

Use `-Verbose` on any wrapper for command-level diagnostics without credential values.

## Environment lifecycle

Supported commands are:

```powershell
.\scripts\dev.ps1 doctor
.\scripts\dev.ps1 start
.\scripts\dev.ps1 status
.\scripts\dev.ps1 restart
.\scripts\dev.ps1 stop
```

`start` checks `.venv`, Docker, routing inputs, the local worker config, and the reviewed-property CSV. It rejects conflicting legacy containers, then runs `docker compose -f docker-compose.dev.yml up -d`, waits up to 90 seconds for PostgreSQL and 120 seconds for OTP, and reconstructs `ACCESSIBILITY_DATABASE_URL`. Compose reuses healthy services and the external PostgreSQL volume. It does not rebuild `graph.obj`.

`status` reports service and artifact readiness, Compose versus legacy ownership, actual port bindings, Git branch/worktree state, router/network/schedule versions, reference week, reviewed-property count, and the newest valid accessibility run.

`doctor` is read-only. It reports `PASS`, `WARNING`, or `FAIL` for Python, Docker, local configuration, routing files, Compose parsing, service definitions, loopback bindings, the external volume, the read-only routing mount, OTP load-only behavior, container ownership, Git ignore rules, Node, npm, and frontend dependencies. Existing containers published on `0.0.0.0` or not owned by `docker-compose.dev.yml` receive warnings.

`stop` runs `docker compose -f docker-compose.dev.yml stop` for the canonical services and brings down the separate disposable PostgreSQL test project if active. A safe fallback stops legacy containers without removing them. It does not remove containers, volumes, graph files, database contents, or accessibility runs. Stopping an absent or already stopped service is nonfatal.

## Testing

Fast feedback:

```powershell
.\scripts\test.ps1 fast
.\scripts\test.ps1 fast -Area routing
```

Fast mode runs focused offline routing/accessibility tests. It never starts PostgreSQL or builds the frontend.

Normal validation:

```powershell
.\scripts\test.ps1 normal
```

Normal mode runs all non-PostgreSQL Python tests, Python compilation, frontend unit tests, and `git diff --check`.

Full pre-commit validation:

```powershell
.\scripts\test.ps1 full
```

Full mode adds the PostgreSQL-only integration suite, frontend lint, and frontend production build. It starts `docker-compose.postgres-test.yml`, inspects the generated test container, discovers its mapped port and local-only credentials, constructs `TEST_DATABASE_URL` in memory, and runs the repository's existing safety validator before pytest connects. Tests marked `r5` are a separate opt-in cross-service gate because this command intentionally does not start the development R5 service. A `try/finally` always executes Compose `down`, including after failures. The disposable volume is not deleted.

The disposable test URL is never derived from `ACCESSIBILITY_DATABASE_URL`, and the development database `uwo_housing_dev` cannot pass the test-database safety policy.

## Routing workflow

Preflight:

```powershell
.\scripts\routing.ps1 preflight
```

One-property smoke test using the first reviewed property:

```powershell
.\scripts\routing.ps1 smoke
```

Select an explicit reviewed property when needed:

```powershell
.\scripts\routing.ps1 smoke -PropertyId 2
```

Run the complete reviewed proof of concept:

```powershell
.\scripts\routing.ps1 poc
```

Verify compatible current profiles are reused:

```powershell
.\scripts\routing.ps1 cache
```

`cache` fails when provider calls occur despite a compatible, unexpired, fully populated prior POC. It permits and explains calls caused by changed scope, graph/schedule/network/property fingerprints, prior failures, or expiry.

Inspect the newest run without contacting OTP or PostgreSQL:

```powershell
.\scripts\routing.ps1 latest
```

The routing wrappers call `scripts.run_accessibility_worker` directly, use the existing six transit periods, and preserve the worker's 20-property and 3-hotspot limits. They never pass `--allow-larger-run`.

Inspect the configured static GTFS calendar and its graph fingerprint without contacting a network service:

```powershell
.\scripts\routing.ps1 gtfs-status
```

Given an independently verified official URL or local ZIP, validate first and then stage without replacing the active feed or graph:

```powershell
.\scripts\routing.ps1 gtfs-stage -GtfsSource '<verified-source>' -DryRun
.\scripts\routing.ps1 gtfs-stage -GtfsSource '<verified-source>'
```

Staging never promotes or rebuilds a graph. Follow [routing-data.md](routing-data.md) for candidate isolation, graph validation, promotion, and rollback. See [transport-frontend.md](transport-frontend.md) for the compact/detail API and Getting to Western UX contracts.

## Persistent local state

Stopping services preserves:

- PostgreSQL data in Docker volume `uwo-postgres-dev-data`.
- OTP graph, OSM, GTFS, router config, and build manifest under `data/routing/`.
- Accessibility worker runs under `data/accessibility-runs/`.
- The ignored worker config at `config/accessibility-worker.toml`.
- The ignored reviewed-property selection at `config/accessibility-poc-properties.csv`.

The disposable PostgreSQL Compose environment is separate from `uwo-postgres-dev`. It uses a test-named database, is configured only for integration tests, and is torn down after `test.ps1 full`. Its volume persists unless a developer deliberately removes it outside these scripts.

## Migrating existing manual containers

`dev.ps1 start` never removes conflicting manually-created containers. When it reports a legacy conflict, use this deliberate migration. These commands preserve `uwo-postgres-dev-data`, `data/routing`, `graph.obj`, and `data/accessibility-runs`.

First verify and record the persistent state:

```powershell
docker volume inspect uwo-postgres-dev-data | Out-Null

$graphHashBefore = (Get-FileHash data\routing\graph.obj -Algorithm SHA256).Hash
$latestRunBefore = Get-ChildItem data\accessibility-runs -Directory |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1 -ExpandProperty Name
```

Before removing the old PostgreSQL container, copy its existing local-only password into the ignored developer file without displaying it:

```powershell
$legacyEnvironment = docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' uwo-postgres-dev
$passwordLine = $legacyEnvironment |
  Where-Object { $_ -like 'POSTGRES_PASSWORD=*' } |
  Select-Object -First 1
if (-not $passwordLine) { throw 'Legacy PostgreSQL password was not found.' }

$legacyPassword = $passwordLine.Substring('POSTGRES_PASSWORD='.Length)
$localEnvPath = Resolve-Path config\local-dev.env
$localEnvLines = @(Get-Content -LiteralPath $localEnvPath)
$passwordEntry = "POSTGRES_PASSWORD=$legacyPassword"
if ($localEnvLines -match '^POSTGRES_PASSWORD=') {
  $localEnvLines = $localEnvLines | ForEach-Object {
    if ($_ -match '^POSTGRES_PASSWORD=') { $passwordEntry } else { $_ }
  }
} else {
  $localEnvLines += 'POSTGRES_USER=uwo'
  $localEnvLines += 'POSTGRES_DB=uwo_housing_dev'
  $localEnvLines += $passwordEntry
  $localEnvLines += 'JAVA_TOOL_OPTIONS=-Xmx8g'
}
$localEnvLines | Set-Content -LiteralPath $localEnvPath -Encoding UTF8
Remove-Variable legacyPassword, passwordEntry, passwordLine, legacyEnvironment
```

Then remove only the two stopped legacy container objects and start the canonical services:

```powershell
docker stop uwo-postgres-dev uwo-otp-router
docker rm uwo-postgres-dev uwo-otp-router

docker compose -f docker-compose.dev.yml config --quiet
.\scripts\dev.ps1 start
.\scripts\dev.ps1 status
```

Verify persistence and routing after migration:

```powershell
docker exec uwo-postgres-dev psql -U uwo -d uwo_housing_dev `
  -c "select count(*) from public.housing_accessibility_profiles;"

$graphHashAfter = (Get-FileHash data\routing\graph.obj -Algorithm SHA256).Hash
if ($graphHashAfter -ne $graphHashBefore) { throw 'graph.obj changed during migration.' }

.\scripts\routing.ps1 preflight
.\scripts\routing.ps1 smoke
.\scripts\routing.ps1 cache
```

The canonical containers keep the recognizable names `uwo-postgres-dev` and `uwo-otp-router`, but now carry Compose ownership labels and publish only `127.0.0.1:55600` and `127.0.0.1:8080`.

### Rollback

To roll back without deleting persistent data, stop and remove only the Compose-managed containers and recreate the prior manual shape with the same external volume and routing directory:

```powershell
docker compose -f docker-compose.dev.yml down

docker run -d --name uwo-postgres-dev --env-file config\local-dev.env `
  -p 127.0.0.1:55600:5432 `
  -v uwo-postgres-dev-data:/var/lib/postgresql/data `
  postgres:15

docker run -d --name uwo-otp-router `
  -e JAVA_TOOL_OPTIONS=-Xmx8g `
  -p 127.0.0.1:8080:8080 `
  -v "${PWD}\data\routing:/var/opentripplanner:ro" `
  opentripplanner/opentripplanner:2.6.0 --load --serve
```

Do not add `--volumes`, `-v`, `docker volume rm`, or any prune command to the Compose teardown. Run `doctor`, `start`, and routing preflight after rollback. Never copy Supabase credentials into local developer configuration.

## Troubleshooting

If OTP is unavailable:

```text
OpenTripPlanner is not running.
Start it with:
.\scripts\dev.ps1 start
```

If the database variable cannot be restored:

```text
ACCESSIBILITY_DATABASE_URL was not loaded.
Create config/local-dev.env from config/local-dev.example.env,
then run:
.\scripts\dev.ps1 start
```

Underlying command errors are retained after the actionable context and sanitized for database URLs, passwords, API keys, tokens, secrets, and authorization values.

## Western demo browser suite

With the canonical development containers, API, and Vite frontend running,
exercise the desktop and 390×844 mobile product journeys in local Chromium:

```powershell
.\scripts\test_demo_e2e.ps1
```

The suite never permits non-loopback browser requests, so external map tiles
are intentionally absent during the test. Failures capture screenshots under
ignored `data/demo-e2e/test-results/`. Set `E2E_BASE_URL` only through the
wrapper or explicitly when invoking the `e2e` pytest marker.

## End of day

```powershell
.\scripts\dev.ps1 stop
```

The next `start` restores the in-process local environment and restarts the same persistent containers.
