#requires -Version 5.1
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts/warehouse-compose.ps1')

try {
    Assert-WarehouseDocker
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.env') -PathType Leaf)) {
        throw 'Root .env is missing. Restore the project configuration before stopping the Compose stack.'
    }
    Invoke-WarehouseCompose -Arguments @('stop', '--timeout', '30')
    Write-Host 'Warehouse stopped. Database, uploads and dataset volumes are preserved.' -ForegroundColor Green
    Write-Host 'Host npm run dev processes are separate; stop them with Ctrl+C in their terminal.'
    exit 0
} catch {
    Write-Host ('Stop failed: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
