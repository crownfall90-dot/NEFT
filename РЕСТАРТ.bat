@echo off
rem Перезапуск NEFT: остановка, затем запуск.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0neft_stop.ps1" -Quiet
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0neft_start.ps1"
