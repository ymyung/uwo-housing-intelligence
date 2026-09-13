[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("fast", "normal", "full")]
    [string]$Level = "fast",
    [ValidateSet("routing")]
    [string]$Area,
    [switch]$PlanOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Import-Module (Join-Path $PSScriptRoot "lib\LocalDevTools.psm1") -Force
$Python = Get-UwoPythonPath -RepositoryRoot $ProjectRoot
$FrontendRoot = Join-Path $ProjectRoot "frontend"
$TestCompose = Join-Path $ProjectRoot "docker-compose.postgres-test.yml"

function Get-TestPlan {
    param([string]$SelectedLevel)

    if ($SelectedLevel -eq "fast") {
        return @("Python focused offline")
    }
    $plan = @("Python offline")
    if ($SelectedLevel -eq "full") { $plan += "PostgreSQL" }
    $plan += "Compile"
    $plan += "Frontend tests"
    if ($SelectedLevel -eq "full") {
        $plan += "Frontend lint"
        $plan += "Frontend build"
    }
    $plan += "Git diff check"
    return $plan
}

if ($PlanOnly) {
    Write-Host "Test plan: $Level"
    foreach ($stage in Get-TestPlan -SelectedLevel $Level) { Write-Host "- $stage" }
    if ($Level -eq "full") { Write-Host "PostgreSQL cleanup: ALWAYS (try/finally, Compose down without --volumes)" }
    exit 0
}

$Results = New-Object System.Collections.ArrayList

function Add-Result {
    param([string]$Name, [string]$Status, [double]$Seconds, [string]$Detail = "")
    [void]$Results.Add([pscustomobject]@{ Name = $Name; Status = $Status; Seconds = $Seconds; Detail = $Detail })
}

function Invoke-TestStage {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory = $ProjectRoot
    )

    Write-Host "`n[$Name]" -ForegroundColor Cyan
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $result = Invoke-UwoCapturedCommand -FilePath $FilePath -Arguments $Arguments -WorkingDirectory $WorkingDirectory
        if ($result.StdOut.Trim()) { Write-Host $result.StdOut.TrimEnd() }
        if ($result.StdErr.Trim()) { Write-Host (Protect-UwoDiagnosticText $result.StdErr.TrimEnd()) -ForegroundColor DarkYellow }
        $timer.Stop()
        if ($result.ExitCode -eq 0) {
            Add-Result $Name "PASS" $timer.Elapsed.TotalSeconds
            return $true
        }
        Add-Result $Name "FAIL" $timer.Elapsed.TotalSeconds "exit $($result.ExitCode)"
        return $false
    } catch {
        $timer.Stop()
        Write-Host $_.Exception.Message -ForegroundColor Red
        Add-Result $Name "FAIL" $timer.Elapsed.TotalSeconds $_.Exception.Message
        return $false
    }
}

function Wait-TestPostgres {
    param([string]$ContainerId, [int]$TimeoutSeconds = 60)

    $environment = Get-UwoContainerEnvironment -Name $ContainerId
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $ready = Invoke-UwoDocker -Arguments @("exec", $ContainerId, "pg_isready", "-U", $environment.POSTGRES_USER, "-d", $environment.POSTGRES_DB)
        if ($ready.ExitCode -eq 0) { return $true }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Invoke-PostgresSuite {
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $previousTestUrl = [Environment]::GetEnvironmentVariable("TEST_DATABASE_URL", "Process")
    $composeStarted = $false
    $suitePassed = $false
    $detail = ""
    Write-Host "`n[PostgreSQL]" -ForegroundColor Cyan
    try {
        if (-not (Test-UwoDockerDaemon)) {
            throw "Docker Desktop is required for PostgreSQL integration tests. Start it or use test.ps1 normal."
        }
        $up = Invoke-UwoDocker -Arguments @("compose", "-f", $TestCompose, "up", "-d", "postgres-test")
        if ($up.ExitCode -ne 0) { throw "Could not start disposable PostgreSQL: $($up.StdErr.Trim())" }
        $composeStarted = $true

        $idResult = Invoke-UwoDocker -Arguments @("compose", "-f", $TestCompose, "ps", "-q", "postgres-test")
        $containerId = $idResult.StdOut.Trim()
        if ($idResult.ExitCode -ne 0 -or -not $containerId) { throw "Disposable PostgreSQL container ID could not be resolved." }
        if (-not (Wait-TestPostgres -ContainerId $containerId)) { throw "Disposable PostgreSQL did not become ready within 60 seconds." }

        $testUrl = Get-UwoPostgresUrlFromContainer -Name $containerId
        [Environment]::SetEnvironmentVariable("TEST_DATABASE_URL", $testUrl, "Process")
        $safety = Invoke-UwoCapturedCommand -FilePath $Python -Arguments @(
            "-c",
            "import os; from scripts.postgres_test_safety import approve_test_database_environment; approve_test_database_environment(os.environ)"
        ) -WorkingDirectory $ProjectRoot
        if ($safety.ExitCode -ne 0) { throw "The disposable database failed the repository TEST_DATABASE_URL safety policy." }

        $pytest = Invoke-UwoCapturedCommand -FilePath $Python -Arguments @("-m", "pytest", "-m", "postgres and not r5", "-v") -WorkingDirectory $ProjectRoot
        if ($pytest.StdOut.Trim()) { Write-Host $pytest.StdOut.TrimEnd() }
        if ($pytest.StdErr.Trim()) { Write-Host (Protect-UwoDiagnosticText $pytest.StdErr.TrimEnd()) -ForegroundColor DarkYellow }
        if ($pytest.ExitCode -ne 0) {
            $detail = "pytest exit $($pytest.ExitCode)"
        } else {
            $suitePassed = $true
        }
    } catch {
        $detail = $_.Exception.Message
        Write-Host $detail -ForegroundColor Red
    } finally {
        [Environment]::SetEnvironmentVariable("TEST_DATABASE_URL", $previousTestUrl, "Process")
        if ($composeStarted) {
            $down = Invoke-UwoDocker -Arguments @("compose", "-f", $TestCompose, "down")
            if ($down.ExitCode -ne 0) {
                $suitePassed = $false
                $detail = "PostgreSQL tests finished, but Compose cleanup failed. $($down.StdErr.Trim())"
                Write-Host $detail -ForegroundColor Red
            }
        }
    }
    $timer.Stop()
    Add-Result "PostgreSQL" $(if ($suitePassed) { "PASS" } else { "FAIL" }) $timer.Elapsed.TotalSeconds $detail
    return $suitePassed
}

Set-Location $ProjectRoot
$overallTimer = [System.Diagnostics.Stopwatch]::StartNew()

if ($Level -eq "fast") {
    $expression = "routing_provider or accessibility_worker or accessibility_quality or accessibility_profiles or accessibility_demo or ranking_v1 or ranking_api or london_reference or london_mobility or mobility_context"
    if ($Area -eq "routing") {
        $expression = "routing_provider or accessibility_worker or accessibility_quality or accessibility_profiles or accessibility_demo or ranking_v1 or ranking_api or london_reference or london_mobility or mobility_context"
    }
    [void](Invoke-TestStage -Name "Python focused offline" -FilePath $Python -Arguments @("-m", "pytest", "-m", "not postgres", "-k", $expression))
} else {
    [void](Invoke-TestStage -Name "Python offline" -FilePath $Python -Arguments @("-m", "pytest", "-m", "not postgres"))
    if ($Level -eq "full") { [void](Invoke-PostgresSuite) }
    [void](Invoke-TestStage -Name "Compile" -FilePath $Python -Arguments @("-m", "compileall", "pipeline", "scripts", "backend"))

    if (Test-Path -LiteralPath (Join-Path $FrontendRoot "package.json") -PathType Leaf) {
        if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
            Add-Result "Frontend tests" "FAIL" 0 "npm was not found"
            if ($Level -eq "full") {
                Add-Result "Frontend lint" "FAIL" 0 "npm was not found"
                Add-Result "Frontend build" "FAIL" 0 "npm was not found"
            }
        } elseif (-not (Test-Path -LiteralPath (Join-Path $FrontendRoot "node_modules") -PathType Container)) {
            Add-Result "Frontend tests" "FAIL" 0 "Run npm ci in frontend first"
            if ($Level -eq "full") {
                Add-Result "Frontend lint" "FAIL" 0 "Run npm ci in frontend first"
                Add-Result "Frontend build" "FAIL" 0 "Run npm ci in frontend first"
            }
        } else {
            $npmPath = (Get-Command npm.cmd -ErrorAction Stop).Source
            [void](Invoke-TestStage -Name "Frontend tests" -FilePath $npmPath -Arguments @("test") -WorkingDirectory $FrontendRoot)
            if ($Level -eq "full") {
                [void](Invoke-TestStage -Name "Frontend lint" -FilePath $npmPath -Arguments @("run", "lint") -WorkingDirectory $FrontendRoot)
                [void](Invoke-TestStage -Name "Frontend build" -FilePath $npmPath -Arguments @("run", "build") -WorkingDirectory $FrontendRoot)
            }
        }
    }
    [void](Invoke-TestStage -Name "Git diff check" -FilePath "git" -Arguments @("diff", "--check"))
}

$overallTimer.Stop()
Write-Host "`nResult summary" -ForegroundColor Cyan
Write-Host ("{0,-24} {1,-7} {2,9}  {3}" -f "Stage", "Result", "Duration", "Detail")
Write-Host ("-" * 76)
foreach ($row in $Results) {
    $color = if ($row.Status -eq "PASS") { "Green" } else { "Red" }
    Write-Host ("{0,-24} {1,-7} {2,8:N1}s  {3}" -f $row.Name, $row.Status, $row.Seconds, $row.Detail) -ForegroundColor $color
}
Write-Host ("Total: {0:N1}s" -f $overallTimer.Elapsed.TotalSeconds)

if (@($Results | Where-Object Status -eq "FAIL").Count) { exit 1 }
exit 0
