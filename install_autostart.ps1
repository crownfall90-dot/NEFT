# NEFT autostart: один запуск при входе в Windows — проверяет и поднимает
# telegram_bot и admin_server, если они не запущены. Никакого повтора по
# расписанию: раньше тут же стояла задача "каждые 3 минуты", она заметно
# мигала окном консоли (Task Scheduler так и делает при прямом вызове
# powershell.exe, даже с -WindowStyle Hidden) и не нужна — сервисы и так
# держатся годами, а проверять их постоянно не просили.
# Run: powershell -ExecutionPolicy Bypass -File install_autostart.ps1
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Watchdog = Join-Path $Root "scripts\neft_watchdog.ps1"
$Vbs = Join-Path $Root "scripts\neft_watchdog_silent.vbs"
$LogonTask = "NEFT Services Logon"
$OldWatchTask = "NEFT Services Watchdog"

if (-not (Test-Path $Watchdog)) {
    Write-Error "missing $Watchdog"
}
if (-not (Test-Path $Vbs)) {
    Write-Error "missing $Vbs"
}

# Убираем старую задачу-вотчдог, если осталась от прежней установки.
cmd /c "schtasks /Delete /TN `"$OldWatchTask`" /F >nul 2>nul"

# wscript.exe + VBS-обёртка (WScript.Shell.Run с окном 0) — единственный
# по-настоящему бесшумный способ запуска через Task Scheduler на Windows.
$LogonAction = New-ScheduledTaskAction -Execute "wscript.exe" `
    -Argument "`"$Vbs`"" -WorkingDirectory $Root
$LogonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden

Register-ScheduledTask -TaskName $LogonTask -Action $LogonAction -Trigger $LogonTrigger `
    -Settings $Settings -Force | Out-Null

Write-Host "OK: logon task '$LogonTask' — однократно при входе, без мигания окна."
& $Watchdog
Write-Host "Services checked/started."
