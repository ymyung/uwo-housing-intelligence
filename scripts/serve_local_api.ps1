[CmdletBinding()]
param(
    [int]$Port = 8000,
    [switch]$Reload,
    [string]$EnvFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ModulePath = Join-Path $PSScriptRoot "lib\LocalDevTools.psm1"
Import-Module $ModulePath -Force
if (-not $EnvFile) { $EnvFile = Join-Path $ProjectRoot "config\local-dev.env" }

# This occurs in the same process that starts Uvicorn, so the API receives the
# derived loopback URL without placing a credential in command history.
[void](Initialize-UwoLocalEnvironment -RepositoryRoot $ProjectRoot -EnvironmentFile $EnvFile -RequireDatabase)
$localDatabaseUrl = [Environment]::GetEnvironmentVariable("ACCESSIBILITY_DATABASE_URL", "Process")
if ([string]::IsNullOrEmpty($localDatabaseUrl)) {
    throw "Local PostgreSQL configuration could not be derived; the API was not started. Run .\scripts\dev.ps1 start and correct config\local-dev.env."
}
# This is the explicit local launcher, so a parent shell's production
# DATABASE_URL must not take precedence over the validated loopback target.
[Environment]::SetEnvironmentVariable("DATABASE_URL", $localDatabaseUrl, "Process")
$python = Get-UwoPythonPath -RepositoryRoot $ProjectRoot
$arguments = @("-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "$Port")
if ($Reload) { $arguments += "--reload" }

& $python @arguments
exit $LASTEXITCODE
