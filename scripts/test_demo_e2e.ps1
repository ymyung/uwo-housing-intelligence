[CmdletBinding()]
param(
    [string]$FrontendUrl = "http://127.0.0.1:5173",
    [string]$ApiUrl = "http://127.0.0.1:8000",
    [string]$OutputDirectory = "data/demo-e2e/test-results"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Repository virtual environment was not found: $Python"
}

try {
    [void](Invoke-WebRequest -UseBasicParsing -Uri "$ApiUrl/api/hotspots" -TimeoutSec 5)
    [void](Invoke-WebRequest -UseBasicParsing -Uri $FrontendUrl -TimeoutSec 5)
} catch {
    throw "The local frontend and API must be running. Start .\scripts\dev.ps1 start, .\scripts\serve_local_api.ps1, and npm run dev in frontend."
}

$previousBaseUrl = [Environment]::GetEnvironmentVariable("E2E_BASE_URL", "Process")
$previousOutput = [Environment]::GetEnvironmentVariable("E2E_OUTPUT_DIR", "Process")
try {
    [Environment]::SetEnvironmentVariable("E2E_BASE_URL", $FrontendUrl, "Process")
    [Environment]::SetEnvironmentVariable("E2E_OUTPUT_DIR", (Join-Path $ProjectRoot $OutputDirectory), "Process")
    Set-Location $ProjectRoot
    & $Python -m pytest tests/e2e -m e2e -v
    exit $LASTEXITCODE
} finally {
    [Environment]::SetEnvironmentVariable("E2E_BASE_URL", $previousBaseUrl, "Process")
    [Environment]::SetEnvironmentVariable("E2E_OUTPUT_DIR", $previousOutput, "Process")
}
