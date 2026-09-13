Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-UwoRepositoryRoot {
    [CmdletBinding()]
    param()

    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Get-UwoPythonPath {
    [CmdletBinding()]
    param([string]$RepositoryRoot = (Resolve-UwoRepositoryRoot))

    $python = Join-Path $RepositoryRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "The repository virtual environment is missing. Create .venv and install the project requirements first."
    }
    return $python
}

function ConvertFrom-UwoEnvFile {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Local environment file does not exist: $Path"
    }
    $values = [ordered]@{}
    $lineNumber = 0
    foreach ($line in Get-Content -LiteralPath $Path) {
        $lineNumber++
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $separator = $trimmed.IndexOf("=")
        if ($separator -lt 1) {
            throw "Malformed local environment entry at line ${lineNumber}: expected KEY=VALUE."
        }
        $key = $trimmed.Substring(0, $separator).Trim()
        $value = $trimmed.Substring($separator + 1).Trim()
        if ($key -notmatch "^[A-Za-z_][A-Za-z0-9_]*$") {
            throw "Malformed local environment key at line ${lineNumber}."
        }
        if ($values.Contains($key)) {
            throw "Duplicate local environment key at line ${lineNumber}: $key"
        }
        $values[$key] = $value
    }
    return $values
}

function Import-UwoLocalEnvironment {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Overwrite
    )

    $values = ConvertFrom-UwoEnvFile -Path $Path
    $loaded = @()
    $preserved = @()
    foreach ($key in $values.Keys) {
        $current = [Environment]::GetEnvironmentVariable($key, "Process")
        if (-not $Overwrite -and -not [string]::IsNullOrEmpty($current)) {
            $preserved += $key
            continue
        }
        [Environment]::SetEnvironmentVariable($key, [string]$values[$key], "Process")
        $loaded += $key
    }
    Write-Verbose ("Loaded local environment keys: " + ($loaded -join ", "))
    Write-Verbose ("Preserved existing process keys: " + ($preserved -join ", "))
    return [pscustomobject]@{
        LoadedKeys = $loaded
        PreservedKeys = $preserved
    }
}

function Assert-UwoDevelopmentDatabaseUrl {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Url)

    try {
        $parsed = [Uri]$Url
    } catch {
        throw "ACCESSIBILITY_DATABASE_URL is malformed."
    }
    if ($parsed.Scheme -notin @("postgres", "postgresql")) {
        throw "Development PostgreSQL must use a postgres or postgresql URL."
    }
    $hostName = $parsed.DnsSafeHost.ToLowerInvariant()
    if ($hostName.Contains("supabase")) {
        throw "Supabase hosts are not allowed for local accessibility development."
    }
    if ($hostName -notin @("localhost", "127.0.0.1", "::1")) {
        throw "Development PostgreSQL must use localhost or 127.0.0.1."
    }
    $databaseName = [Uri]::UnescapeDataString($parsed.AbsolutePath.TrimStart("/"))
    if ($databaseName -ne "uwo_housing_dev") {
        throw "Development PostgreSQL must name the uwo_housing_dev database."
    }
    return $true
}

function Assert-UwoLocalOtpUrl {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Url)

    try {
        $parsed = [Uri]$Url
    } catch {
        throw "OTP_BASE_URL is malformed."
    }
    if ($parsed.Scheme -ne "http") {
        throw "Local OpenTripPlanner must use an http URL."
    }
    if ($parsed.DnsSafeHost.ToLowerInvariant() -notin @("localhost", "127.0.0.1", "::1")) {
        throw "OpenTripPlanner must use localhost or 127.0.0.1."
    }
    return $true
}

function Protect-UwoDiagnosticText {
    [CmdletBinding()]
    param([AllowNull()][string]$Text)

    if ([string]::IsNullOrEmpty($Text)) { return "" }
    $sanitized = $Text -replace '(?i)postgres(?:ql)?://[^\s"''<>]+', '<redacted-database-url>'
    $sanitized = $sanitized -replace '(?i)(authorization\s*[:=]\s*)([^\s,;]+)', '$1<redacted>'
    $sanitized = $sanitized -replace '(?i)((?:password|api[_-]?key|token|secret)\s*[:=]\s*)([^\s,;]+)', '$1<redacted>'
    return $sanitized
}

function ConvertTo-UwoCommandLineArgument {
    param([AllowEmptyString()][string]$Value)

    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $slashes++
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * (($slashes * 2) + 1)))
            [void]$builder.Append('"')
        } else {
            if ($slashes) { [void]$builder.Append(('\' * $slashes)) }
            [void]$builder.Append($character)
        }
        $slashes = 0
    }
    if ($slashes) { [void]$builder.Append(('\' * ($slashes * 2))) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Invoke-UwoCapturedCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory = (Resolve-UwoRepositoryRoot)
    )

    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $FilePath
    $start.Arguments = (($Arguments | ForEach-Object { ConvertTo-UwoCommandLineArgument ([string]$_) }) -join " ")
    $start.WorkingDirectory = $WorkingDirectory
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    if (-not $process.Start()) {
        throw "Could not start command: $FilePath"
    }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $stdout = $stdoutTask.Result
    $stderr = $stderrTask.Result
    $exitCode = $process.ExitCode
    $process.Dispose()
    return [pscustomobject]@{
        ExitCode = $exitCode
        StdOut = $stdout
        StdErr = $stderr
    }
}

function Invoke-UwoDocker {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    return Invoke-UwoCapturedCommand -FilePath "docker" -Arguments $Arguments
}

function Test-UwoDockerDaemon {
    [CmdletBinding()]
    param()

    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        return $false
    }
    $result = Invoke-UwoDocker -Arguments @("info", "--format", "{{.ServerVersion}}")
    return $result.ExitCode -eq 0
}

function Get-UwoContainerState {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Name)

    $result = Invoke-UwoDocker -Arguments @("inspect", $Name)
    if ($result.ExitCode -ne 0) {
        return [pscustomobject]@{
            Exists = $false
            Running = $false
            Status = "missing"
            Image = $null
            ComposeProject = ""
            ComposeService = ""
        }
    }
    $item = ($result.StdOut | ConvertFrom-Json)[0]
    $composeProject = ""
    $composeService = ""
    if ($null -ne $item.Config.Labels) {
        $projectProperty = $item.Config.Labels.PSObject.Properties["com.docker.compose.project"]
        $serviceProperty = $item.Config.Labels.PSObject.Properties["com.docker.compose.service"]
        if ($null -ne $projectProperty) { $composeProject = [string]$projectProperty.Value }
        if ($null -ne $serviceProperty) { $composeService = [string]$serviceProperty.Value }
    }
    return [pscustomobject]@{
        Exists = $true
        Running = [bool]$item.State.Running
        Status = [string]$item.State.Status
        Image = [string]$item.Config.Image
        ComposeProject = $composeProject
        ComposeService = $composeService
    }
}

function Test-UwoCanonicalDevelopmentContainer {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Service
    )

    $state = Get-UwoContainerState -Name $Name
    return (
        -not $state.Exists -or
        ($state.ComposeProject -eq "uwo-housing-dev" -and $state.ComposeService -eq $Service)
    )
}

function Get-UwoLegacyDevelopmentContainers {
    [CmdletBinding()]
    param()

    $legacy = @()
    foreach ($definition in @(
        @{ Name = "uwo-postgres-dev"; Service = "postgres" },
        @{ Name = "uwo-otp-router"; Service = "otp" }
    )) {
        $state = Get-UwoContainerState -Name $definition.Name
        if (
            $state.Exists -and
            ($state.ComposeProject -ne "uwo-housing-dev" -or $state.ComposeService -ne $definition.Service)
        ) {
            $legacy += $definition.Name
        }
    }
    return $legacy
}

function ConvertTo-UwoContainerEnvironment {
    [CmdletBinding()]
    param([AllowEmptyCollection()][object[]]$Entries)

    $values = @{}
    foreach ($entry in @($Entries)) {
        if ($entry -isnot [string]) { continue }
        $separator = $entry.IndexOf("=")
        if ($separator -lt 1) { continue }
        $key = $entry.Substring(0, $separator)
        if ($key -notmatch "^[A-Za-z_][A-Za-z0-9_]*$") { continue }
        # Preserve the complete value after the first delimiter. Docker values
        # such as passwords may themselves contain one or more '=' characters.
        $values[$key] = $entry.Substring($separator + 1)
    }
    return $values
}

function Get-UwoContainerEnvironment {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Name)

    $result = Invoke-UwoDocker -Arguments @("inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", $Name)
    if ($result.ExitCode -ne 0) {
        throw "Could not inspect local container $Name."
    }
    # Invoke-UwoCapturedCommand normally returns one newline-delimited string,
    # but callers and test hosts can present stdout as a collection. Do not let
    # PowerShell's string coercion join distinct Docker environment entries.
    $entries = if ($result.StdOut -is [string]) {
        @($result.StdOut -split "`r?`n")
    } else {
        @($result.StdOut)
    }
    return ConvertTo-UwoContainerEnvironment -Entries $entries
}

function Get-UwoContainerHostPort {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][int]$ContainerPort
    )

    $result = Invoke-UwoDocker -Arguments @("port", $Name, "${ContainerPort}/tcp")
    if ($result.ExitCode -ne 0 -or $result.StdOut.Trim() -notmatch ":(?<port>\d+)\s*$") {
        throw "Could not discover the localhost port for $Name."
    }
    return [int]$Matches.port
}

function Get-UwoContainerBindingAddresses {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Name)

    $result = Invoke-UwoDocker -Arguments @("inspect", "--format", "{{json .HostConfig.PortBindings}}", $Name)
    if ($result.ExitCode -ne 0) { throw "Could not inspect published ports for $Name." }
    $bindings = $result.StdOut | ConvertFrom-Json
    $addresses = @()
    foreach ($port in $bindings.PSObject.Properties) {
        foreach ($binding in @($port.Value)) {
            $addresses += [string]$binding.HostIp
        }
    }
    return @($addresses | Sort-Object -Unique)
}

function Test-UwoLoopbackBindings {
    [CmdletBinding()]
    param([AllowEmptyCollection()][string[]]$Addresses)

    if ($null -eq $Addresses -or @($Addresses).Count -eq 0) { return $false }
    return @($Addresses | Where-Object { $_ -notin @("127.0.0.1", "::1") }).Count -eq 0
}

function Get-UwoDevelopmentComposeConfig {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$ComposePath)

    if (-not (Test-Path -LiteralPath $ComposePath -PathType Leaf)) {
        throw "Canonical development Compose file is missing: $ComposePath"
    }
    $previousPassword = [Environment]::GetEnvironmentVariable("POSTGRES_PASSWORD", "Process")
    if ([string]::IsNullOrEmpty($previousPassword)) {
        [Environment]::SetEnvironmentVariable("POSTGRES_PASSWORD", "compose-validation-placeholder", "Process")
    }
    try {
        $result = Invoke-UwoDocker -Arguments @("compose", "-f", $ComposePath, "config", "--format", "json")
    } finally {
        [Environment]::SetEnvironmentVariable("POSTGRES_PASSWORD", $previousPassword, "Process")
    }
    if ($result.ExitCode -ne 0) {
        throw "Canonical development Compose configuration is invalid: $(Protect-UwoDiagnosticText $result.StdErr.Trim())"
    }
    try { return $result.StdOut | ConvertFrom-Json } catch {
        throw "Docker Compose returned invalid configuration JSON."
    }
}

function Assert-UwoDevelopmentComposeEnvironment {
    [CmdletBinding()]
    param()

    $user = [Environment]::GetEnvironmentVariable("POSTGRES_USER", "Process")
    $database = [Environment]::GetEnvironmentVariable("POSTGRES_DB", "Process")
    $password = [Environment]::GetEnvironmentVariable("POSTGRES_PASSWORD", "Process")
    if ([string]::IsNullOrEmpty($user)) { $user = "uwo" }
    if ([string]::IsNullOrEmpty($database)) { $database = "uwo_housing_dev" }
    if ($user -ne "uwo") {
        throw "Canonical development PostgreSQL requires POSTGRES_USER=uwo."
    }
    if ($database -ne "uwo_housing_dev") {
        throw "Canonical development PostgreSQL requires POSTGRES_DB=uwo_housing_dev."
    }
    if ([string]::IsNullOrEmpty($password)) {
        throw "POSTGRES_PASSWORD was not loaded. Set the local-only value in config/local-dev.env; it is never printed."
    }
    return $true
}

function New-UwoPostgresUrl {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$User,
        [Parameter(Mandatory = $true)][string]$Password,
        [Parameter(Mandatory = $true)][string]$Database,
        [Parameter(Mandatory = $true)][int]$Port
    )

    $encodedUser = [Uri]::EscapeDataString($User)
    $encodedPassword = [Uri]::EscapeDataString($Password)
    $encodedDatabase = [Uri]::EscapeDataString($Database)
    return "postgresql://${encodedUser}:${encodedPassword}@127.0.0.1:${Port}/${encodedDatabase}"
}

function Get-UwoPostgresUrlFromContainer {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [int]$ContainerPort = 5432
    )

    $environment = Get-UwoContainerEnvironment -Name $Name
    foreach ($key in @("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")) {
        if (-not $environment.ContainsKey($key) -or [string]::IsNullOrEmpty($environment[$key])) {
            throw "$Name does not define the required local PostgreSQL environment."
        }
    }
    $port = Get-UwoContainerHostPort -Name $Name -ContainerPort $ContainerPort
    return New-UwoPostgresUrl -User $environment["POSTGRES_USER"] -Password $environment["POSTGRES_PASSWORD"] -Database $environment["POSTGRES_DB"] -Port $port
}

function Initialize-UwoLocalEnvironment {
    [CmdletBinding()]
    param(
        [string]$RepositoryRoot = (Resolve-UwoRepositoryRoot),
        [string]$EnvironmentFile = (Join-Path (Resolve-UwoRepositoryRoot) "config\local-dev.env"),
        [switch]$RequireDatabase
    )

    $loaded = $null
    if (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf) {
        $loaded = Import-UwoLocalEnvironment -Path $EnvironmentFile
    }
    if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable("OTP_BASE_URL", "Process"))) {
        [Environment]::SetEnvironmentVariable("OTP_BASE_URL", "http://127.0.0.1:8080", "Process")
    }
    [void](Assert-UwoLocalOtpUrl -Url $env:OTP_BASE_URL)

    $databaseSource = "process"
    if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable("ACCESSIBILITY_DATABASE_URL", "Process"))) {
        $databaseSource = "container"
        $container = Get-UwoContainerState -Name "uwo-postgres-dev"
        if ($container.Exists) {
            $databaseUrl = Get-UwoPostgresUrlFromContainer -Name "uwo-postgres-dev"
            [Environment]::SetEnvironmentVariable("ACCESSIBILITY_DATABASE_URL", $databaseUrl, "Process")
        }
    }
    $databaseUrl = [Environment]::GetEnvironmentVariable("ACCESSIBILITY_DATABASE_URL", "Process")
    if (-not [string]::IsNullOrEmpty($databaseUrl)) {
        [void](Assert-UwoDevelopmentDatabaseUrl -Url $databaseUrl)
    } elseif ($RequireDatabase) {
        throw "ACCESSIBILITY_DATABASE_URL was not loaded. Create config/local-dev.env from config/local-dev.example.env, then run: .\scripts\dev.ps1 start"
    }
    return [pscustomobject]@{
        EnvironmentFileLoaded = $null -ne $loaded
        DatabaseSource = if ([string]::IsNullOrEmpty($databaseUrl)) { "missing" } else { $databaseSource }
    }
}

function Wait-UwoPostgresReady {
    [CmdletBinding()]
    param(
        [string]$Name = "uwo-postgres-dev",
        [int]$TimeoutSeconds = 90
    )

    $environment = Get-UwoContainerEnvironment -Name $Name
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $result = Invoke-UwoDocker -Arguments @("exec", $Name, "pg_isready", "-U", $environment.POSTGRES_USER, "-d", $environment.POSTGRES_DB)
        if ($result.ExitCode -eq 0) { return $true }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Test-UwoOtpReady {
    [CmdletBinding()]
    param([string]$BaseUrl = "http://127.0.0.1:8080")

    [void](Assert-UwoLocalOtpUrl -Url $BaseUrl)
    $body = @{ query = "{ serviceTimeRange { start end } }" } | ConvertTo-Json -Compress
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Method Post -Uri ($BaseUrl.TrimEnd("/") + "/otp/gtfs/v1") -ContentType "application/json" -Body $body -TimeoutSec 5
        if ($response.StatusCode -ne 200) { return $false }
        $payload = $response.Content | ConvertFrom-Json
        return $null -ne $payload.data.serviceTimeRange
    } catch {
        return $false
    }
}

function Wait-UwoOtpReady {
    [CmdletBinding()]
    param(
        [string]$BaseUrl = "http://127.0.0.1:8080",
        [int]$TimeoutSeconds = 90
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if (Test-UwoOtpReady -BaseUrl $BaseUrl) { return $true }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Get-UwoLatestAccessibilityRun {
    [CmdletBinding()]
    param([string]$RunRoot = (Join-Path (Resolve-UwoRepositoryRoot) "data\accessibility-runs"))

    if (-not (Test-Path -LiteralPath $RunRoot -PathType Container)) { return $null }
    $candidates = @()
    foreach ($directory in Get-ChildItem -LiteralPath $RunRoot -Directory) {
        $manifestPath = Join-Path $directory.FullName "manifest.json"
        if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { continue }
        try {
            $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
            if ([string]$manifest.run_id -ne $directory.Name) { continue }
            $startedAt = [DateTimeOffset]::Parse([string]$manifest.started_at)
            $candidates += [pscustomobject]@{
                RunId = $directory.Name
                Path = $directory.FullName
                StartedAt = $startedAt
                Manifest = $manifest
            }
        } catch {
            Write-Verbose "Ignoring invalid accessibility run directory: $($directory.FullName)"
        }
    }
    return $candidates | Sort-Object StartedAt -Descending | Select-Object -First 1
}

function Get-UwoRoutingWorkerArguments {
    [CmdletBinding()]
    param(
        [ValidateSet("preflight", "run")][string]$Command,
        [Parameter(Mandatory = $true)][string]$ConfigPath,
        [Parameter(Mandatory = $true)][string]$PropertiesPath,
        [string[]]$PropertyIds = @(),
        [string]$Modes = "walking,cycling,transit",
        [string]$HotspotId = "western-main-campus"
    )

    $arguments = @("-m", "scripts.run_accessibility_worker", $Command, "--config", $ConfigPath, "--properties", $PropertiesPath, "--hotspot-ids", $HotspotId)
    if ($PropertyIds.Count -gt 0) {
        $arguments += @("--property-ids", ($PropertyIds -join ","))
    }
    if ($Command -eq "run") {
        $arguments += @("--modes", $Modes)
    }
    return $arguments
}

Export-ModuleMember -Function @(
    "Resolve-UwoRepositoryRoot",
    "Get-UwoPythonPath",
    "ConvertFrom-UwoEnvFile",
    "Import-UwoLocalEnvironment",
    "Assert-UwoDevelopmentDatabaseUrl",
    "Assert-UwoLocalOtpUrl",
    "Protect-UwoDiagnosticText",
    "Invoke-UwoCapturedCommand",
    "Invoke-UwoDocker",
    "Test-UwoDockerDaemon",
    "Get-UwoContainerState",
    "Test-UwoCanonicalDevelopmentContainer",
    "Get-UwoLegacyDevelopmentContainers",
    "ConvertTo-UwoContainerEnvironment",
    "Get-UwoContainerEnvironment",
    "Get-UwoContainerHostPort",
    "Get-UwoContainerBindingAddresses",
    "Test-UwoLoopbackBindings",
    "Get-UwoDevelopmentComposeConfig",
    "Assert-UwoDevelopmentComposeEnvironment",
    "New-UwoPostgresUrl",
    "Get-UwoPostgresUrlFromContainer",
    "Initialize-UwoLocalEnvironment",
    "Wait-UwoPostgresReady",
    "Test-UwoOtpReady",
    "Wait-UwoOtpReady",
    "Get-UwoLatestAccessibilityRun",
    "Get-UwoRoutingWorkerArguments"
)
