@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if /i not "%~1"=="_bg" (
  powershell -NoProfile -WindowStyle Hidden -Command ^
    "Start-Process -FilePath '%~f0' -ArgumentList '_bg' -WindowStyle Hidden"
  exit /b 0
)

set "PY=%~dp0.venv\Scripts\python.exe"
set "BOT=%~dp0scripts\telegram_bot.py"
if not exist "%PY%" exit /b 1
if not exist "%BOT%" exit /b 1

powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*telegram_bot.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

powershell -NoProfile -Command ^
  "Start-Process -FilePath '%PY%' -ArgumentList '"%BOT%"' -WorkingDirectory '%~dp0' -WindowStyle Hidden"

endlocal
