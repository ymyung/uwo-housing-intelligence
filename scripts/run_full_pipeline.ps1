param(
    [string]$RunDir,
    [ValidateSet("stage0", "stage1", "stage2", "manual_fixes", "stage3", "stage3_qc")]
    [string]$StartAtStage = "stage0",
    [string]$PythonPath,
    [string]$GeocodeCache = "data\processed\geocode_cache.csv",
    [string]$OllamaModel = "qwen2.5:14b-instruct",
    [switch]$SmokeTest,
    [switch]$SkipAI,
    [switch]$SkipManualFixes,
    [switch]$SkipGeocoding,
    [switch]$CacheOnlyGeocoding,
    [switch]$SkipLaterStages,
    # Retained so the legacy startup wrapper does not fail argument binding.
    [switch]$SkipOTP,
    [switch]$SkipImages,
    [switch]$ImportToSupabase
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot

$VenvPython = if ($PythonPath) {
    if ([System.IO.Path]::IsPathRooted($PythonPath)) { $PythonPath } else { Join-Path $ProjectRoot $PythonPath }
} else {
    Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
$Python = if (Test-Path $VenvPython) { $VenvPython } elseif ($PythonPath) {
    throw "Configured Python executable does not exist: $VenvPython"
} else { "python" }

$StageOrder = @{
    "stage0" = 0
    "stage1" = 1
    "stage2" = 2
    "manual_fixes" = 3
    "stage3" = 4
    "stage3_qc" = 5
}

function Test-StageSelected {
    param([string]$Stage)
    return $StageOrder[$Stage] -ge $StageOrder[$StartAtStage]
}

function Invoke-PipelinePython {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Pipeline command failed with exit code ${LASTEXITCODE}: python $($Arguments -join ' ')"
    }
}

if ($ImportToSupabase) {
    throw "Database import is intentionally outside the Stage 0-to-3 orchestrator."
}

if (-not $RunDir) {
    $RunDir = (& $Python -m pipeline.run_context create --runs-root data\runs).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Could not create pipeline run." }
}
$RunDir = (Resolve-Path $RunDir).Path

Write-Host "=== UWO Housing Stage 0-to-3 Pipeline ===" -ForegroundColor Cyan
Write-Host "Run directory: $RunDir"

$RunMode = if ($StartAtStage -eq "stage0") { "--resume" } else { "--overwrite" }
$Stage0Args = @("scraper\collect_listing_urls.py", "--run-dir", $RunDir, $RunMode)
$Stage1Args = @("pipeline\uwo_listing_enricher.py", "--run-dir", $RunDir, $RunMode)
$Stage2Args = @("pipeline\ai_enricher.py", "--run-dir", $RunDir, $RunMode, "--consensus", "--model", $OllamaModel)
$Stage3Args = @(
    "pipeline\geocoder.py", "--run-dir", $RunDir, $RunMode,
    "--cache-csv", $GeocodeCache
)

if ($SmokeTest) {
    $Stage0Args += @("--max-pages", "1")
    $Stage1Args += @("--limit", "5")
    $Stage2Args += @("--limit", "5")
    $Stage3Args += @("--limit", "5")
}

if (Test-StageSelected "stage0") {
    Write-Host "[Stage 0] Listing discovery" -ForegroundColor Yellow
    Invoke-PipelinePython @Stage0Args
}

if (Test-StageSelected "stage1") {
    Write-Host "[Stage 1] Website detail extraction" -ForegroundColor Yellow
    Invoke-PipelinePython @Stage1Args
}

if (Test-StageSelected "stage2") {
    Write-Host "[Stage 2] AI enrichment" -ForegroundColor Yellow
    if ($SkipAI) { $Stage2Args += "--skip" }
    Invoke-PipelinePython @Stage2Args
}

$ManualArgs = @("scripts\apply_manual_review_fixes.py", "--run-dir", $RunDir, $RunMode)
if (Test-StageSelected "manual_fixes") {
    Write-Host "[Manual fixes] Known review corrections" -ForegroundColor Yellow
    if ($SkipManualFixes) { $ManualArgs += "--skip" }
    Invoke-PipelinePython @ManualArgs
}

if ($SkipGeocoding -and (Test-StageSelected "stage3")) {
    Write-Host "[Stage 3] Explicitly skipped" -ForegroundColor DarkYellow
    Invoke-PipelinePython -m pipeline.run_context skip --run-dir $RunDir `
        --stage stage3 --reason "Geocoding explicitly disabled."
    Invoke-PipelinePython -m pipeline.run_context skip --run-dir $RunDir `
        --stage stage3_qc --reason "Geocoding QC cannot run without Stage 3."
} elseif (Test-StageSelected "stage3") {
    Write-Host "[Stage 3] Geocoding" -ForegroundColor Yellow
    if ($CacheOnlyGeocoding) { $Stage3Args += "--cache-only" }
    Invoke-PipelinePython @Stage3Args

}

if ((-not $SkipGeocoding) -and (Test-StageSelected "stage3_qc")) {
    Write-Host "[Stage 3 QC] Canonical and review outputs" -ForegroundColor Yellow
    Invoke-PipelinePython scripts\apply_geocode_qc.py --run-dir $RunDir $RunMode
}

if ($SkipLaterStages -or $SkipOTP -or $SkipImages) {
    Write-Host "Later transit, OTP, and image stages explicitly skipped." -ForegroundColor DarkYellow
} else {
    Write-Host "Later stages are not part of this Stage 0-to-3 run." -ForegroundColor DarkYellow
}

Invoke-PipelinePython -m pipeline.run_context finalize --run-dir $RunDir | Out-Null
$Manifest = Get-Content (Join-Path $RunDir "manifest.json") -Raw | ConvertFrom-Json
$StageSummary = $Manifest.stages.PSObject.Properties | ForEach-Object {
    "$($_.Name)=$($_.Value.status)"
}

Write-Host "Run status: $($Manifest.status)" -ForegroundColor Green
Write-Host "Stages: $($StageSummary -join ', ')"
Write-Host "Warnings: $($Manifest.warnings.Count); Errors: $($Manifest.errors.Count)"
Write-Host "Final run directory: $RunDir"
