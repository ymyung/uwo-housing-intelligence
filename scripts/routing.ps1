[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("preflight", "smoke", "poc", "cache", "latest", "gtfs-status", "gtfs-stage")]
    [string]$Command = "latest",
    [int]$PropertyId,
    [string]$Config,
    [string]$Properties,
    [string]$EnvFile,
    [string]$GtfsSource,
    [string]$Output,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Import-Module (Join-Path $PSScriptRoot "lib\LocalDevTools.psm1") -Force
$Python = Get-UwoPythonPath -RepositoryRoot $ProjectRoot
if (-not $Config) { $Config = Join-Path $ProjectRoot "config\accessibility-worker.toml" }
if (-not $Properties) { $Properties = Join-Path $ProjectRoot "config\accessibility-poc-properties.csv" }
if (-not $EnvFile) { $EnvFile = Join-Path $ProjectRoot "config\local-dev.env" }
$HotspotId = "western-main-campus"

function Get-Value {
    param([object]$Object, [string]$Name, $Default = 0)
    if ($null -eq $Object) { return $Default }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) { return $Default }
    return $property.Value
}

function Invoke-Worker {
    param([string[]]$Arguments)

    Write-Verbose ("Worker command: .venv\Scripts\python.exe " + ($Arguments -join " "))
    $result = Invoke-UwoCapturedCommand -FilePath $Python -Arguments $Arguments -WorkingDirectory $ProjectRoot
    if ($result.ExitCode -ne 0) {
        $diagnostic = Protect-UwoDiagnosticText (($result.StdErr + "`n" + $result.StdOut).Trim())
        if ($diagnostic -match "(?i)connection refused|actively refused") {
            throw "OpenTripPlanner is not running.`nStart it with:`n.\scripts\dev.ps1 start`n`nUnderlying error:`n$diagnostic"
        }
        throw "Accessibility worker failed with exit code $($result.ExitCode).`n$diagnostic"
    }
    if ($VerbosePreference -eq "Continue" -and $result.StdErr.Trim()) {
        Write-Verbose (Protect-UwoDiagnosticText $result.StdErr.Trim())
    }
    try { return $result.StdOut | ConvertFrom-Json } catch {
        throw "Accessibility worker returned malformed JSON. Sanitized output: $(Protect-UwoDiagnosticText $result.StdOut)"
    }
}

function Get-ReviewedRows {
    if (-not (Test-Path -LiteralPath $Properties -PathType Leaf)) {
        throw "Reviewed property CSV is missing: $Properties`nCreate it from config\accessibility-poc-properties.example.csv before routing."
    }
    $rows = @(Import-Csv -LiteralPath $Properties)
    if (-not $rows.Count) { throw "Reviewed property CSV contains no properties: $Properties" }
    return $rows
}

function Assert-RoutingConfiguration {
    if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) {
        throw "Local accessibility worker config is missing: $Config`nCopy config\accessibility-worker.example.toml to config\accessibility-worker.toml and review it."
    }
    [void](Get-ReviewedRows)
    $baseUrlMatch = Select-String -LiteralPath $Config -Pattern '^\s*base_url\s*=\s*"(?<url>[^"]+)"\s*$' | Select-Object -First 1
    if (-not $baseUrlMatch) { throw "The worker config does not define routing.base_url." }
    [void](Assert-UwoLocalOtpUrl -Url $baseUrlMatch.Matches[0].Groups["url"].Value)
    $environmentState = Initialize-UwoLocalEnvironment -RepositoryRoot $ProjectRoot -EnvironmentFile $EnvFile -RequireDatabase
    Write-Verbose "Local database source: $($environmentState.DatabaseSource)"
}

function Get-RunPaths {
    param([string]$RunId)
    $root = Join-Path $ProjectRoot "data\accessibility-runs\$RunId"
    return [pscustomobject]@{
        Root = $root
        Manifest = Join-Path $root "manifest.json"
        Results = Join-Path $root "route-results.jsonl"
        Quality = Join-Path $root "quality-report.csv"
        Review = Join-Path $root "review-required.csv"
    }
}

function Write-RunMetrics {
    param([object]$Manifest)
    $metrics = $Manifest.metrics
    $requested = [int](Get-Value $Manifest "success_count") + [int](Get-Value $Manifest "failure_count")
    $paths = Get-RunPaths -RunId $Manifest.run_id
    $reviewCount = if (Test-Path -LiteralPath $paths.Review) { @(Import-Csv -LiteralPath $paths.Review).Count } else { 0 }
    Write-Host "Properties processed:  $($Manifest.selected_property_count)"
    Write-Host "Profiles requested:    $requested"
    Write-Host "Profiles created:      $(Get-Value $metrics 'profiles_created')"
    Write-Host "Cache hits:            $(Get-Value $metrics 'cache_hits')"
    Write-Host "Provider calls:        $(Get-Value $metrics 'provider_calls')"
    Write-Host "Database writes:       $(Get-Value $metrics 'database_writes')"
    Write-Host "Failures:              $(Get-Value $Manifest 'failure_count')"
    Write-Host "Warnings:              $(Get-Value $Manifest 'quality_warnings')"
    Write-Host "Review-required count: $reviewCount"
    Write-Host "Run ID:                $($Manifest.run_id)"
    Write-Host "Run status:            $($Manifest.status)"
}

function Get-PeriodLabel {
    param([string]$Mode, [string]$Period)
    if ($Mode -eq "walking") { return "Walking" }
    if ($Mode -eq "cycling") { return "Cycling" }
    $labels = @{
        weekday_morning_commute = "Weekday morning"
        weekday_midday = "Weekday midday"
        weekday_evening_commute = "Weekday evening"
        weekday_late_evening = "Late evening"
        saturday_daytime = "Saturday daytime"
        sunday_daytime = "Sunday daytime"
    }
    if ($labels.ContainsKey($Period)) { return $labels[$Period] }
    return "Transit $Period"
}

function Write-RouteSummary {
    param([string]$RunId)
    $paths = Get-RunPaths -RunId $RunId
    if (-not (Test-Path -LiteralPath $paths.Quality -PathType Leaf)) {
        throw "Run $RunId does not have quality-report.csv."
    }
    $rows = @(Import-Csv -LiteralPath $paths.Quality)
    $propertyIds = @($rows | Select-Object -ExpandProperty property_id -Unique)
    Write-Host ""
    Write-Host "Property $($propertyIds -join ', ') -> Western main campus" -ForegroundColor Cyan
    Write-Host ("{0,-28} {1,10}  {2}" -f "Mode / Period", "Typical", "Status")
    Write-Host ("-" * 52)
    foreach ($row in $rows) {
        $label = Get-PeriodLabel -Mode $row.mode -Period $row.time_period
        $typical = if ($row.representative_duration_seconds) { "{0:N1} min" -f ([double]$row.representative_duration_seconds / 60) } else { "n/a" }
        $status = if ($row.decision -in @("manual_review_required", "failed")) {
            "FAIL"
        } elseif ($row.reason_codes -or $row.decision -eq "accepted_with_warning") {
            "WARNING"
        } else {
            "PASS"
        }
        Write-Host ("{0,-28} {1,10}  {2}" -f $label, $typical, $status)
    }
    $warnings = @($rows | Where-Object { $_.reason_codes })
    if ($warnings.Count) {
        Write-Host "`nWarnings:" -ForegroundColor Yellow
        foreach ($row in $warnings) {
            Write-Host "- $(if ($row.time_period) { $row.time_period } else { $row.mode }): $($row.reason_codes)"
        }
    }
}

function Write-LatestSummary {
    $latest = Get-UwoLatestAccessibilityRun
    if (-not $latest) { throw "No complete accessibility run directory with manifest.json was found." }
    $manifest = $latest.Manifest
    $metrics = $manifest.metrics
    Write-Host "Run ID:          $($latest.RunId)"
    Write-Host "Status:          $($manifest.status)"
    Write-Host "Property count:  $($manifest.selected_property_count)"
    Write-Host "Hotspot count:   $($manifest.selected_hotspot_count)"
    Write-Host "Requested modes: $($manifest.requested_modes -join ', ')"
    Write-Host "Cache hits:      $(Get-Value $metrics 'cache_hits')"
    Write-Host "Provider calls:  $(Get-Value $metrics 'provider_calls')"
    Write-Host "Database writes: $(Get-Value $metrics 'database_writes')"
    Write-Host "Success count:   $(Get-Value $manifest 'success_count')"
    Write-Host "Failure count:   $(Get-Value $manifest 'failure_count')"
    Write-Host "Quality warnings:$(Get-Value $manifest 'quality_warnings')"
    $paths = Get-RunPaths -RunId $latest.RunId
    if (Test-Path -LiteralPath $paths.Quality -PathType Leaf) {
        $qualityRows = @(Import-Csv -LiteralPath $paths.Quality)
        Write-Host "`nQuality status summary" -ForegroundColor Cyan
        foreach ($group in $qualityRows | Group-Object quality_status | Sort-Object Name) {
            Write-Host ("- {0}: {1}" -f $(if ($group.Name) { $group.Name } else { "unknown" }), $group.Count)
        }
        Write-Host "Reason-code summary" -ForegroundColor Cyan
        $codes = @($qualityRows | ForEach-Object { $_.reason_codes -split ',' } | Where-Object { $_ })
        if ($codes.Count) {
            foreach ($group in $codes | Group-Object | Sort-Object Name) { Write-Host "- $($group.Name): $($group.Count)" }
        } else {
            Write-Host "- none"
        }
    }
    return $latest
}

function Get-CacheCallExplanation {
    param([object]$Previous, [object]$Current)
    if (-not $Previous) { return "no earlier completed run existed for comparison" }
    if (($Previous.Manifest.requested_modes -join ",") -ne ($Current.requested_modes -join ",") -or $Previous.Manifest.selected_property_count -ne $Current.selected_property_count) {
        return "the reviewed scope or requested modes changed"
    }
    $oldFingerprint = $Previous.Manifest.input_fingerprints | ConvertTo-Json -Compress -Depth 8
    $newFingerprint = $Current.input_fingerprints | ConvertTo-Json -Compress -Depth 8
    if ($oldFingerprint -ne $newFingerprint) { return "routing, schedule, network, hotspot, or property fingerprints changed" }
    if ($Previous.Manifest.failure_count -gt 0) { return "the prior run contained failed profiles"
    }
    $oldPaths = Get-RunPaths -RunId $Previous.RunId
    if (Test-Path -LiteralPath $oldPaths.Results -PathType Leaf) {
        $expired = $false
        foreach ($line in Get-Content -LiteralPath $oldPaths.Results) {
            if (-not $line.Trim()) { continue }
            $row = $line | ConvertFrom-Json
            if ($row.profile -and $row.profile.expires_at -and [DateTimeOffset]::Parse($row.profile.expires_at) -le [DateTimeOffset]::UtcNow) { $expired = $true; break }
        }
        if ($expired) { return "one or more prior profiles expired" }
    }
    return $null
}

try {
    Set-Location $ProjectRoot
    if ($Command -eq "latest") {
        [void](Write-LatestSummary)
        exit 0
    }

    if ($Command -in @("gtfs-status", "gtfs-stage")) {
        $gtfsArguments = @("-m", "scripts.manage_gtfs")
        if ($Command -eq "gtfs-status") {
            $gtfsArguments += @("status", "--config", $Config)
            if ($Output) { $gtfsArguments += @("--output", $Output) }
        } else {
            if (-not $GtfsSource) {
                throw "gtfs-stage requires -GtfsSource with a verified official URL or local GTFS ZIP."
            }
            $gtfsArguments += @("stage", "--config", $Config, "--source", $GtfsSource)
            if ($DryRun) { $gtfsArguments += "--dry-run" }
        }
        $result = Invoke-UwoCapturedCommand -FilePath $Python -Arguments $gtfsArguments -WorkingDirectory $ProjectRoot
        if ($result.ExitCode -ne 0) {
            throw "GTFS $Command failed.`n$(Protect-UwoDiagnosticText (($result.StdErr + "`n" + $result.StdOut).Trim()))"
        }
        $gtfs = $result.StdOut | ConvertFrom-Json
        Write-Host "Feed version:             $($gtfs.feed_version)"
        Write-Host "Service range:            $($gtfs.service_start_date) through $($gtfs.service_end_date)"
        Write-Host "Reference week supported: $($gtfs.reference_week_supported)"
        Write-Host "Expired as of $($gtfs.as_of_date):  $($gtfs.expired)"
        Write-Host "Graph feed matches:       $($gtfs.graph_feed_fingerprint_matches)"
        $stagedDirectory = Get-Value $gtfs "staged_directory" $null
        if ($stagedDirectory) { Write-Host "Staged directory:         $stagedDirectory" }
        exit 0
    }

    Assert-RoutingConfiguration
    $rows = @(Get-ReviewedRows)
    if ($Command -eq "preflight") {
        $arguments = Get-UwoRoutingWorkerArguments -Command preflight -ConfigPath $Config -PropertiesPath $Properties -HotspotId $HotspotId
        $result = Invoke-Worker -Arguments $arguments
        Write-Host "Routing endpoint:    $($result.routing_endpoint)"
        Write-Host "Database:            $($result.database)"
        Write-Host "Selected properties: $($result.selected_property_count)"
        Write-Host "Selected hotspots:   $($result.selected_hotspot_count)"
        Write-Host "Router version:      $($result.routing_metadata.router_version)"
        Write-Host "Network version:     $($result.routing_metadata.network_version)"
        Write-Host "Schedule version:    $($result.routing_metadata.schedule_version)"
        exit 0
    }

    $selectedIds = @()
    if ($Command -eq "smoke") {
        if ($PSBoundParameters.ContainsKey("PropertyId")) {
            $selected = @($rows | Where-Object { [int]$_.property_id -eq $PropertyId })
        } else {
            $selected = @($rows | Select-Object -First 1)
        }
        if (@($selected).Count -eq 0) { throw "Property $PropertyId is not present in the reviewed property CSV." }
        $selectedIds = @([string]$selected[0].property_id)
    } else {
        $selectedIds = @($rows | ForEach-Object { [string]$_.property_id })
    }

    $previous = Get-UwoLatestAccessibilityRun
    $arguments = Get-UwoRoutingWorkerArguments -Command run -ConfigPath $Config -PropertiesPath $Properties -PropertyIds $selectedIds -Modes "walking,cycling,transit" -HotspotId $HotspotId
    $manifest = Invoke-Worker -Arguments $arguments
    if ($Command -eq "smoke") {
        Write-RouteSummary -RunId $manifest.run_id
        Write-Host "`nRun ID: $($manifest.run_id)"
    } else {
        Write-RunMetrics -Manifest $manifest
    }

    if ($Command -eq "cache") {
        $providerCalls = [int](Get-Value $manifest.metrics "provider_calls")
        if ($providerCalls -eq 0) {
            Write-Host "Cache verification: PASS - all compatible profiles were cache hits." -ForegroundColor Green
        } else {
            $reason = Get-CacheCallExplanation -Previous $previous -Current $manifest
            if ($reason) {
                Write-Host "Cache verification: provider calls were legitimate because $reason." -ForegroundColor Yellow
            } else {
                throw "Cache verification failed: $providerCalls provider calls occurred despite a compatible, unexpired, fully populated prior POC run."
            }
        }
    }
} catch {
    Write-Error (Protect-UwoDiagnosticText $_.Exception.Message)
    exit 1
}

exit 0
