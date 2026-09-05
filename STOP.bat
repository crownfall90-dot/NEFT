@echo off
rem Дубль ОСТАНОВКА.bat латиницей — на случай проблем с кириллицей в именах.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0neft_stop.ps1"
