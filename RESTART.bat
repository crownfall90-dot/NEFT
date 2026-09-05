@echo off
rem Дубль РЕСТАРТ.bat латиницей — на случай проблем с кириллицей в именах.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0neft_stop.ps1" -Quiet
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0neft_start.ps1"
