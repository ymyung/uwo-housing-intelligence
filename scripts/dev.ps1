[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "status", "restart", "doctor")]
    [string]$Command = "status",
    [string]$EnvFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ModulePath = Join-Path $PSScriptRoot "lib\LocalDevTools.psm1"
Import-Module $ModulePath -Force
if (-not $EnvFile) { $EnvFile = Join-Path $ProjectRoot "config\local-dev.env" }
$WorkerConfig = Join-Path $ProjectRoot "config\accessibility-worker.toml"
$PropertyCsv = Join-Path $ProjectRoot "config\accessibility-poc-properties.csv"
$RoutingRoot = Join-Path $ProjectRoot "data\routing"
$DevCompose = Join-Path $ProjectRoot "docker-compose.dev.yml"
$TestCompose = Join-Path $ProjectRoot "docker-compose.postgres-test.yml"

function Add-Check {
    param(
        [System.Collections.ArrayList]$Rows,
        [string]$Component,
        [ValidateSet("PASS", "WARNING", "FAIL", "READY", "STOPPED", "MISSING")]
        [string]$Status,
        [string]$Detail = ""
    )
    [void]$Rows.Add([pscustomobject]@{ Component = $Component; Status = $Status; Detail = $Detail })
}

function Write-StatusTable {
    param([object[]]$Rows)

    Write-Host ("{0,-31} {1,-9} {2}" -f "Component", "Status", "Detail")
    Write-Host ("-" * 78)
    foreach ($row in $Rows) {
        $color = switch ($row.Status) {
            { $_ -in @("PASS", "READY") } { "Green"; break }
            { $_ -in @("WARNING", "STOPPED") } { "Yellow"; break }
            default { "Red" }
        }
        Write-Host ("{0,-31} {1,-9} {2}" -f $row.Component, $row.Status, $row.Detail) -ForegroundColor $color
    }
}

function Get-ManifestMetadata {
    $path = Join-Path $RoutingRoot "build-manifest.json"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    try { return Get-Content -LiteralPath $path -Raw | ConvertFrom-Json } catch { return $null }
}

function Get-ReferenceWeek {
    if (-not (Test-Path -LiteralPath $WorkerConfig -PathType Leaf)) { return "unknown" }
    $match = Select-String -LiteralPath $WorkerConfig -Pattern '^\s*reference_service_week\s*=\s*(?<value>\d{4}-\d{2}-\d{2})\s*$' | Select-Object -First 1
    if ($match) { return $match.Matches[0].Groups["value"].Value }
    return "unknown"
}

function Get-GitMetadata {
    $branchResult = Invoke-UwoCapturedCommand -FilePath "git" -Arguments @("branch", "--show-current") -WorkingDirectory $ProjectRoot
    $statusResult = Invoke-UwoCapturedCommand -FilePath "git" -Arguments @("status", "--porcelain") -WorkingDirectory $ProjectRoot
    return [pscustomobject]@{
        Branch = if ($branchResult.ExitCode -eq 0) { $branchResult.StdOut.Trim() } else { "unknown" }
        State = if ($statusResult.ExitCode -eq 0 -and -not $statusResult.StdOut.Trim()) { "CLEAN" } else { "DIRTY" }
    }
}

function Get-DevelopmentRows {
    $rows = New-Object System.Collections.ArrayList
    $python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    Add-Check $rows "Virtual environment" $(if (Test-Path -LiteralPath $python) { "READY" } else { "MISSING" })

    $dockerReady = Test-UwoDockerDaemon
    Add-Check $rows "Docker" $(if ($dockerReady) { "READY" } else { "MISSING" })
    Add-Check $rows "Development Compose" $(if (Test-Path -LiteralPath $DevCompose -PathType Leaf) { "READY" } else { "MISSING" }) "docker-compose.dev.yml"
    if ($dockerReady) {
        $postgres = Get-UwoContainerState -Name "uwo-postgres-dev"
        $otp = Get-UwoContainerState -Name "uwo-otp-router"
        $legacy = @(Get-UwoLegacyDevelopmentContainers)
        Add-Check $rows "Development orchestration" $(if ($legacy.Count) { "WARNING" } else { "READY" }) $(if ($legacy.Count) { "Legacy/manual containers: $($legacy -join ', ')" } else { "docker-compose.dev.yml" })
        Add-Check $rows "Development PostgreSQL" $(if ($postgres.Running -and (Wait-UwoPostgresReady -TimeoutSeconds 2)) { "READY" } elseif ($postgres.Exists) { "STOPPED" } else { "MISSING" }) $postgres.Image
        Add-Check $rows "OpenTripPlanner" $(if ($otp.Running -and (Test-UwoOtpReady)) { "READY" } elseif ($otp.Exists) { "STOPPED" } else { "MISSING" }) $otp.Image
        if ($postgres.Exists) {
            $postgresBindings = @(Get-UwoContainerBindingAddresses -Name "uwo-postgres-dev")
            Add-Check $rows "PostgreSQL loopback binding" $(if (Test-UwoLoopbackBindings $postgresBindings) { "READY" } else { "WARNING" }) $(if (Test-UwoLoopbackBindings $postgresBindings) { "127.0.0.1 only" } else { "Wildcard/non-loopback binding detected" })
        }
        if ($otp.Exists) {
            $otpBindings = @(Get-UwoContainerBindingAddresses -Name "uwo-otp-router")
            Add-Check $rows "OTP loopback binding" $(if (Test-UwoLoopbackBindings $otpBindings) { "READY" } else { "WARNING" }) $(if (Test-UwoLoopbackBindings $otpBindings) { "127.0.0.1 only" } else { "Wildcard/non-loopback binding detected" })
        }
    } else {
        Add-Check $rows "Development orchestration" "MISSING" "docker-compose.dev.yml"
        Add-Check $rows "Development PostgreSQL" "MISSING"
        Add-Check $rows "OpenTripPlanner" "MISSING"
    }

    foreach ($item in @(
        @("OTP graph", (Join-Path $RoutingRoot "graph.obj")),
        @("GTFS", (Join-Path $RoutingRoot "london-transit.gtfs.zip")),
        @("OSM", (Join-Path $RoutingRoot "ontario.osm.pbf")),
        @("Router config", (Join-Path $RoutingRoot "router-config.json")),
        @("Routing manifest", (Join-Path $RoutingRoot "build-manifest.json")),
        @("Worker config", $WorkerConfig),
        @("Reviewed property CSV", $PropertyCsv)
    )) {
        Add-Check $rows $item[0] $(if (Test-Path -LiteralPath $item[1] -PathType Leaf) { "READY" } else { "MISSING" })
    }
    $git = Get-GitMetadata
    Add-Check $rows "Git branch" "READY" $git.Branch
    Add-Check $rows "Git working tree" $(if ($git.State -eq "CLEAN") { "READY" } else { "WARNING" }) $git.State
    return $rows
}

function Write-DevelopmentStatus {
    $rows = @(Get-DevelopmentRows)
    Write-StatusTable $rows
    $metadata = Get-ManifestMetadata
    $latest = Get-UwoLatestAccessibilityRun
    $propertyCount = if (Test-Path -LiteralPath $PropertyCsv) { @(Import-Csv -LiteralPath $PropertyCsv).Count } else { 0 }
    Write-Host ""
    Write-Host "OTP router version:          $(if ($metadata) { $metadata.router_version } else { 'unknown' })"
    Write-Host "Network version:             $(if ($metadata) { $metadata.network_version } else { 'unknown' })"
    Write-Host "Schedule version:            $(if ($metadata) { $metadata.schedule_version } else { 'unknown' })"
    Write-Host "Configured reference week:   $(Get-ReferenceWeek)"
    Write-Host "Reviewed POC properties:     $propertyCount"
    Write-Host "Latest accessibility run ID: $(if ($latest) { $latest.RunId } else { 'none' })"
    Write-Host "Latest run status:           $(if ($latest) { $latest.Manifest.status } else { 'none' })"
    Write-Host "Development orchestration:  docker-compose.dev.yml"
}

function Assert-RequiredLocalFiles {
    $required = @(
        (Join-Path $RoutingRoot "graph.obj"),
        (Join-Path $RoutingRoot "london-transit.gtfs.zip"),
        (Join-Path $RoutingRoot "ontario.osm.pbf"),
        (Join-Path $RoutingRoot "router-config.json"),
        (Join-Path $RoutingRoot "build-manifest.json"),
        $WorkerConfig,
        $PropertyCsv
    )
    $missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
    if ($missing.Count) {
        throw "Required local development files are missing:`n$($missing -join "`n")`nSee docs/local-development.md for setup instructions."
    }
}

function Get-LegacyMigrationMessage {
    param([string[]]$Names)

    return @"
Existing manually-created development containers were detected: $($Names -join ', ')

No containers were changed. To migrate to the canonical Compose environment:
1. preserve the existing PostgreSQL password in ignored config\local-dev.env,
2. verify the uwo-postgres-dev-data volume and data\routing files,
3. stop and remove the old containers only,
4. run .\scripts\dev.ps1 start,
5. verify database contents, routing smoke, and cache behavior.

Exact reviewed commands and rollback steps are in docs\local-development.md under "Migrating existing manual containers".
Never remove uwo-postgres-dev-data or data\routing.
"@
}

function Start-DevelopmentEnvironment {
    [void](Get-UwoPythonPath -RepositoryRoot $ProjectRoot)
    Assert-RequiredLocalFiles
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker CLI was not found. Install or start Docker Desktop, then rerun .\scripts\dev.ps1 start"
    }
    if (-not (Test-UwoDockerDaemon)) {
        throw "Docker Desktop is not running. Start Docker Desktop, then rerun .\scripts\dev.ps1 start"
    }
    if (-not (Test-Path -LiteralPath $DevCompose -PathType Leaf)) {
        throw "Canonical development Compose file is missing: $DevCompose"
    }
    $legacy = @(Get-UwoLegacyDevelopmentContainers)
    if ($legacy.Count) { throw (Get-LegacyMigrationMessage -Names $legacy) }

    if (Test-Path -LiteralPath $EnvFile -PathType Leaf) {
        $loadedEnvironment = Import-UwoLocalEnvironment -Path $EnvFile
        Write-Verbose "Loaded Compose environment keys: $($loadedEnvironment.LoadedKeys -join ', ')"
    }
    [void](Assert-UwoDevelopmentComposeEnvironment)
    if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable("OTP_BASE_URL", "Process"))) {
        [Environment]::SetEnvironmentVariable("OTP_BASE_URL", "http://127.0.0.1:8080", "Process")
    }
    [void](Assert-UwoLocalOtpUrl -Url $env:OTP_BASE_URL)
    $up = Invoke-UwoDocker -Arguments @("compose", "-f", $DevCompose, "up", "-d")
    if ($up.ExitCode -ne 0) {
        throw "Could not start canonical development services: $(Protect-UwoDiagnosticText $up.StdErr.Trim())"
    }
    $environmentState = Initialize-UwoLocalEnvironment -RepositoryRoot $ProjectRoot -EnvironmentFile $EnvFile -RequireDatabase
    Write-Verbose "Local environment source: database=$($environmentState.DatabaseSource)"

    if (-not (Wait-UwoPostgresReady -TimeoutSeconds 90)) {
        throw "Development PostgreSQL did not become ready within 90 seconds. Inspect it with: docker logs uwo-postgres-dev"
    }
    if (-not (Wait-UwoOtpReady -BaseUrl $env:OTP_BASE_URL -TimeoutSeconds 120)) {
        throw "OpenTripPlanner did not become ready within 120 seconds. Inspect it with: docker logs uwo-otp-router"
    }
    Write-DevelopmentStatus
}

function Stop-DevelopmentEnvironment {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Warning "Docker CLI is unavailable; no containers were changed."
        return
    }
    if (-not (Test-UwoDockerDaemon)) {
        Write-Warning "Docker Desktop is not running; containers are already inaccessible."
        return
    }
    $legacy = @(Get-UwoLegacyDevelopmentContainers)
    if ($legacy.Count) {
        Write-Warning "Legacy/manual development containers detected; stopping them without removing anything. Migrate using docs\local-development.md."
        foreach ($name in $legacy) {
            $state = Get-UwoContainerState -Name $name
            if ($state.Running) {
                $result = Invoke-UwoDocker -Arguments @("stop", $name)
                if ($result.ExitCode -ne 0) { Write-Warning "Could not stop $name`: $(Protect-UwoDiagnosticText $result.StdErr.Trim())" }
            }
        }
    } else {
        $previousPassword = [Environment]::GetEnvironmentVariable("POSTGRES_PASSWORD", "Process")
        try {
            if (Test-Path -LiteralPath $EnvFile -PathType Leaf) {
                [void](Import-UwoLocalEnvironment -Path $EnvFile)
            }
            if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable("POSTGRES_PASSWORD", "Process"))) {
                # Compose requires interpolation even though stop never creates a database.
                [Environment]::SetEnvironmentVariable("POSTGRES_PASSWORD", "compose-stop-placeholder", "Process")
            }
            $result = Invoke-UwoDocker -Arguments @("compose", "-f", $DevCompose, "stop")
        } finally {
            [Environment]::SetEnvironmentVariable("POSTGRES_PASSWORD", $previousPassword, "Process")
        }
        if ($result.ExitCode -ne 0) { throw "Could not stop canonical development services: $(Protect-UwoDiagnosticText $result.StdErr.Trim())" }
    }
    if (Test-Path -LiteralPath $TestCompose -PathType Leaf) {
        $result = Invoke-UwoDocker -Arguments @("compose", "-f", $TestCompose, "down")
        if ($result.ExitCode -ne 0) { Write-Warning "Could not stop the disposable PostgreSQL Compose environment: $($result.StdErr.Trim())" }
    }
    Write-Host "Stopped local development services. Containers, volumes, routing files, and run artifacts were retained." -ForegroundColor Green
}

function Invoke-Doctor {
    $rows = New-Object System.Collections.ArrayList
    $localEnvironmentValues = $null
    $python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    Add-Check $rows "Python executable" $(if (Test-Path -LiteralPath $python) { "PASS" } else { "FAIL" }) $(if (Test-Path -LiteralPath $python) { $python } else { "Create .venv and install requirements." })
    Add-Check $rows "Virtual environment" $(if (Test-Path -LiteralPath (Join-Path $ProjectRoot ".venv") -PathType Container) { "PASS" } else { "FAIL" })
    Add-Check $rows "Docker CLI" $(if (Get-Command docker -ErrorAction SilentlyContinue) { "PASS" } else { "FAIL" }) "Install Docker Desktop if missing."
    $dockerReady = Test-UwoDockerDaemon
    Add-Check $rows "Docker daemon" $(if ($dockerReady) { "PASS" } else { "FAIL" }) "Start Docker Desktop if unavailable."

    if (Test-Path -LiteralPath $EnvFile -PathType Leaf) {
        try {
            $values = ConvertFrom-UwoEnvFile -Path $EnvFile
            $localEnvironmentValues = $values
            if ($values.Contains("ACCESSIBILITY_DATABASE_URL")) { [void](Assert-UwoDevelopmentDatabaseUrl -Url $values.ACCESSIBILITY_DATABASE_URL) }
            if ($values.Contains("OTP_BASE_URL")) { [void](Assert-UwoLocalOtpUrl -Url $values.OTP_BASE_URL) }
            if ($values.Contains("POSTGRES_USER") -and $values.POSTGRES_USER -ne "uwo") { throw "POSTGRES_USER must be uwo for local development." }
            if ($values.Contains("POSTGRES_DB") -and $values.POSTGRES_DB -ne "uwo_housing_dev") { throw "POSTGRES_DB must be uwo_housing_dev for local development." }
            Add-Check $rows "Local environment file" "PASS" "Validated without displaying values."
        } catch {
            Add-Check $rows "Local environment file" "FAIL" $_.Exception.Message
        }
    } else {
        Add-Check $rows "Local environment file" "WARNING" "Copy config\local-dev.example.env to config\local-dev.env."
    }
    $hasComposePassword = (
        $null -ne $localEnvironmentValues -and
        $localEnvironmentValues.Contains("POSTGRES_PASSWORD") -and
        -not [string]::IsNullOrEmpty($localEnvironmentValues.POSTGRES_PASSWORD)
    ) -or -not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable("POSTGRES_PASSWORD", "Process"))
    Add-Check $rows "Compose PostgreSQL password" $(if ($hasComposePassword) { "PASS" } else { "WARNING" }) $(if ($hasComposePassword) { "Loaded without displaying it." } else { "Set POSTGRES_PASSWORD in ignored config\local-dev.env before migration/start." })

    if ((Test-Path -LiteralPath $DevCompose -PathType Leaf) -and (Get-Command docker -ErrorAction SilentlyContinue)) {
        try {
            $composeConfig = Get-UwoDevelopmentComposeConfig -ComposePath $DevCompose
            Add-Check $rows "Development Compose config" "PASS" "docker compose config parsed successfully."
            $hasServices = ($null -ne $composeConfig.services.postgres -and $null -ne $composeConfig.services.otp)
            Add-Check $rows "Development Compose services" $(if ($hasServices) { "PASS" } else { "FAIL" }) "Expected postgres and otp services."
            if ($hasServices) {
                $postgresHosts = @($composeConfig.services.postgres.ports | ForEach-Object { [string]$_.host_ip })
                $otpHosts = @($composeConfig.services.otp.ports | ForEach-Object { [string]$_.host_ip })
                Add-Check $rows "Compose PostgreSQL binding" $(if (Test-UwoLoopbackBindings $postgresHosts) { "PASS" } else { "FAIL" }) "Expected 127.0.0.1:55600:5432."
                Add-Check $rows "Compose OTP binding" $(if (Test-UwoLoopbackBindings $otpHosts) { "PASS" } else { "FAIL" }) "Expected 127.0.0.1:8080:8080."

                $postgresVolume = @($composeConfig.services.postgres.volumes | Where-Object { $_.target -eq "/var/lib/postgresql/data" }) | Select-Object -First 1
                $persistentVolume = (
                    $null -ne $postgresVolume -and
                    $postgresVolume.type -eq "volume" -and
                    $postgresVolume.source -eq "uwo-postgres-dev-data" -and
                    $composeConfig.volumes.'uwo-postgres-dev-data'.external
                )
                Add-Check $rows "Compose PostgreSQL volume" $(if ($persistentVolume) { "PASS" } else { "FAIL" }) "External volume uwo-postgres-dev-data."

                $otpMount = @($composeConfig.services.otp.volumes | Where-Object { $_.target -eq "/var/opentripplanner" }) | Select-Object -First 1
                $expectedRoutingPath = [IO.Path]::GetFullPath($RoutingRoot).TrimEnd('\')
                $routingMount = (
                    $null -ne $otpMount -and
                    $otpMount.type -eq "bind" -and
                    [IO.Path]::GetFullPath([string]$otpMount.source).TrimEnd('\') -eq $expectedRoutingPath -and
                    [bool]$otpMount.read_only
                )
                Add-Check $rows "Compose OTP routing mount" $(if ($routingMount) { "PASS" } else { "FAIL" }) "data\routing -> /var/opentripplanner (read-only)."
                $otpCommand = @($composeConfig.services.otp.command | ForEach-Object { [string]$_ })
                $loadOnly = ($otpCommand -contains "--load" -and $otpCommand -contains "--serve" -and $otpCommand -notcontains "--build")
                Add-Check $rows "Compose OTP start mode" $(if ($loadOnly) { "PASS" } else { "FAIL" }) "Loads and serves the existing graph; never builds it."
            }
        } catch {
            Add-Check $rows "Development Compose config" "FAIL" (Protect-UwoDiagnosticText $_.Exception.Message)
        }
    } else {
        Add-Check $rows "Development Compose config" "FAIL" "docker-compose.dev.yml or Docker CLI is missing."
    }

    try {
        [void](Assert-UwoDevelopmentDatabaseUrl -Url "postgresql://uwo:validation-only@127.0.0.1:55600/uwo_housing_dev")
        $remoteRejected = $false
        try { [void](Assert-UwoDevelopmentDatabaseUrl -Url "postgresql://uwo:validation-only@example.com/uwo_housing_dev") } catch { $remoteRejected = $true }
        Add-Check $rows "Development DB safety guard" $(if ($remoteRejected) { "PASS" } else { "FAIL" }) "Requires loopback and uwo_housing_dev; rejects remote/Supabase targets."
    } catch {
        Add-Check $rows "Development DB safety guard" "FAIL" $_.Exception.Message
    }

    if ($dockerReady) {
        $postgres = Get-UwoContainerState -Name "uwo-postgres-dev"
        $otp = Get-UwoContainerState -Name "uwo-otp-router"
        $legacy = @(Get-UwoLegacyDevelopmentContainers)
        Add-Check $rows "Container orchestration" $(if ($legacy.Count) { "WARNING" } else { "PASS" }) $(if ($legacy.Count) { "Legacy/manual containers require deliberate migration: $($legacy -join ', ')" } else { "Managed by docker-compose.dev.yml." })
        Add-Check $rows "PostgreSQL container" $(if ($postgres.Exists) { "PASS" } else { "FAIL" }) "Expected name: uwo-postgres-dev"
        Add-Check $rows "OTP container" $(if ($otp.Exists) { "PASS" } else { "FAIL" }) "Expected name: uwo-otp-router"
        if ($postgres.Exists) {
            $postgresBindings = @(Get-UwoContainerBindingAddresses -Name "uwo-postgres-dev")
            Add-Check $rows "PostgreSQL port binding" $(if (Test-UwoLoopbackBindings $postgresBindings) { "PASS" } else { "WARNING" }) $(if (Test-UwoLoopbackBindings $postgresBindings) { "Loopback only." } else { "Wildcard exposure detected; canonical migration fixes it." })
        }
        if ($otp.Exists) {
            $otpBindings = @(Get-UwoContainerBindingAddresses -Name "uwo-otp-router")
            Add-Check $rows "OTP port binding" $(if (Test-UwoLoopbackBindings $otpBindings) { "PASS" } else { "WARNING" }) $(if (Test-UwoLoopbackBindings $otpBindings) { "Loopback only." } else { "Wildcard exposure detected; canonical migration fixes it." })
        }
        $volume = Invoke-UwoDocker -Arguments @("volume", "inspect", "uwo-postgres-dev-data")
        Add-Check $rows "Development PostgreSQL volume" $(if ($volume.ExitCode -eq 0) { "PASS" } else { "FAIL" }) "Existing external volume uwo-postgres-dev-data."
    } else {
        Add-Check $rows "PostgreSQL container" "WARNING" "Not inspected because Docker is unavailable."
        Add-Check $rows "OTP container" "WARNING" "Not inspected because Docker is unavailable."
    }

    foreach ($item in @(
        @("OTP graph", (Join-Path $RoutingRoot "graph.obj")),
        @("OSM input", (Join-Path $RoutingRoot "ontario.osm.pbf")),
        @("GTFS input", (Join-Path $RoutingRoot "london-transit.gtfs.zip")),
        @("Router config", (Join-Path $RoutingRoot "router-config.json")),
        @("Routing manifest", (Join-Path $RoutingRoot "build-manifest.json")),
        @("Worker config", $WorkerConfig),
        @("Property CSV", $PropertyCsv),
        @("Hotspot config", (Join-Path $ProjectRoot "config\accessibility-hotspots.json")),
        @("Development Compose", $DevCompose),
        @("PostgreSQL test Compose", $TestCompose)
    )) {
        Add-Check $rows $item[0] $(if (Test-Path -LiteralPath $item[1] -PathType Leaf) { "PASS" } else { "FAIL" }) $item[1]
    }

    $ignoreResult = Invoke-UwoCapturedCommand -FilePath "git" -Arguments @("check-ignore", "config/local-dev.env", "data/dev-logs/probe.log") -WorkingDirectory $ProjectRoot
    Add-Check $rows "Git ignore rules" $(if ($ignoreResult.ExitCode -eq 0 -and @($ignoreResult.StdOut.Trim() -split "`r?`n").Count -eq 2) { "PASS" } else { "FAIL" }) "Local environment and developer logs must remain ignored."
    Add-Check $rows "Node" $(if (Get-Command node -ErrorAction SilentlyContinue) { "PASS" } else { "FAIL" }) "Required for frontend checks."
    Add-Check $rows "npm" $(if (Get-Command npm -ErrorAction SilentlyContinue) { "PASS" } else { "FAIL" }) "Required for frontend checks."
    Add-Check $rows "Frontend dependencies" $(if (Test-Path -LiteralPath (Join-Path $ProjectRoot "frontend\node_modules") -PathType Container) { "PASS" } else { "WARNING" }) "Run npm ci in frontend if missing."
    Write-StatusTable $rows
    if (@($rows | Where-Object Status -eq "FAIL").Count) { return 1 }
    return 0
}

try {
    Set-Location $ProjectRoot
    switch ($Command) {
        "start" { Start-DevelopmentEnvironment }
        "stop" { Stop-DevelopmentEnvironment }
        "status" { Write-DevelopmentStatus }
        "restart" { Stop-DevelopmentEnvironment; Start-DevelopmentEnvironment }
        "doctor" {
            $doctorExit = Invoke-Doctor
            if ($doctorExit -ne 0) { exit $doctorExit }
        }
    }
} catch {
    Write-Error $_.Exception.Message
    exit 1
}

exit 0
