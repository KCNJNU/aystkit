# Shared by the Windows launchers. Keep console messages ASCII for PowerShell 5.1.
$script:WarehouseRoot = Split-Path -Parent $PSScriptRoot
$script:WarehouseDocker = $null

function Assert-WarehouseDocker {
    $dockerCommand = Get-Command docker -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($dockerCommand) {
        $script:WarehouseDocker = $dockerCommand.Source
    } else {
        # An IDE opened before Docker installation may still have the old PATH.
        $installPaths = @(
            (Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin/docker.exe'),
            (Join-Path $env:ProgramFiles 'Docker/Docker/resources/bin/docker.exe')
        )
        $script:WarehouseDocker = $installPaths | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf -ErrorAction SilentlyContinue } | Select-Object -First 1
    }
    if (-not $script:WarehouseDocker) {
        throw 'Docker was not found. Install Docker Desktop, select Linux containers, start it, then run start-all.cmd again.'
    }
    & $script:WarehouseDocker compose version
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose v2 is required. Update Docker Desktop.' }
    $engine = & $script:WarehouseDocker info --format '{{.OSType}}'
    if ($LASTEXITCODE -ne 0) { throw 'Docker Engine is unavailable. Start Docker Desktop and wait until the engine is running.' }
    if (($engine | Out-String).Trim() -ne 'linux') { throw 'Switch Docker Desktop to Linux containers before starting this project.' }
}

function Invoke-WarehouseCompose {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    & $script:WarehouseDocker compose --project-directory $script:WarehouseRoot --file (Join-Path $script:WarehouseRoot 'docker-compose.yml') --env-file (Join-Path $script:WarehouseRoot '.env') @Arguments
    if ($LASTEXITCODE -ne 0) {
        if ($Arguments[0] -eq 'up') {
            throw "Docker Compose startup/build failed (exit $LASTEXITCODE). If the error mentions 'load metadata', 'failed to resolve source metadata', 'no such host' or a registry timeout, check Docker Desktop > Settings > Docker Engine > registry-mirrors, and Settings > Resources > Proxies. See DEPLOYMENT.md for registry troubleshooting. Otherwise inspect docker compose ps and docker compose logs --tail 100 backend worker migration storage-init."
        }
        throw "Docker Compose failed (exit $LASTEXITCODE). Check the error above. To inspect services: docker compose ps; docker compose logs --tail 100 backend worker migration storage-init"
    }
}

function Wait-WarehouseHttp {
    param([string]$Url, [int]$TimeoutSeconds)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -eq 200) { return }
        } catch { }
        Write-Host "Waiting for $Url ..."
        Start-Sleep -Seconds 3
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Service did not become ready within $TimeoutSeconds seconds: $Url. Inspect docker compose ps and docker compose logs --tail 100 backend worker web."
}
