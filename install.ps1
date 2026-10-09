# SourceFerry one-command Windows installation. PowerShell creates secrets; Python runs in Docker.
[CmdletBinding()]
param(
    [switch]$Verify,
    [switch]$ShowToken,
    [switch]$Help,
    [ValidateRange(1, 3600)][int]$WaitTimeout = 300
)

$ErrorActionPreference = 'Stop'
$InstallRoot = $PSScriptRoot
$HelperImage = 'python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f'
$ProjectName = if ($env:COMPOSE_PROJECT_NAME) { $env:COMPOSE_PROJECT_NAME } else { 'sourceferry' }
$EnvFile = Join-Path $InstallRoot '.env'
$ReportFile = Join-Path $InstallRoot 'artifacts/installation-report.json'
$Stage = 'prerequisites'
$HelperReady = $false
$ReportReady = $false
$SavedEnvironment = @{}
$SettingsKeys = @('LOCAL_WEB_API_TOKEN', 'CRAWL4AI_API_TOKEN', 'SEARXNG_SECRET_KEY', 'GATEWAY_BIND_ADDRESS', 'GATEWAY_PORT', 'LOCAL_WEB_GATEWAY_URL', 'PYTHON_IMAGE', 'SEARXNG_IMAGE', 'CRAWL4AI_IMAGE', 'SEARXNG_TIMEOUT_SECONDS', 'CRAWL4AI_TIMEOUT_SECONDS', 'ENRICHMENT_CONCURRENCY', 'SNIPPET_MAX_CHARS', 'API_CLIENT_TIMEOUT_SECONDS')
$OriginalLocation = Get-Location

function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw 'Docker command failed. See its output above.' }
}

function Invoke-Helper {
    Invoke-Docker run --rm --user 0 --env PYTHONDONTWRITEBYTECODE=1 --mount "type=bind,source=$InstallRoot,target=/workspace" --workdir /workspace $HelperImage python /workspace/scripts/install.py @args
}

function Invoke-Compose {
    Invoke-Docker compose --project-name $ProjectName --env-file $EnvFile --file (Join-Path $InstallRoot 'docker-compose.yml') @args
}

function Add-Pass {
    Invoke-Helper report --stage $Stage --status passed
    Write-Host "PASS $Stage"
}

function Read-Environment([string]$Text) {
    $Values = @{}
    $LineNumber = 0
    foreach ($RawLine in ($Text -split '\r?\n')) {
        $LineNumber++
        $Line = $RawLine.Trim()
        if (-not $Line -or $Line.StartsWith('#')) { continue }
        if ($Line -notmatch '^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { throw "Invalid .env assignment on line $LineNumber." }
        $Key = $Matches[1]
        $Value = $Matches[2]
        if ($Values.ContainsKey($Key)) { throw "Duplicate .env setting: $Key." }
        if ($Value.Length -ge 2 -and $Value[0] -eq $Value[$Value.Length - 1] -and ($Value[0] -eq '"' -or $Value[0] -eq "'")) {
            $Value = $Value.Substring(1, $Value.Length - 2)
        }
        $Values[$Key] = $Value
    }
    return $Values
}

function Test-Placeholder([string]$Value) {
    return (-not $Value -or $Value -match '^(CHANGE_ME|REPLACE_ME|<)')
}

function New-Secret {
    $Bytes = New-Object byte[] 32
    $Generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $Generator.GetBytes($Bytes) } finally { $Generator.Dispose() }
    return ([BitConverter]::ToString($Bytes)).Replace('-', '').ToLowerInvariant()
}

function Set-PrivateFile([string]$Path) {
    $Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    # Remove inherited access before writing any secret into the new empty file.
    & icacls $Path /inheritance:r /grant:r "$($Identity):(F)" > $null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot protect .env. Use a local NTFS installation directory with working icacls permissions.' }
}

function Initialize-Environment {
    if (Test-Path -LiteralPath $EnvFile) {
        if ((Get-Item -LiteralPath $EnvFile).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Refusing a link for .env.' }
        $Content = [IO.File]::ReadAllText($EnvFile)
    } else {
        $Content = [IO.File]::ReadAllText((Join-Path $InstallRoot '.env.example'))
    }
    $Values = Read-Environment $Content
    $Defaults = Read-Environment ([IO.File]::ReadAllText((Join-Path $InstallRoot '.env.example')))
    foreach ($Key in $Defaults.Keys) {
        if (-not $Values.ContainsKey($Key)) {
            $Content = $Content.TrimEnd() + "`n$Key=$($Defaults[$Key])`n"
            $Values[$Key] = $Defaults[$Key]
        }
    }
    $Kept = New-Object 'System.Collections.Generic.HashSet[string]'
    $SecretKeys = @('LOCAL_WEB_API_TOKEN', 'CRAWL4AI_API_TOKEN', 'SEARXNG_SECRET_KEY')
    foreach ($Key in $SecretKeys) {
        $Value = $Values[$Key]
        if (-not (Test-Placeholder $Value)) {
            if ($Value.Length -lt 32 -or $Value -match '\s') { throw "$Key must contain at least 32 characters without whitespace. Correct .env before retrying." }
            if (-not $Kept.Add($Value)) { throw 'The three .env secrets must be distinct. Correct .env before retrying.' }
        }
    }
    foreach ($Key in $SecretKeys) {
        if (Test-Placeholder $Values[$Key]) {
            do { $Value = New-Secret } while (-not $Kept.Add($Value))
            $Content = [regex]::Replace($Content, "(?m)^$Key\s*=.*$", "$Key=$Value")
        }
    }
    $TemporaryFile = Join-Path $InstallRoot ('.env.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::Create($TemporaryFile).Dispose()
        Set-PrivateFile $TemporaryFile
        [IO.File]::WriteAllText($TemporaryFile, ($Content.TrimEnd() + "`n"), (New-Object System.Text.UTF8Encoding($false)))
        $SourcePath = [IO.Path]::GetFullPath($TemporaryFile)
        $TargetPath = [IO.Path]::GetFullPath($EnvFile)
        $RootPrefix = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
        if (-not $SourcePath.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase) -or $TargetPath -ne [IO.Path]::GetFullPath((Join-Path $InstallRoot '.env'))) {
            throw 'Refusing to move an environment file outside the installation directory.'
        }
        Move-Item -LiteralPath $SourcePath -Destination $TargetPath -Force
        Set-PrivateFile $EnvFile
    } finally {
        if (Test-Path -LiteralPath $TemporaryFile) { Remove-Item -LiteralPath $TemporaryFile -Force }
    }
    Write-Host 'PASS Private .env prepared; existing secrets and configuration preserved.'
}

try {
    if ($Help) {
        Write-Host 'SourceFerry - web search and fetch for AI clients'
        Write-Host 'Usage: .\install.ps1 [-Verify | -ShowToken] [-WaitTimeout 300]'
        Write-Host 'Requires Docker Desktop with Linux containers and Docker Compose v2.24+.'
        Write-Host '-Verify reruns all acceptance checks; -ShowToken displays saved client settings.'
        Write-Host 'This product includes software developed by UncleCode (https://x.com/unclecode) as part of the Crawl4AI project (https://github.com/unclecode/crawl4ai).'
        exit 0
    }
    if ($Verify -and $ShowToken) { throw 'Use either -Verify or -ShowToken.' }
    if ($env:OS -ne 'Windows_NT') { throw 'Use bash ./install.sh on Linux.' }
    if ($ProjectName -notmatch '^[a-z0-9][a-z0-9_-]*$') { throw 'COMPOSE_PROJECT_NAME must contain lowercase letters, digits, underscores or hyphens.' }
    Set-Location -LiteralPath $InstallRoot
    if (-not $ShowToken) {
        $ReportDirectory = Split-Path -Parent $ReportFile
        foreach ($Path in @($ReportDirectory, $ReportFile)) {
            if ((Test-Path -LiteralPath $Path) -and ((Get-Item -LiteralPath $Path).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Refusing a link for the report directory or file.' }
        }
        [IO.Directory]::CreateDirectory($ReportDirectory) > $null
        [IO.File]::WriteAllText($ReportFile, '{"installer_status":"running","installer_stages":[]}', (New-Object System.Text.UTF8Encoding($false)))
        $ReportReady = $true
    }
    foreach ($Key in $SettingsKeys) {
        $SavedEnvironment[$Key] = [Environment]::GetEnvironmentVariable($Key, 'Process')
        Remove-Item -LiteralPath "Env:$Key" -ErrorAction SilentlyContinue
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Install Docker Desktop with the WSL 2 backend first, start it, and switch to Linux containers. See README.md.' }
    $DockerInfo = (Invoke-Docker info --format '{{.OSType}}|{{.NCPU}}|{{.MemTotal}}') -split '\|'
    if ($DockerInfo[0] -ne 'linux') { throw 'Switch Docker Desktop to Linux containers.' }
    $ContextHost = Invoke-Docker context inspect --format '{{.Endpoints.docker.Host}}'
    $DockerHost = if ($env:DOCKER_HOST) { $env:DOCKER_HOST } else { $ContextHost }
    if ($DockerHost -notmatch '^(npipe|unix)://') { throw 'The installer requires a local Docker Desktop daemon to check the published Windows port.' }
    if ($ShowToken) {
        Invoke-Helper token
        exit 0
    }
    $ComposeVersion = Invoke-Docker compose version --short
    $Stage = 'helper-image'
    Invoke-Docker pull $HelperImage
    $HelperReady = $true
    Invoke-Helper report --initialize
    Add-Pass

    $Stage = 'prerequisites'
    Invoke-Helper preflight --compose-version $ComposeVersion --memory-bytes $DockerInfo[2] --cpus $DockerInfo[1]
    Add-Pass

    $Stage = 'configuration'
    if (-not $Verify) { Initialize-Environment }
    $Configuration = @(Invoke-Helper config)
    Invoke-Compose config --quiet
    if (-not $Verify) {
        $ExistingPort = ''
        try { $ExistingPort = Invoke-Compose port gateway 8080 2>$null } catch { }
        $Listener = $null
        try {
            $Listener = New-Object System.Net.Sockets.TcpListener([Net.IPAddress]::Parse($Configuration[0]), [int]$Configuration[1])
            $Listener.Start()
        } catch {
            if (-not ($ExistingPort -like "*:$($Configuration[1])")) { throw 'Cannot bind the configured gateway address/port. Choose a free port and a Windows interface address in .env.' }
        } finally { if ($Listener) { $Listener.Stop() } }
    }
    Add-Pass

    $Stage = 'images'
    if (-not $Verify) {
        Invoke-Compose pull searxng crawl4ai
        Invoke-Compose build gateway
    }
    if (-not $Verify) {
        $GatewayImage = Invoke-Docker image inspect --format '{{.Id}}' "$ProjectName-gateway"
    } else {
        $GatewayImages = @(Invoke-Compose images --quiet gateway)
        $GatewayImage = if ($GatewayImages.Count) { $GatewayImages[0] } else { $null }
    }
    if (-not $GatewayImage) { throw 'No installed gateway image was found. Run .\install.ps1 first.' }
    Add-Pass

    $Stage = 'offline-tests'
    Invoke-Docker run --rm --user 0 --env PYTHONDONTWRITEBYTECODE=1 --mount "type=bind,source=$InstallRoot,target=/workspace" --workdir /workspace $GatewayImage python -m unittest discover -s tests -v
    Add-Pass

    $Stage = 'services'
    if (-not $Verify) { Invoke-Compose up --detach --wait --wait-timeout $WaitTimeout gateway searxng crawl4ai }
    Add-Pass

    $Stage = 'published-health'
    $HealthUri = "$($Configuration[2])/health"
    # HttpClient bypasses system proxies and avoids PowerShell's web-page parser.
    Add-Type -AssemblyName System.Net.Http
    $Handler = New-Object System.Net.Http.HttpClientHandler
    $Handler.UseProxy = $false
    $Client = New-Object System.Net.Http.HttpClient($Handler)
    $Client.Timeout = [TimeSpan]::FromSeconds(15)
    try {
        $Response = $Client.GetAsync($HealthUri).GetAwaiter().GetResult()
        $Payload = $Response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
        if ([int]$Response.StatusCode -ne 200 -or $Payload.status -ne 'ok') { throw 'The published gateway health endpoint returned an unexpected response.' }
    } finally { $Client.Dispose(); $Handler.Dispose() }
    Add-Pass

    $Stage = 'verification'
    Invoke-Docker run --rm --network "$($ProjectName)_default" --user 0 --env PYTHONDONTWRITEBYTECODE=1 --mount "type=bind,source=$InstallRoot,target=/workspace" --workdir /workspace $GatewayImage python /workspace/scripts/verify.py --base-url http://gateway:8080 --env-file /workspace/.env --report /workspace/artifacts/installation-report.json
    Invoke-Helper report --stage verification --status verified
    Write-Host "`nInstallation verified. Report: artifacts/installation-report.json"
    Write-Host 'Save these client settings securely:'
    # Intentionally print only the gateway credential, after all checks pass.
    Invoke-Helper token
} catch {
    if ($HelperReady -and -not $ShowToken) {
        $Status = if ($Stage -eq 'verification') { 'verification_failed' } else { 'failed' }
        try { Invoke-Helper report --stage $Stage --status $Status *> $null } catch { }
    } elseif ($ReportReady -and -not $ShowToken) {
        [IO.File]::WriteAllText($ReportFile, ('{"installer_status":"failed","installer_stages":[{"stage":"' + $Stage + '","status":"failed"}]}'), (New-Object System.Text.UTF8Encoding($false)))
    }
    # Native command output already provides diagnostics. Never print captured data.
    Write-Host "FAIL $Stage. $($_.Exception.Message)" -ForegroundColor Red
    if ($Stage -eq 'verification') { Write-Host 'Installation completed; acceptance verification failed. The gateway stack is retained.' }
    Write-Host 'Report: artifacts/installation-report.json'
    Write-Host 'After fixing the problem, rerun .\install.ps1 (or -Verify to repeat checks).'
    exit 1
} finally {
    foreach ($Key in $SavedEnvironment.Keys) {
        if ($null -eq $SavedEnvironment[$Key]) {
            Remove-Item -LiteralPath "Env:$Key" -ErrorAction SilentlyContinue
        } else {
            Set-Item -LiteralPath "Env:$Key" -Value $SavedEnvironment[$Key]
        }
    }
    Set-Location -LiteralPath $OriginalLocation.Path
}
