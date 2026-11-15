#requires -Version 5.1
<#
.SYNOPSIS
Starts the complete warehouse stack using Docker Compose.
.PARAMETER CheckOnly
Checks Docker and configuration without building or starting containers.
.PARAMETER SkipSeed
Skips administrator bootstrap for a database that already has users.
.PARAMETER NoBuild
Reuses locally built application images.
#>
[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$SkipSeed,
    [switch]$NoBuild,
    [ValidateRange(15, 1800)][int]$ReadyTimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts/warehouse-compose.ps1')

try {
    Write-Host '[1/5] Checking Docker and project configuration...'
    Assert-WarehouseDocker
    $envFile = Join-Path $PSScriptRoot '.env'
    if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
        if ($CheckOnly) { throw 'Root .env is missing. Copy .env.example to .env and set your administrator credentials.' }
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot '.env.example') -Destination $envFile
        throw 'Created root .env from .env.example. Set BOOTSTRAP_ADMIN_EMAIL and a strong BOOTSTRAP_ADMIN_PASSWORD (12+ characters), then run start-all.cmd again.'
    }
    # Capture the resolved configuration: never print environment values or secrets.
    $configuration = (Invoke-WarehouseCompose -Arguments @('config', '--format', 'json') | Out-String) | ConvertFrom-Json
    $backendEnvironment = $configuration.services.migration.environment
    if (-not $SkipSeed) {
        $email = [string]$backendEnvironment.BOOTSTRAP_ADMIN_EMAIL
        $password = [string]$backendEnvironment.BOOTSTRAP_ADMIN_PASSWORD
        if (-not $email.Trim() -or $password.Length -lt 12 -or $password -match 'replace-with|replace-me') {
            throw 'Set BOOTSTRAP_ADMIN_EMAIL and a strong BOOTSTRAP_ADMIN_PASSWORD (12+ characters) in root .env. Existing installations may use start-all.cmd -SkipSeed.'
        }
        $password = $null
    }
    if (-not $backendEnvironment.QWEN_API_KEY) {
        Write-Warning 'QWEN_API_KEY is not configured. Warehouse features remain available; AI requests require the key.'
    }
    $configuration = $null
    $backendEnvironment = $null
    if ($CheckOnly) {
        Write-Host 'Preflight passed. No containers were started. Ports: 3000, 5173, 5432, 6379, 9000, 9001.'
        exit 0
    }

    Write-Host '[2/5] Starting PostgreSQL, Redis, MinIO, migrations, API, worker and web...'
    Write-Host 'The first run downloads and builds images; this can take several minutes.'
    $upArguments = @('up', '-d')
    if ($NoBuild) { $upArguments += '--no-build' } else { $upArguments += '--build' }
    Invoke-WarehouseCompose -Arguments $upArguments

    Write-Host '[3/5] Waiting for database, queue and file storage readiness...'
    Wait-WarehouseHttp -Url 'http://localhost:3000/ready' -TimeoutSeconds $ReadyTimeoutSeconds
    if (-not $SkipSeed) {
        Write-Host 'Initializing administrator and warehouse project (existing accounts are preserved)...'
        Invoke-WarehouseCompose -Arguments @('run', '--rm', '--no-deps', 'migration', 'npm', 'run', 'prisma:seed', '--workspace', '@cleaning-robot/api')
    }

    Write-Host '[4/5] Initializing the dataset version repository...'
    Invoke-WarehouseCompose -Arguments @('exec', '-T', 'worker', 'git', '-C', '/app/data/dataset-repository', 'init')

    Write-Host '[5/5] Checking the web server and API proxy...'
    Wait-WarehouseHttp -Url 'http://localhost:5173/healthz' -TimeoutSeconds $ReadyTimeoutSeconds
    Wait-WarehouseHttp -Url 'http://localhost:5173/nest/ready' -TimeoutSeconds $ReadyTimeoutSeconds
    $runningServices = @(Invoke-WarehouseCompose -Arguments @('ps', '--status', 'running', '--services'))
    foreach ($service in @('postgres', 'redis', 'minio', 'backend', 'worker', 'web')) {
        if ($runningServices -notcontains $service) { throw "Required service is not running: $service. Inspect docker compose logs --tail 100 $service." }
    }
    Invoke-WarehouseCompose -Arguments @('ps')
    Write-Host ''
    Write-Host 'Warehouse is ready.' -ForegroundColor Green
    Write-Host 'Web / Upload Center: http://localhost:5173'
    Write-Host 'API docs:            http://localhost:3000/api/docs'
    Write-Host 'MinIO console:       http://localhost:9001'
    Write-Host 'Login: use the administrator account configured in root .env.'
    Write-Host 'Stop: double-click stop-all.cmd. Closing this window keeps services running.'
    exit 0
} catch {
    Write-Host ''
    Write-Host ('Startup failed: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Services already started are kept for inspection. Use stop-all.cmd to stop them; data volumes are preserved.'
    exit 1
}
