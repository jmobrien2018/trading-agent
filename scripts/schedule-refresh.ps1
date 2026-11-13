<#
.SYNOPSIS
    Register (or remove) a Windows scheduled task that runs `trading-agent data refresh`.

.DESCRIPTION
    Runs every few minutes, around the clock, while you are logged on. The refresh itself checks
    Alpaca's market clock: outside market hours a run finds nothing new and exits in seconds.
    Uses pythonw.exe so no console window pops up. Output goes to logs\trading-agent.log.

    The task runs as you, only while you are logged on, so it can read the API keys from your
    Windows Credential Manager. No password is stored. A run still going when the next one is due
    is left alone (the new one is skipped), and any run is stopped after 30 minutes.

.EXAMPLE
    .\scripts\schedule-refresh.ps1                    # every 5 minutes
    .\scripts\schedule-refresh.ps1 -EveryMinutes 2
    .\scripts\schedule-refresh.ps1 -Remove
#>
param(
    [ValidateRange(1, 60)][int]$EveryMinutes = 5,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$TaskName = "trading-agent data refresh"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    return
}

$Root = Split-Path -Parent $PSScriptRoot
$PythonW = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $PythonW)) {
    throw "Not found: $PythonW. Create the virtual environment first (see README)."
}

$action = New-ScheduledTaskAction -Execute $PythonW -Argument "-m trading_agent.cli data refresh" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Keeps the trading-agent market data up to date (paper only, read-only)." `
    -Force | Out-Null

Write-Host "Registered '$TaskName': every $EveryMinutes minute(s) while you are logged on."
Write-Host "Check it with:  trading-agent data status"
Write-Host "Remove it with: .\scripts\schedule-refresh.ps1 -Remove"
