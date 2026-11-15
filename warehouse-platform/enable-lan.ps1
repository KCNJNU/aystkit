#requires -Version 5.1
#requires -RunAsAdministrator
<#
.SYNOPSIS
Configures the existing Docker warehouse for HTTP access on the local network.
.PARAMETER IPAddress
Optional local IPv4 address. Otherwise selects the physical adapter with the
lowest default-route metric. VPN and virtual adapters are not auto-selected.
.PARAMETER NoBuild
Reuse images after the updated application has already been built once.
#>
[CmdletBinding()]
param([string]$IPAddress, [switch]$NoBuild)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts/warehouse-compose.ps1')

try {
    Assert-WarehouseDocker
    $envFile = Join-Path $PSScriptRoot '.env'
    if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { throw 'Root .env is missing. Configure the project with start-all.cmd first.' }

    $addresses = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
        $_.AddressState -eq 'Preferred' -and $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*'
    })
    if ($IPAddress) {
        $selected = $addresses | Where-Object { $_.IPAddress -eq $IPAddress } | Select-Object -First 1
        if (-not $selected) { throw 'IPAddress must be an active IPv4 address assigned to this computer.' }
    } else {
        $physicalIndexes = @(Get-NetAdapter -Physical | Where-Object { $_.Status -eq 'Up' } | Select-Object -ExpandProperty ifIndex)
        $routes = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' | Where-Object {
            $_.InterfaceIndex -in $physicalIndexes
        } | Sort-Object @{ Expression = { $_.RouteMetric + $_.InterfaceMetric } })
        $selected = $null
        foreach ($route in $routes) {
            $selected = $addresses | Where-Object { $_.InterfaceIndex -eq $route.InterfaceIndex -and -not $_.SkipAsSource } | Select-Object -First 1
            if ($selected) { break }
        }
        if (-not $selected) { throw 'No physical LAN adapter found. Run again with -IPAddress <local IPv4>.' }
        $IPAddress = $selected.IPAddress
    }

    Write-Host "Configuring LAN access on $($selected.InterfaceAlias): $IPAddress"
    # Update only these public connection settings. Never print or reset secrets.
    $envText = [IO.File]::ReadAllText($envFile)
    $originMatch = [regex]::Match($envText, '(?m)^CORS_ORIGIN=([^\r\n]*)')
    $origins = @('http://localhost:5173', 'http://127.0.0.1:5173', "http://${IPAddress}:5173")
    if ($originMatch.Success) { $origins += $originMatch.Groups[1].Value.Trim().Trim('"').Trim("'").Split(',') }
    $settings = [ordered]@{
        AUTH_COOKIE_SECURE = 'false'
        OBJECT_STORAGE_PUBLIC_ENDPOINT = $IPAddress
        OBJECT_STORAGE_PUBLIC_PORT = '9000'
        CORS_ORIGIN = (($origins | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Select-Object -Unique) -join ',')
    }
    foreach ($key in $settings.Keys) {
        $pattern = '(?m)^' + [regex]::Escape($key) + '=[^\r\n]*'
        $replacement = $key + '=' + $settings[$key]
        if ([regex]::IsMatch($envText, $pattern)) {
            $envText = [regex]::Replace($envText, $pattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($match) $replacement })
        } else { $envText = $envText.TrimEnd() + "`r`n" + $replacement + "`r`n" }
    }
    [IO.File]::WriteAllText($envFile, $envText, [Text.UTF8Encoding]::new($false))
    $envText = $null

    # Docker Compose already publishes these ports. No extra netsh portproxy is
    # necessary. Restrict our firewall rules to this adapter and local subnet.
    foreach ($port in @(5173, 9000)) {
        $ruleName = "RobotHygienism-LAN-$port"
        $rule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
        $ruleArgs = @{
            Direction = 'Inbound'; Action = 'Allow'; Enabled = 'True'; Profile = 'Any'
            Protocol = 'TCP'; LocalPort = $port; RemoteAddress = 'LocalSubnet'
            LocalAddress = $IPAddress; InterfaceAlias = $selected.InterfaceAlias
        }
        if ($rule) { Set-NetFirewallRule -Name $ruleName @ruleArgs | Out-Null }
        else { New-NetFirewallRule -Name $ruleName -DisplayName "RobotHygienism LAN TCP $port" @ruleArgs | Out-Null }
    }

    $buildArgument = '--build'
    if ($NoBuild) { $buildArgument = '--no-build' }
    Invoke-WarehouseCompose -Arguments @('up', '-d', $buildArgument, 'backend', 'worker', 'web')
    Wait-WarehouseHttp -Url "http://${IPAddress}:5173/healthz" -TimeoutSeconds 180
    Wait-WarehouseHttp -Url "http://${IPAddress}:5173/nest/ready" -TimeoutSeconds 180
    Wait-WarehouseHttp -Url "http://${IPAddress}:9000/minio/health/live" -TimeoutSeconds 60
    Write-Host ''
    Write-Host "LAN ready: http://${IPAddress}:5173" -ForegroundColor Green
    Write-Host 'Each person signs in with their own account. Assign users and project access in User Management.'
    Write-Host 'Keep this computer and Docker running. Rerun this script after the LAN IP changes.'
    Write-Host 'HTTP LAN mode uses AUTH_COOKIE_SECURE=false. Use true when deploying behind HTTPS.'
    exit 0
} catch {
    Write-Host ('LAN setup failed: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Run in an Administrator PowerShell. Configuration already applied is kept; rerunning is safe.'
    exit 1
}
