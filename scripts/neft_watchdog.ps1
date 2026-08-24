# Поднимает telegram_bot и admin_server, если они не запущены.
$ErrorActionPreference = "SilentlyContinue"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$Bot = Join-Path $Root "scripts\telegram_bot.py"
$Admin = Join-Path $Root "scripts\admin_server.py"

if (-not (Test-Path $Py)) { exit 1 }

function Ensure-NeftProcess([string]$needle, [string]$script) {
    if (-not (Test-Path $script)) { return }
    $running = Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$needle*" }
    if ($running) { return }
    Start-Process -FilePath $Py -ArgumentList "`"$script`"" -WorkingDirectory $Root -WindowStyle Hidden
}

Ensure-NeftProcess "telegram_bot.py" $Bot
Start-Sleep -Seconds 1
Ensure-NeftProcess "admin_server.py" $Admin
