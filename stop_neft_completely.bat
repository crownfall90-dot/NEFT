@echo off
setlocal

echo === NEFT: stopping everything and disabling autostart ===
echo.

echo [1/3] Disabling Windows Task Scheduler autostart task...
schtasks /End /TN "NEFT Services Logon" >nul 2>&1
schtasks /Change /TN "NEFT Services Logon" /DISABLE >nul 2>&1
if %errorlevel%==0 (
    echo     OK: task "NEFT Services Logon" disabled.
) else (
    echo     Task not found or already disabled - skipping.
)

echo.
echo [2/3] Stopping telegram_bot.py, admin_server.py and watchdog processes...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'telegram_bot\.py|admin_server\.py|neft_watchdog' } | ForEach-Object { Write-Host ('  stopping PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo.
echo [3/3] Verifying nothing is left running...
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'telegram_bot\.py|admin_server\.py|neft_watchdog' }; if ($p) { $p | Select-Object ProcessId, CommandLine | Format-Table -AutoSize } else { Write-Host '  Nothing running - clean.' }"

echo.
echo === Done. NEFT is fully stopped. ===
echo After a PC restart, services will NOT start on their own.
echo To re-enable autostart later, run install_autostart.ps1.
echo.
pause
endlocal
