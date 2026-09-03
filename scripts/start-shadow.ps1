<#
.SYNOPSIS
    Start the Shadow loop if one is not already running.

.DESCRIPTION
    The chaining that kept a Shadow session running was a background shell
    waiting for the previous one to print "stopped". That works while the
    session that started it is alive and disappears with it, which is the
    wrong lifetime for something whose whole job is to still be running
    tomorrow.

    This is the same intent as a supervisor rather than a chain: asked often
    enough, it starts a loop when there is none and does nothing when there
    is. Nothing here decides to stop a loop - a session an operator ended on
    purpose is not a fault to repair, and by the time this runs there is no
    way to tell the two apart.

    The loop places no orders. It has no execution port; it evaluates real
    bars against the live account and records decisions.

.NOTES
    Secrets come from .env and are set on this process only. Nothing is
    written to a log or echoed.
#>

[CmdletBinding()]
param(
    [string] $Repository = "C:\workspace\personal\finance-auto-trading-main",
    [string] $Python = "C:\workspace\personal\finance-auto-trading-task-2\.venv\Scripts\python.exe",
    [string] $Account = $env:AUTOTRADER_ACCOUNT_ALIAS,
    [int]    $Leverage = 3,
    [string] $RunFor = "12h",
    [string] $Database = "finance_auto_trading_prod"
)

$ErrorActionPreference = "Stop"

function Find-ShadowLoop {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like "*autotrader.apps.trader*--shadow*" }
}

$running = Find-ShadowLoop
if ($running) {
    "already running: PID " + (($running.ProcessId) -join ", ")
    exit 0
}

if (-not (Test-Path $Repository)) { throw "repository not found: $Repository" }
if (-not (Test-Path $Python)) { throw "interpreter not found: $Python" }

$envFile = Join-Path $Repository ".env"
if (-not (Test-Path $envFile)) { throw "no .env beside the repository" }

# Only KEY=VALUE lines. Values are not logged.
foreach ($line in Get-Content $envFile) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
    $split = $trimmed.IndexOf("=")
    if ($split -lt 1) { continue }
    $name = $trimmed.Substring(0, $split).Trim()
    $value = $trimmed.Substring($split + 1).Trim()
    Set-Item -Path ("Env:" + $name) -Value $value
}

# The production database, whatever .env happens to name, and the source tree
# this script belongs to rather than whatever is installed.
$env:MYSQL_DATABASE = $Database
$env:PYTHONPATH = Join-Path $Repository "src"
$env:PYTHONUNBUFFERED = "1"

$logs = Join-Path $Repository "build\shadow-logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")

# Detached on purpose: this has to outlive whatever started it.
$process = Start-Process -FilePath $Python -PassThru -WindowStyle Hidden `
    -WorkingDirectory $Repository `
    -RedirectStandardOutput (Join-Path $logs "$stamp.out") `
    -RedirectStandardError (Join-Path $logs "$stamp.err") `
    -ArgumentList @(
        "-u", "-m", "autotrader.apps.trader",
        "--account", $Account, "--run", "--shadow",
        "--leverage", "$Leverage", "--for", $RunFor
    )

Start-Sleep -Seconds 8
if (-not (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) {
    $stderr = Join-Path $logs "$stamp.err"
    throw "the loop exited immediately; see $stderr"
}
"started: PID $($process.Id), for $RunFor, log $logs\$stamp.out"
